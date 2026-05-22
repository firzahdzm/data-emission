"""Populate data/dev.db with realistic dummy data to exercise ALL features.

The seed mirrors what production would look like after several weeks of
operation, so every page has meaningful content:

    /              dashboard      ← open period accumulating since last settle
    /captures      Emissions      ← wide-format α grid across 80 snapshots
    /history       Snapshots      ← run statuses incl. failed/partial
    /archive       Periods        ← 2 closed settlements with 30/70 split
    /kas           Fund           ← 1 distribution done, balance still positive

Re-run any time — it wipes `data/dev.db` (and its WAL/SHM siblings) first.

Usage:
    .venv/bin/python scripts/seed_dummy.py

Then start the dev server:
    EMISSION_CONFIG_PATH=config.dev.yaml EMISSION_DEV_USER=admin \\
      .venv/bin/uvicorn emission_tracker.main:create_app --factory --port 8001
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from emission_tracker.config import AppConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.queries import create_kas_distribution, create_settlement

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config.dev.yaml"
DB_PATH = REPO_ROOT / "data" / "dev.db"

random.seed(56)

# ---- Tunables ---------------------------------------------------------------

N_SNAPSHOTS = 80         # total snapshots seeded (oldest → newest)
INTERVAL_MIN = 72        # 1 tempo apart (matches production polling)

# Snapshots [0, PERIOD_BOUNDARIES[0]) → settlement #1 (Week 1)
# Snapshots [PERIOD_BOUNDARIES[0], PERIOD_BOUNDARIES[1]) → settlement #2 (Week 2)
# Snapshots [PERIOD_BOUNDARIES[1], N_SNAPSHOTS) → open period (dashboard)
PERIOD_BOUNDARIES = (30, 60)
TOKEN_PRICES_USD = (4.50, 5.25)
PERIOD_NOTES = ("Week 1", "Week 2")

KAS_DIST_AMOUNT_USD = 200.0
KAS_DIST_NOTE = "First payout"

# Per-person emission profile (RAO per snapshot, jittered ±20%)
PERSON_RATE_RAO = {
    "Alice": 800_000_000,   # 0.8 α/snapshot — top earner
    "Bob":   400_000_000,   # 0.4 α — mid tier
    "Carol": 100_000_000,   # 0.1 α — low tier
    "Dave":   10_000_000,   # 0.01 α — barely emitting
}

# Snapshot statuses by index
FAILED_INDEXES = {7}
PARTIAL_INDEXES = {18, 45, 67}

# Dave's first hotkey deregisters from this snapshot index onwards
DAVE_DEREG_FROM_INDEX = 65


# ---- Helpers ----------------------------------------------------------------

def _wipe_db() -> None:
    """Remove dev.db + its WAL/SHM siblings if they exist."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
            print(f"  removed {p.name}")


