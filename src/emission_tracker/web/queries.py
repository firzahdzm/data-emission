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


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, CAST(taken_at AS TEXT) AS taken_at, block_number, status "
        "FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


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
