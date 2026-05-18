import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import TaoStatsClient

log = logging.getLogger(__name__)


@dataclass
class SnapshotResult:
    snapshot_id: int
    status: str  # 'ok' | 'partial' | 'failed'
    ok_count: int
    deregistered_count: int
    fail_count: int


def take_snapshot(
    conn: sqlite3.Connection,
    client: TaoStatsClient,
    rate_limiter: TokenBucket,
    subnet_id: int,
    request_interval_seconds: float,
) -> SnapshotResult:
    snapshot_id = _insert_snapshot_row(conn)
    hotkeys = [
        row["ss58"]
        for row in conn.execute(
            "SELECT ss58 FROM hotkeys WHERE subnet_id = ? ORDER BY ROWID",
            (subnet_id,),
        ).fetchall()
    ]
    ok = deregistered = fail = 0
    last_block: int | None = None

    for i, hk in enumerate(hotkeys):
        if i > 0 and request_interval_seconds > 0:
            time.sleep(request_interval_seconds)
        rate_limiter.acquire()
        try:
            info = client.get_neuron(subnet_id=subnet_id, hotkey=hk)
        except Exception as exc:
            log.warning("hotkey=%s fetch failed: %s", hk, exc)
            fail += 1
            continue
        if info is None:
            conn.execute(
                "INSERT INTO neuron_snapshots (snapshot_id, hotkey_ss58, uid, emission, is_registered) "
                "VALUES (?, ?, NULL, NULL, 0)",
                (snapshot_id, hk),
            )
            deregistered += 1
        else:
            conn.execute(
                "INSERT INTO neuron_snapshots (snapshot_id, hotkey_ss58, uid, emission, is_registered) "
                "VALUES (?, ?, ?, ?, 1)",
                (snapshot_id, hk, info.uid, info.emission),
            )
            ok += 1
            if info.block is not None:
                last_block = info.block
        # Commit per-hotkey so the write lock isn't held for the full ~5.5min
        # loop — other writers (e.g. admin clicking Close Period) can proceed.
        conn.commit()

    total = len(hotkeys)
    if fail == 0:
        status = "ok"
    elif fail < total:
        status = "partial"
    else:
        status = "failed"

    conn.execute(
        "UPDATE snapshots SET status = ?, block_number = ? WHERE id = ?",
        (status, last_block, snapshot_id),
    )
    conn.commit()
    log.info(
        "snapshot #%d %s — %d ok, %d deregistered, %d fail",
        snapshot_id, status, ok, deregistered, fail,
    )
    return SnapshotResult(
        snapshot_id=snapshot_id,
        status=status,
        ok_count=ok,
        deregistered_count=deregistered,
        fail_count=fail,
    )


def _insert_snapshot_row(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO snapshots (taken_at, status) VALUES (?, 'in_progress')",
        (datetime.now(timezone.utc),),
    )
    conn.commit()
    return cursor.lastrowid