def _insert_snapshot_range(
    conn: sqlite3.Connection,
    snapshot_times: list[datetime],
    hotkeys: list[tuple[str, str]],
    start_idx: int,
    end_idx: int,
) -> int:
    """Insert snapshots in [start_idx, end_idx) + their neuron_snapshots.

    Returns: count of neuron_snapshots rows written.
    """
    dave_h1 = next((hk for name, hk in hotkeys if name == "Dave"), None)
    rows_written = 0

    for i in range(start_idx, end_idx):
        taken_at = snapshot_times[i]
        block = 8_200_000 + 360 * i
        if i in FAILED_INDEXES:
            status = "failed"
        elif i in PARTIAL_INDEXES:
            status = "partial"
        else:
            status = "ok"

        cur = conn.execute(
            "INSERT INTO snapshots (taken_at, block_number, status) VALUES (?, ?, ?)",
            (taken_at, block, status),
        )
        sid = cur.lastrowid

        if status == "failed":
            continue  # failed snapshots have no neuron rows

        for uid, (person, hk) in enumerate(hotkeys, start=1):
            # Drop ~15% of rows for partial snapshots to simulate API misses
            if status == "partial" and random.random() < 0.15:
                continue
            # Dave's first hotkey is deregistered from snapshot 65+
            if hk == dave_h1 and i >= DAVE_DEREG_FROM_INDEX:
                conn.execute(
                    "INSERT INTO neuron_snapshots "
                    "(snapshot_id, hotkey_ss58, uid, emission, is_registered) "
                    "VALUES (?, ?, NULL, NULL, 0)",
                    (sid, hk),
                )
                rows_written += 1
                continue

            base = PERSON_RATE_RAO[person]
            jitter = random.randint(-base // 5, base // 5)
            emission = max(0, base + jitter)
            conn.execute(
                "INSERT INTO neuron_snapshots "
                "(snapshot_id, hotkey_ss58, uid, emission, is_registered) "
                "VALUES (?, ?, ?, ?, 1)",
                (sid, hk, uid + i * 100, emission),
            )
            rows_written += 1

    conn.commit()
    return rows_written


# ---- Main -------------------------------------------------------------------

def main() -> None:
    config = AppConfig.load(yaml_path=CONFIG, env_path=REPO_ROOT / ".env")

    print(f"Wiping {DB_PATH} (and WAL/SHM)…")
    _wipe_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    sync_team(conn, config.team, subnet_id=config.subnet_id)

    hotkeys = [(p.name, hk) for p in config.team for hk in p.hotkeys]
    print(f"Team:  {len(config.team)} persons · {len(hotkeys)} hotkeys")

    # Build the time axis: 80 snapshots ending "now", 72 min apart.
    now = datetime.now(timezone.utc)
    snapshot_times = [
        now - timedelta(minutes=INTERVAL_MIN * (N_SNAPSHOTS - 1 - i))
        for i in range(N_SNAPSHOTS)
    ]
    span_days = (snapshot_times[-1] - snapshot_times[0]).total_seconds() / 86400
    print(f"Range: {snapshot_times[0]:%Y-%m-%d %H:%M} → "
          f"{snapshot_times[-1]:%Y-%m-%d %H:%M} ({span_days:.1f} days)")
    print()

    # ── Phase 1: Week 1 ────────────────────────────────────────────────────
    print("Phase 1: Week 1 snapshots …")
    n = _insert_snapshot_range(conn, snapshot_times, hotkeys, 0, PERIOD_BOUNDARIES[0])
    print(f"  → wrote {n} neuron_snapshots rows over {PERIOD_BOUNDARIES[0]} snapshots")

    s1 = create_settlement(
        conn, token_price_usd=TOKEN_PRICES_USD[0], note=PERIOD_NOTES[0]
    )
    # Backdate settled_at to right after the period's last snapshot
    boundary_ts = snapshot_times[PERIOD_BOUNDARIES[0] - 1] + timedelta(minutes=5)
    conn.execute(
        "UPDATE settlements SET settled_at = ? WHERE id = ?",
        (boundary_ts, s1["id"]),
    )
    conn.commit()
    print(f"  → settlement #{s1['id']} @ ${TOKEN_PRICES_USD[0]:.2f}/α: "
          f"reward ${s1['total_personal_reward_usd']:,.2f} + "
          f"kas ${s1['total_kas_contribution_usd']:,.2f}")

    # ── Phase 2: Week 2 ────────────────────────────────────────────────────
    print()
    print("Phase 2: Week 2 snapshots …")
    n = _insert_snapshot_range(
        conn, snapshot_times, hotkeys, PERIOD_BOUNDARIES[0], PERIOD_BOUNDARIES[1]
    )
    print(f"  → wrote {n} neuron_snapshots rows over "
          f"{PERIOD_BOUNDARIES[1] - PERIOD_BOUNDARIES[0]} snapshots")

    s2 = create_settlement(
        conn, token_price_usd=TOKEN_PRICES_USD[1], note=PERIOD_NOTES[1]
    )
    boundary_ts = snapshot_times[PERIOD_BOUNDARIES[1] - 1] + timedelta(minutes=5)
    conn.execute(
        "UPDATE settlements SET settled_at = ? WHERE id = ?",
        (boundary_ts, s2["id"]),
    )
    conn.commit()
    print(f"  → settlement #{s2['id']} @ ${TOKEN_PRICES_USD[1]:.2f}/α: "
          f"reward ${s2['total_personal_reward_usd']:,.2f} + "
          f"kas ${s2['total_kas_contribution_usd']:,.2f}")

    # ── Phase 3: Kas distribution (partial) ────────────────────────────────
    print()
    print("Phase 3: Kas distribution …")
    dist = create_kas_distribution(
        conn, amount_usd=KAS_DIST_AMOUNT_USD, note=KAS_DIST_NOTE
    )
    dist_ts = boundary_ts + timedelta(hours=2)
    conn.execute(
        "UPDATE kas_distributions SET distributed_at = ? WHERE id = ?",
        (dist_ts, dist["id"]),
    )
    conn.commit()
    print(f"  → distribution #{dist['id']}: ${KAS_DIST_AMOUNT_USD:,.2f} split "
          f"across {len(dist['lines'])} persons")

    # ── Phase 4: Open period (unsettled snapshots) ─────────────────────────
    print()
    print("Phase 4: Open-period snapshots (unsettled — dashboard shows these) …")
    n = _insert_snapshot_range(
        conn, snapshot_times, hotkeys, PERIOD_BOUNDARIES[1], N_SNAPSHOTS
    )
    print(f"  → wrote {n} neuron_snapshots rows over "
          f"{N_SNAPSHOTS - PERIOD_BOUNDARIES[1]} open snapshots "
          f"(incl. Dave's HK1 deregistered from idx {DAVE_DEREG_FROM_INDEX})")

    # ── Summary ────────────────────────────────────────────────────────────
    print()
    print("───── Summary ──────────────────────────────────────────────────────")
    total_snaps = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    total_neurons = conn.execute("SELECT COUNT(*) FROM neuron_snapshots").fetchone()[0]
    total_settles = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    total_dists = conn.execute("SELECT COUNT(*) FROM kas_distributions").fetchone()[0]
    kas_contributed = conn.execute(
        "SELECT COALESCE(SUM(kas_contribution_idr), 0) FROM settlement_lines"
    ).fetchone()[0]
    kas_distributed = conn.execute(
        "SELECT COALESCE(SUM(amount_idr), 0) FROM kas_distributions"
    ).fetchone()[0]
    kas_balance = kas_contributed - kas_distributed

    print(f"  snapshots         : {total_snaps}  "
          f"(1 failed, {len(PARTIAL_INDEXES)} partial, rest ok)")
    print(f"  neuron rows       : {total_neurons}")
    print(f"  settlements       : {total_settles}")
    print(f"  kas distributions : {total_dists}")
    print(f"  kas contributed   : ${kas_contributed:,.2f}")
    print(f"  kas distributed   : ${kas_distributed:,.2f}")
    print(f"  kas balance       : ${kas_balance:,.2f}  ← still distributable")
    print()
    print(f"DB ready: {DB_PATH}")
    print()
    print("Run dev server:")
    print("  EMISSION_CONFIG_PATH=config.dev.yaml EMISSION_DEV_USER=admin \\")
    print("    .venv/bin/uvicorn emission_tracker.main:create_app --factory --port 8001")
    print()
    print("Then open http://127.0.0.1:8001 and try:")
    print("  • Dashboard       : accumulated open-period emission per hotkey")
    print(f"  • Emissions       : per-snapshot α grid ({total_snaps} columns)")
    print("  • Snapshots       : ok/partial/failed run quality")
    print(f"  • Periods         : {total_settles} closed settlements (click to inspect)")
    print(f"  • Fund            : ${kas_balance:,.2f} remaining, "
          f"{total_dists} distribution done")
    print()
    print("Admin actions to test (as EMISSION_DEV_USER=admin):")
    print("  • Close period   (Dashboard → button)        → freezes open period #3")
    print("  • Delete settle  (Periods → #1 or #2)        → reopens that period")
    print("  • Distribute kas (Fund → button)             → splits remaining balance")
    print("  • Delete dist    (Fund → distribution row)   → returns amount to balance")

    conn.close()


if __name__ == "__main__":
    main()
