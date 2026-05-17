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
