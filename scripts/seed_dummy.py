"""Populate data/dev.db with realistic dummy data for local UI testing.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config.dev.yaml"
DB_PATH = REPO_ROOT / "data" / "dev.db"

random.seed(56)


def main() -> None:
    config = AppConfig.load(yaml_path=CONFIG, env_path=REPO_ROOT / ".env")

    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"removed existing {DB_PATH}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    sync_team(conn, config.team, subnet_id=config.subnet_id)

    hotkeys = [(p.name, hk) for p in config.team for hk in p.hotkeys]
    print(f"seeded {len(config.team)} persons / {len(hotkeys)} hotkeys")

    # 30 snapshots across the last 36 hours, 72-min apart
    now = datetime.now(timezone.utc)
    snapshot_specs = []
    for i in range(30):
        taken_at = now - timedelta(minutes=72 * (29 - i))
        block_number = 8_200_000 + 360 * i
        if i == 7:
            status = "failed"
        elif i in (12, 22):
            status = "partial"
        else:
            status = "ok"
        snapshot_specs.append((taken_at, block_number, status))

    snapshot_ids: list[int] = []
    for taken_at, block_number, status in snapshot_specs:
        cur = conn.execute(
            "INSERT INTO snapshots (taken_at, block_number, status) VALUES (?, ?, ?)",
            (taken_at, block_number, status),
        )
        snapshot_ids.append(cur.lastrowid)

    # Per-person emission profile (RAO). Alice top earner, Bob mid, Carol low,
    # Dave inactive (mostly 0). Dave HK1 deregisters in the recent tail.
    person_rate_rao = {
        "Alice": 800_000_000,
        "Bob":   400_000_000,
        "Carol": 100_000_000,
        "Dave":   10_000_000,
    }

    dave_dereg_hotkey = next(hk for name, hk in hotkeys if name == "Dave")
    dave_dereg_from_snapshot_index = 25

    rows_written = 0
    for i, sid in enumerate(snapshot_ids):
        status = snapshot_specs[i][2]
        if status == "failed":
            continue
        for uid, (person, hk) in enumerate(hotkeys, start=1):
            if status == "partial" and random.random() < 0.15:
                continue
            if hk == dave_dereg_hotkey and i >= dave_dereg_from_snapshot_index:
                conn.execute(
                    "INSERT INTO neuron_snapshots (snapshot_id, hotkey_ss58, uid, emission, is_registered) "
                    "VALUES (?, ?, NULL, NULL, 0)",
                    (sid, hk),
                )
                rows_written += 1
                continue

            base = person_rate_rao[person]
            jitter = random.randint(-base // 5, base // 5)
            emission = max(0, base + jitter)
            conn.execute(
                "INSERT INTO neuron_snapshots (snapshot_id, hotkey_ss58, uid, emission, is_registered) "
                "VALUES (?, ?, ?, ?, 1)",
                (sid, hk, uid + i * 100, emission),
            )
            rows_written += 1

    print(f"wrote {rows_written} neuron_snapshots rows across {len(snapshot_ids)} snapshots")

    # Create one historical settlement covering the first 12 snapshots so the
    # Archive page has something to show out of the gate.
    boundary_snapshot_id = snapshot_ids[11]
    boundary_taken_at = snapshot_specs[11][0]
    cells = conn.execute(
        """
        SELECT h.ss58 AS hotkey, p.name AS person_name,
               COALESCE(SUM(ns.emission), 0) AS cumulative
        FROM hotkeys h
        JOIN persons p ON p.id = h.person_id
        LEFT JOIN (
            neuron_snapshots ns
            JOIN snapshots s ON s.id = ns.snapshot_id
                             AND s.status IN ('ok', 'partial')
                             AND s.id <= ?
        ) ON ns.hotkey_ss58 = h.ss58
        GROUP BY h.ss58, p.name
        """,
        (boundary_snapshot_id,),
    ).fetchall()
    total = sum(int(r["cumulative"]) for r in cells)
    cur = conn.execute(
        "INSERT INTO settlements "
        "(settled_at, settled_through_snapshot_id, note, total_cumulative_rao) "
        "VALUES (?, ?, ?, ?)",
        (
            boundary_taken_at + timedelta(minutes=5),
            boundary_snapshot_id,
            "Week 1 dummy payout",
            total,
        ),
    )
    sid = cur.lastrowid
    for r in cells:
        conn.execute(
            "INSERT INTO settlement_lines "
            "(settlement_id, hotkey_ss58, person_name, cumulative_rao) "
            "VALUES (?, ?, ?, ?)",
            (sid, r["hotkey"], r["person_name"], int(r["cumulative"])),
        )
    print(
        f"created dummy settlement #{sid} through snapshot "
        f"#{boundary_snapshot_id} ({total / 1e9:.4f} alpha frozen)"
    )

    conn.close()
    print()
    print(f"done. DB at {DB_PATH}")
    print()
    print("Run dev server:")
    print("  EMISSION_CONFIG_PATH=config.dev.yaml EMISSION_DEV_USER=admin \\")
    print("    .venv/bin/uvicorn emission_tracker.main:create_app --factory --port 8001")


if __name__ == "__main__":
    main()
