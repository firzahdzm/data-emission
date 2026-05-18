import sqlite3
from datetime import datetime


def dashboard_summary(
    conn: sqlite3.Connection,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    """Return per-person cumulative emission in range, ordered desc."""
    cursor = conn.execute(
        """
        SELECT p.name,
               COALESCE(SUM(ns.emission), 0) AS cumulative
        FROM persons p
        LEFT JOIN hotkeys h ON h.person_id = p.id
        LEFT JOIN (
            neuron_snapshots ns
            JOIN snapshots s ON s.id = ns.snapshot_id
                             AND s.status IN ('ok', 'partial')
                             AND s.taken_at >= ?
                             AND s.taken_at <  ?
        ) ON ns.hotkey_ss58 = h.ss58
        GROUP BY p.name
        ORDER BY cumulative DESC, p.name ASC
        """,
        (from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def hotkey_series(
    conn: sqlite3.Connection,
    hotkey: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT CAST(s.taken_at AS TEXT) AS taken_at,
               ns.emission AS per_snapshot_emission,
               SUM(ns.emission) OVER (
                 PARTITION BY ns.hotkey_ss58
                 ORDER BY s.taken_at
               ) AS cumulative
        FROM neuron_snapshots ns
        JOIN snapshots s ON s.id = ns.snapshot_id
        WHERE ns.hotkey_ss58 = ?
          AND s.status IN ('ok', 'partial')
          AND s.taken_at >= ?
          AND s.taken_at <  ?
        ORDER BY s.taken_at
        """,
        (hotkey, from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def person_series(
    conn: sqlite3.Connection,
    name: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    cursor = conn.execute(
        """
        WITH per_snap AS (
            SELECT s.id AS snapshot_id,
                   CAST(s.taken_at AS TEXT) AS taken_at,
                   SUM(COALESCE(ns.emission, 0)) AS per_snapshot_emission
            FROM persons p
            JOIN hotkeys h           ON h.person_id = p.id
            LEFT JOIN neuron_snapshots ns ON ns.hotkey_ss58 = h.ss58
            JOIN snapshots s         ON s.id = ns.snapshot_id
            WHERE p.name = ?
              AND s.status IN ('ok', 'partial')
              AND s.taken_at >= ?
              AND s.taken_at <  ?
            GROUP BY s.id, s.taken_at
        )
        SELECT taken_at,
               per_snapshot_emission,
               SUM(per_snapshot_emission) OVER (ORDER BY taken_at) AS cumulative
        FROM per_snap
        ORDER BY taken_at
        """,
        (name, from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def captures_table(conn: sqlite3.Connection, limit: int = 20) -> dict:
    """Wide-format emission history: rows=hotkey, columns=last N snapshots.

    Returns:
        {
            "snapshots": [{"id": int, "taken_at": str}, ...]  # ascending time
            "rows": [
                {
                    "hotkey": ss58,
                    "name":   person name,
                    "cells":  [  # same length as snapshots
                        {"emission": float|None, "is_registered": 0|1} | None,
                        ...
                    ]
                },
                ...
            ]
        }

    Snapshots are the most recent N (status ok/partial), ordered ascending
    so older is on the left. A cell is None when the hotkey has no row in
    that snapshot (worker error / hotkey didn't exist yet).
    """
    snap_rows = conn.execute(
        """
        SELECT id, CAST(taken_at AS TEXT) AS taken_at
        FROM snapshots
        WHERE status IN ('ok', 'partial')
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    snapshots = [dict(r) for r in reversed(snap_rows)]
    snap_ids = [s["id"] for s in snapshots]

    if not snap_ids:
        return {"snapshots": [], "rows": []}

    placeholders = ",".join("?" for _ in snap_ids)
    cells = conn.execute(
        f"""
        SELECT h.ss58 AS hotkey,
               p.name,
               ns.snapshot_id,
               ns.emission,
               ns.is_registered
        FROM hotkeys h
        JOIN persons p ON p.id = h.person_id
        LEFT JOIN neuron_snapshots ns
               ON ns.hotkey_ss58 = h.ss58
              AND ns.snapshot_id IN ({placeholders})
        """,
        snap_ids,
    ).fetchall()

    by_hk: dict[str, dict] = {}
    for c in cells:
        bucket = by_hk.setdefault(
            c["hotkey"], {"name": c["name"], "cells_map": {}}
        )
        if c["snapshot_id"] is not None:
            bucket["cells_map"][c["snapshot_id"]] = {
                "emission": c["emission"],
                "is_registered": c["is_registered"],
            }

    rows = []
    for hotkey, data in by_hk.items():
        rows.append(
            {
                "hotkey": hotkey,
                "name": data["name"],
                "cells": [data["cells_map"].get(sid) for sid in snap_ids],
            }
        )
    rows.sort(key=lambda r: (r["name"], r["hotkey"]))
    return {"snapshots": snapshots, "rows": rows}


def snapshot_history(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Per-snapshot run quality stats, newest first.

    For each snapshot:
        id, taken_at, status, block_number
        team_size:    current hotkey count (same for all rows in a response)
        total_rows:   neuron_snapshots rows actually written for this snapshot
        registered:   how many of those have is_registered=1
        deregistered: how many of those have is_registered=0
        failed:       team_size - total_rows
                      (hotkeys that should have been fetched but no row exists,
                      typically per-hotkey API failures during the run)

    Note: team_size is captured at query time, not at snapshot time. If the
    roster grew/shrank, historical rows may show a small skew in "failed"
    for snapshots taken with a different team size — acceptable for v1.
    """
    team_size = conn.execute("SELECT COUNT(*) AS n FROM hotkeys").fetchone()["n"]
    cursor = conn.execute(
        """
        SELECT
            s.id,
            CAST(s.taken_at AS TEXT)                                  AS taken_at,
            s.status,
            s.block_number,
            COUNT(ns.hotkey_ss58)                                     AS total_rows,
            COALESCE(SUM(CASE WHEN ns.is_registered = 1 THEN 1 ELSE 0 END), 0) AS registered,
            COALESCE(SUM(CASE WHEN ns.is_registered = 0 THEN 1 ELSE 0 END), 0) AS deregistered
        FROM snapshots s
        LEFT JOIN neuron_snapshots ns ON ns.snapshot_id = s.id
        GROUP BY s.id, s.taken_at, s.status, s.block_number
        ORDER BY s.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = []
    for r in cursor.fetchall():
        d = dict(r)
        d["team_size"] = team_size
        d["failed"] = max(0, team_size - d["total_rows"])
        rows.append(d)
    return rows


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, CAST(taken_at AS TEXT) AS taken_at, block_number, status "
        "FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def dashboard_hotkey_summary(
    conn: sqlite3.Connection,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    """Per-hotkey summary for the dashboard.

    Each row:
        hotkey:        ss58 address
        name:          person owning the hotkey
        cumulative:    SUM(emission) in [from_dt, to_dt) over ok/partial snapshots
        is_registered: 1 if registered in the latest successful snapshot, else 0
                       (0 also when the hotkey has no row in the latest snapshot at all)
        last_refresh:  ISO timestamp string of the most recent ok/partial snapshot
                       where this hotkey appeared; None if never seen

    Ordered by cumulative DESC, name ASC, hotkey ASC.
    """
    # 1. Cumulative per hotkey in range
    range_rows = conn.execute(
        """
        SELECT h.ss58 AS hotkey,
               p.name,
               COALESCE(SUM(ns.emission), 0) AS cumulative
        FROM hotkeys h
        JOIN persons p ON p.id = h.person_id
        LEFT JOIN (
            neuron_snapshots ns
            JOIN snapshots s ON s.id = ns.snapshot_id
                             AND s.status IN ('ok', 'partial')
                             AND s.taken_at >= ?
                             AND s.taken_at <  ?
        ) ON ns.hotkey_ss58 = h.ss58
        GROUP BY h.ss58, p.name
        ORDER BY cumulative DESC, p.name ASC, h.ss58 ASC
        """,
        (from_dt, to_dt),
    ).fetchall()

    rows = [dict(r) for r in range_rows]

    # 2. Latest snapshot id for status lookup
    latest = conn.execute(
        "SELECT id FROM snapshots WHERE status IN ('ok', 'partial') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_id = latest["id"] if latest else None

    status_map: dict[str, int] = {}
    if latest_id is not None:
        status_rows = conn.execute(
            "SELECT hotkey_ss58, is_registered FROM neuron_snapshots "
            "WHERE snapshot_id = ?",
            (latest_id,),
        ).fetchall()
        status_map = {r["hotkey_ss58"]: r["is_registered"] for r in status_rows}

    # 3. Last refresh time per hotkey
    refresh_rows = conn.execute(
        """
        SELECT ns.hotkey_ss58, MAX(CAST(s.taken_at AS TEXT)) AS last_refresh
        FROM neuron_snapshots ns
        JOIN snapshots s ON s.id = ns.snapshot_id
        WHERE s.status IN ('ok', 'partial')
        GROUP BY ns.hotkey_ss58
        """
    ).fetchall()
    refresh_map = {r["hotkey_ss58"]: r["last_refresh"] for r in refresh_rows}

    for r in rows:
        r["is_registered"] = int(status_map.get(r["hotkey"], 0))
        r["last_refresh"] = refresh_map.get(r["hotkey"])

    return rows


def current_registration_status(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-person registration status from the latest successful snapshot.

    Returns a mapping name -> {
        "active":               int  # hotkeys registered in latest snapshot
        "total":                int  # total hotkeys for this person
        "deregistered_hotkeys": list[str]  # ss58 of currently-deregistered hotkeys
    }

    If a hotkey has no row in the latest snapshot at all (e.g. worker error),
    it's counted as "deregistered" to surface the visibility gap. Persons with
    no snapshots at all get total=N, active=0.
    """
    latest_id_row = conn.execute(
        "SELECT id FROM snapshots WHERE status IN ('ok', 'partial') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_id = latest_id_row["id"] if latest_id_row else None

    cursor = conn.execute(
        """
        SELECT p.name, h.ss58, ns.is_registered
        FROM persons p
        JOIN hotkeys h ON h.person_id = p.id
        LEFT JOIN neuron_snapshots ns
               ON ns.hotkey_ss58 = h.ss58
              AND ns.snapshot_id = ?
        ORDER BY p.name, h.ss58
        """,
        (latest_id,),
    )

    result: dict[str, dict] = {}
    for row in cursor.fetchall():
        name = row["name"]
        bucket = result.setdefault(
            name, {"active": 0, "total": 0, "deregistered_hotkeys": []}
        )
        bucket["total"] += 1
        if row["is_registered"] == 1:
            bucket["active"] += 1
        else:
            bucket["deregistered_hotkeys"].append(row["ss58"])
    return result
