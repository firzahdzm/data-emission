import sqlite3
from unittest.mock import MagicMock

import pytest

from emission_tracker.bot.snapshot import SnapshotResult, take_snapshot
from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import NeuronInfo


HK1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"


@pytest.fixture
def seeded_db(memory_db: sqlite3.Connection) -> sqlite3.Connection:
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="Alice", hotkeys=[HK1, HK2])],
        subnet_id=56,
    )
    return memory_db


def _no_op_bucket() -> TokenBucket:
    # capacity high enough that acquire never blocks during tests
    return TokenBucket(capacity=1000, refill_per_second=1000)


def test_take_snapshot_records_ok_status(seeded_db: sqlite3.Connection):
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),
        NeuronInfo(uid=11, emission=0.4, block=100),
    ]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert isinstance(result, SnapshotResult)
    assert result.status == "ok"
    assert result.ok_count == 2
    assert result.fail_count == 0
    assert result.deregistered_count == 0

    rows = seeded_db.execute(
        "SELECT hotkey_ss58, emission, is_registered FROM neuron_snapshots ORDER BY hotkey_ss58"
    ).fetchall()
    assert len(rows) == 2
    assert {r["hotkey_ss58"]: r["emission"] for r in rows} == {HK1: 0.5, HK2: 0.4}


def test_take_snapshot_records_deregistered(seeded_db: sqlite3.Connection):
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),
        None,  # second hotkey deregistered
    ]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert result.status == "ok"  # deregistered != failed
    assert result.ok_count == 1
    assert result.deregistered_count == 1

    row = seeded_db.execute(
        "SELECT is_registered, emission FROM neuron_snapshots WHERE hotkey_ss58 = ?",
        (HK2,),
    ).fetchone()
    assert row["is_registered"] == 0
    assert row["emission"] is None


def test_take_snapshot_partial_on_one_failure(seeded_db: sqlite3.Connection):
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),
        RuntimeError("API down"),
    ]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert result.status == "partial"
    assert result.ok_count == 1
    assert result.fail_count == 1


def test_take_snapshot_failed_on_all_failure(seeded_db: sqlite3.Connection):
    client = MagicMock()
    client.get_neuron.side_effect = [RuntimeError("x"), RuntimeError("y")]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert result.status == "failed"
    assert result.fail_count == 2
