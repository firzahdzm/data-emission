import sqlite3
from datetime import datetime, timezone


def last_settlement_snapshot_id(conn: sqlite3.Connection) -> int:
    """Boundary snapshot id of the most recent settlement (0 if none).
    Dashboard queries use ``WHERE snapshot_id > this`` to filter to the
    current (post-settle) period only."""
    row = conn.execute(
        "SELECT settled_through_snapshot_id FROM settlements "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["settled_through_snapshot_id"] if row else 0


_SETTLEMENT_COLS = (
    "id, CAST(settled_at AS TEXT) AS settled_at, "
    "settled_through_snapshot_id, note, total_cumulative_rao, "
    # Internal DB columns stay named _idr (legacy); aliased to _usd in
    # every SELECT so the rest of the codebase + API + UI deal in USD.
    "total_idr AS total_usd, "
    "base_salary_idr AS base_salary_usd, "
    "token_price_idr AS token_price_usd, "
    # NULL = unpaid; non-NULL = ISO timestamp when admin marked paid.
    "CAST(paid_at AS TEXT) AS paid_at"
)

RAO_PER_ALPHA = 10**9
PERSONAL_REWARD_PCT = 0.30  # of emission_idr per person
KAS_CONTRIBUTION_PCT = 0.70  # complement; explicit for readability


def last_settlement(conn: sqlite3.Connection) -> dict | None:
    """Most recent settlement row (None if no settlement has happened yet)."""
    row = conn.execute(
        f"SELECT {_SETTLEMENT_COLS} FROM settlements ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def list_settlements(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Recent settlements, newest first."""
    cursor = conn.execute(
        f"SELECT {_SETTLEMENT_COLS} FROM settlements ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cursor.fetchall()]


def settlement_detail(conn: sqlite3.Connection, settlement_id: int) -> dict | None:
    """One settlement plus its per-hotkey lines + payout totals.

    Distribution semantics (v0.4+):
        reward_idr (per line)          = 30% × (cum_alpha × token_price_idr)
        kas_contribution_idr (per line) = 70% × (cum_alpha × token_price_idr)
        total_personal_reward_idr      = SUM(reward_idr) across lines
        total_kas_contribution_idr     = SUM(kas_contribution_idr) across lines

    For backward compat, the older `total_idr` / `base_salary_idr` /
    legacy `kas_bersama_idr` fields are still returned but will be None
    when token_price_idr is set (the new field). Templates should branch
    on `token_price_idr is not None` for the new layout.
    """
    head = conn.execute(
        f"SELECT {_SETTLEMENT_COLS} FROM settlements WHERE id = ?",
        (settlement_id,),
    ).fetchone()
    if head is None:
        return None
    lines = conn.execute(
        "SELECT hotkey_ss58, person_name, cumulative_rao, "
        "       personal_share_idr AS personal_share_usd, "
        "       reward_idr         AS reward_usd, "
        "       kas_contribution_idr AS kas_contribution_usd "
        "FROM settlement_lines WHERE settlement_id = ? "
        "ORDER BY reward_idr DESC, cumulative_rao DESC, person_name, hotkey_ss58",
        (settlement_id,),
    ).fetchall()
    result = {**dict(head), "lines": [dict(line) for line in lines]}

    result["total_personal_reward_usd"] = sum(line["reward_usd"] for line in result["lines"])
    result["total_kas_contribution_usd"] = sum(line["kas_contribution_usd"] for line in result["lines"])
    return result


PERSONAL_SHARE_PCT = 0.30  # 30% of total_idr allocated to performers by emission share


def create_settlement(
    conn: sqlite3.Connection,
    token_price_usd: float,
    note: str | None = None,
) -> dict:
    """Freeze per-hotkey cumulative emission AND compute payout distribution
    in one atomic step.

    Per line (computed once and frozen, never editable):
        emission_usd         = cum_rao × token_price_usd / 1e9
        reward_usd  (30%)    = round(emission_usd × 0.30, 2)
        kas_contribution     = round(emission_usd − reward_usd, 2)

    Raises ValueError if there are no new completed snapshots to settle,
    or if token_price_usd is negative.
    """
    if token_price_usd < 0:
        raise ValueError("token_price_usd must be non-negative")
    last_id = last_settlement_snapshot_id(conn)
    latest = conn.execute(
        "SELECT MAX(id) AS id FROM snapshots "
        "WHERE status IN ('ok', 'partial') AND id > ?",
        (last_id,),
    ).fetchone()
    settled_through = latest["id"] if latest else None
    if settled_through is None:
        raise ValueError("No new completed snapshots since last settlement")

    rows = conn.execute(
        """
        SELECT h.ss58 AS hotkey,
               p.name AS person_name,
               COALESCE(SUM(ns.emission), 0) AS cumulative
        FROM hotkeys h
        JOIN persons p ON p.id = h.person_id
        LEFT JOIN (
            neuron_snapshots ns
            JOIN snapshots s ON s.id = ns.snapshot_id
                             AND s.status IN ('ok', 'partial')
                             AND s.id >  ?
                             AND s.id <= ?
        ) ON ns.hotkey_ss58 = h.ss58
        GROUP BY h.ss58, p.name
        """,
        (last_id, settled_through),
    ).fetchall()

    rows_int = [
        {
            "hotkey": r["hotkey"],
            "person_name": r["person_name"],
            "cumulative": int(r["cumulative"]),
        }
        for r in rows
    ]
    total_cum = sum(r["cumulative"] for r in rows_int)

    now = datetime.now(timezone.utc)
    cursor = conn.execute(
        "INSERT INTO settlements "
        "(settled_at, settled_through_snapshot_id, note, total_cumulative_rao, "
        " token_price_idr) "
        "VALUES (?, ?, ?, ?, ?)",
        (now, settled_through, note, total_cum, token_price_usd),
    )
    settlement_id = cursor.lastrowid

    for r in rows_int:
        emission_usd = r["cumulative"] * token_price_usd / RAO_PER_ALPHA
        line_reward = round(emission_usd * PERSONAL_REWARD_PCT, 2)
        line_kas = round(emission_usd - line_reward, 2)
        conn.execute(
            "INSERT INTO settlement_lines "
            "(settlement_id, hotkey_ss58, person_name, cumulative_rao, "
            " reward_idr, kas_contribution_idr) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                settlement_id,
                r["hotkey"],
                r["person_name"],
                r["cumulative"],
                line_reward,
                line_kas,
            ),
        )
    conn.commit()
    return settlement_detail(conn, settlement_id)


# ---- Kas Bersama queries ----

def all_time_contributions(conn: sqlite3.Connection) -> list[dict]:
    """Per-person all-time emission summed across every settlement_lines row.

    Returns list of {name, cumulative_rao} ordered by cumulative desc.
    Used to weight kas bersama distribution shares.
    """
    rows = conn.execute(
        """
        SELECT person_name AS name,
               COALESCE(SUM(cumulative_rao), 0) AS cumulative_rao
        FROM settlement_lines
        GROUP BY person_name
        ORDER BY cumulative_rao DESC, person_name ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def kas_totals(conn: sqlite3.Connection) -> dict:
    """Running balance of kas bersama (in USD, float).

        contributed = SUM(kas_contribution) across all settlement_lines
        distributed = SUM(amount) across all kas_distributions
        balance     = contributed - distributed
    """
    contributed = conn.execute(
        "SELECT COALESCE(SUM(kas_contribution_idr), 0) AS n FROM settlement_lines"
    ).fetchone()["n"]
    distributed = conn.execute(
        "SELECT COALESCE(SUM(amount_idr), 0) AS n FROM kas_distributions"
    ).fetchone()["n"]
    return {
        "contributed": float(contributed),
        "distributed": float(distributed),
        "balance": round(float(contributed) - float(distributed), 2),
    }


def preview_kas_distribution(conn: sqlite3.Connection, amount_usd: float) -> list[dict]:
    """Preview how `amount_usd` would split across all-time contributors.

    Returns list of {name, all_time_emission_rao, share_usd} ordered by share desc.
    Sum of share_usd equals amount_usd exactly (last person gets the rounding remainder).
    """
    if amount_usd < 0:
        raise ValueError("amount_usd must be non-negative")
    contribs = all_time_contributions(conn)
    total_emission = sum(c["cumulative_rao"] for c in contribs)
    if total_emission == 0 or not contribs:
        return [
            {"name": c["name"], "all_time_emission_rao": c["cumulative_rao"], "share_usd": 0.0}
            for c in contribs
        ]
    result = []
    running_total = 0.0
    for i, c in enumerate(contribs):
        if i == len(contribs) - 1:
            share = round(amount_usd - running_total, 2)
        else:
            share = round(c["cumulative_rao"] / total_emission * amount_usd, 2)
            running_total += share
        result.append(
            {
                "name": c["name"],
                "all_time_emission_rao": c["cumulative_rao"],
                "share_usd": share,
            }
        )
    return result


def create_kas_distribution(
    conn: sqlite3.Connection,
    amount_usd: float,
    note: str | None = None,
) -> dict:
    """Atomically: snapshot per-person shares (by all-time emission) and
    insert a kas_distributions header + lines.

    Raises ValueError if amount_usd is negative, or if balance is insufficient.
    """
    if amount_usd < 0:
        raise ValueError("amount_usd must be non-negative")
    totals = kas_totals(conn)
    if amount_usd > totals["balance"]:
        raise ValueError(
            f"Insufficient kas balance: requested {amount_usd}, available {totals['balance']}"
        )
    shares = preview_kas_distribution(conn, amount_usd)
    now = datetime.now(timezone.utc)
    cursor = conn.execute(
        "INSERT INTO kas_distributions (distributed_at, amount_idr, note) "
        "VALUES (?, ?, ?)",
        (now, amount_usd, note),
    )
    distribution_id = cursor.lastrowid
    for s in shares:
        conn.execute(
            "INSERT INTO kas_distribution_lines "
            "(distribution_id, person_name, all_time_emission_rao, share_idr) "
            "VALUES (?, ?, ?, ?)",
            (distribution_id, s["name"], s["all_time_emission_rao"], s["share_usd"]),
        )
    conn.commit()
    return kas_distribution_detail(conn, distribution_id)


def delete_kas_distribution(conn: sqlite3.Connection, distribution_id: int) -> bool:
    """Delete a kas distribution + cascade its lines. The balance is
    automatically reopened (since balance = contributed - distributed).
    """
    cursor = conn.execute(
        "DELETE FROM kas_distributions WHERE id = ?", (distribution_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def list_kas_distributions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    cursor = conn.execute(
        "SELECT id, CAST(distributed_at AS TEXT) AS distributed_at, "
        "       amount_idr AS amount_usd, note "
        "FROM kas_distributions ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cursor.fetchall()]


def kas_distribution_detail(conn: sqlite3.Connection, distribution_id: int) -> dict | None:
    head = conn.execute(
        "SELECT id, CAST(distributed_at AS TEXT) AS distributed_at, "
        "       amount_idr AS amount_usd, note "
        "FROM kas_distributions WHERE id = ?",
        (distribution_id,),
    ).fetchone()
    if head is None:
        return None
    lines = conn.execute(
        "SELECT person_name, all_time_emission_rao, "
        "       share_idr AS share_usd "
        "FROM kas_distribution_lines WHERE distribution_id = ? "
        "ORDER BY share_idr DESC, person_name ASC",
        (distribution_id,),
    ).fetchall()
    return {**dict(head), "lines": [dict(line) for line in lines]}


def delete_settlement(conn: sqlite3.Connection, settlement_id: int) -> bool:
    """Delete a settlement (and cascade its lines). Returns True if a row
    was removed. After deletion, the previous settlement (if any) becomes
    the current boundary."""
    cursor = conn.execute(
        "DELETE FROM settlements WHERE id = ?", (settlement_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def set_settlement_paid(
    conn: sqlite3.Connection,
    settlement_id: int,
    paid: bool,
) -> dict | None:
    """Mark a settlement as paid (paid_at = now) or unpaid (paid_at = NULL).

    This is purely a bookkeeping flag — it does NOT alter the frozen
    reward / kas-contribution amounts, the kas balance, or the period
    boundary. It exists so admins can track which periods have actually
    been disbursed to team members vs which are still pending payout.

    Returns the updated settlement detail dict (same shape as
    `settlement_detail`), or None if the settlement_id does not exist.
    """
    exists = conn.execute(
        "SELECT 1 FROM settlements WHERE id = ?", (settlement_id,)
    ).fetchone()
    if not exists:
        return None
    if paid:
        conn.execute(
            "UPDATE settlements SET paid_at = ? WHERE id = ?",
            (datetime.now(timezone.utc), settlement_id),
        )
    else:
        conn.execute(
            "UPDATE settlements SET paid_at = NULL WHERE id = ?",
            (settlement_id,),
        )
    conn.commit()
    return settlement_detail(conn, settlement_id)


def dashboard_summary(
    conn: sqlite3.Connection,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    """Return per-person cumulative emission in current period (since last
    settlement), filtered by the requested date range, ordered desc."""
    settle_boundary = last_settlement_snapshot_id(conn)
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
                             AND s.id > ?
                             AND s.taken_at >= ?
                             AND s.taken_at <  ?
        ) ON ns.hotkey_ss58 = h.ss58
        GROUP BY p.name
        ORDER BY cumulative DESC, p.name ASC
        """,
        (settle_boundary, from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def hotkey_series(
    conn: sqlite3.Connection,
    hotkey: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    settle_boundary = last_settlement_snapshot_id(conn)
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
          AND s.id > ?
          AND s.taken_at >= ?
          AND s.taken_at <  ?
        ORDER BY s.taken_at
        """,
        (hotkey, settle_boundary, from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def person_series(
    conn: sqlite3.Connection,
    name: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    settle_boundary = last_settlement_snapshot_id(conn)
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
              AND s.id > ?
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
        (name, settle_boundary, from_dt, to_dt),
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
    settle_boundary = last_settlement_snapshot_id(conn)
    snap_rows = conn.execute(
        """
        SELECT id, CAST(taken_at AS TEXT) AS taken_at
        FROM snapshots
        WHERE status IN ('ok', 'partial')
          AND id > ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (settle_boundary, limit),
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
    settle_boundary = last_settlement_snapshot_id(conn)
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
        WHERE s.id > ?
        GROUP BY s.id, s.taken_at, s.status, s.block_number
        ORDER BY s.id DESC
        LIMIT ?
        """,
        (settle_boundary, limit),
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
    settle_boundary = last_settlement_snapshot_id(conn)
    # 1. Cumulative per hotkey in range (and after last settlement)
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
                             AND s.id > ?
                             AND s.taken_at >= ?
                             AND s.taken_at <  ?
        ) ON ns.hotkey_ss58 = h.ss58
        GROUP BY h.ss58, p.name
        ORDER BY cumulative DESC, p.name ASC, h.ss58 ASC
        """,
        (settle_boundary, from_dt, to_dt),
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
