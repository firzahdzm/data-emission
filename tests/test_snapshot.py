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
        None,  # HK2 first attempt empty
        None,  # HK2 confirmation still empty -> genuine deregistration
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


def test_transient_empty_is_confirmed_not_deregistered(seeded_db: sqlite3.Connection):
    """A one-off empty response must NOT be recorded as a deregistration if the
    confirmation fetch shows the hotkey is still registered."""
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),  # HK1 ok
        None,                                          # HK2 transient empty
        NeuronInfo(uid=11, emission=0.4, block=100),  # HK2 confirmation -> still here
    ]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert result.status == "ok"
    assert result.ok_count == 2
    assert result.deregistered_count == 0
    assert result.fail_count == 0

    row = seeded_db.execute(
        "SELECT is_registered, emission FROM neuron_snapshots WHERE hotkey_ss58 = ?",
        (HK2,),
    ).fetchone()
    assert row["is_registered"] == 1
    assert row["emission"] == 0.4


def test_confirm_fetch_exception_counts_as_fail(seeded_db: sqlite3.Connection):
    """If the confirmation fetch raises, treat as a fetch failure (partial),
    not a deregistration."""
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),  # HK1 ok
        None,                                          # HK2 first empty
        RuntimeError("API down on confirm"),           # HK2 confirmation errors
        RuntimeError("retry also down"),               # HK2 retry pass also errors
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
    assert result.deregistered_count == 0

    row = seeded_db.execute(
        "SELECT * FROM neuron_snapshots WHERE hotkey_ss58 = ?", (HK2,)
    ).fetchone()
    assert row is None  # no phantom row written


def test_take_snapshot_partial_on_one_failure(seeded_db: sqlite3.Connection):
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),
        RuntimeError("API down"),       # HK2 fails (pass 1)
        RuntimeError("still down"),      # HK2 retry also fails
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


def test_failed_hotkey_is_retried_and_recovers(seeded_db: sqlite3.Connection):
    """A hotkey that errors on the first pass is retried; if the retry
    succeeds the snapshot ends 'ok' with no failures recorded."""
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),  # HK1 ok (pass 1)
        RuntimeError("transient"),                     # HK2 fails (pass 1)
        NeuronInfo(uid=11, emission=0.4, block=100),  # HK2 retry succeeds (pass 2)
    ]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert result.status == "ok"
    assert result.ok_count == 2
    assert result.fail_count == 0

    row = seeded_db.execute(
        "SELECT is_registered, emission FROM neuron_snapshots WHERE hotkey_ss58 = ?",
        (HK2,),
    ).fetchone()
    assert row["is_registered"] == 1
    assert row["emission"] == 0.4


def test_failed_hotkey_retry_still_fails_stays_partial(seeded_db: sqlite3.Connection):
    """If the retry also errors, the hotkey stays counted as a failure."""
    client = MagicMock()
    client.get_neuron.side_effect = [
        NeuronInfo(uid=10, emission=0.5, block=100),  # HK1 ok
        RuntimeError("down"),                          # HK2 fails (pass 1)
        RuntimeError("still down"),                    # HK2 retry fails (pass 2)
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

    row = seeded_db.execute(
        "SELECT * FROM neuron_snapshots WHERE hotkey_ss58 = ?", (HK2,)
    ).fetchone()
    assert row is None  # nothing written for the still-failing hotkey


def test_take_snapshot_failed_on_all_failure(seeded_db: sqlite3.Connection):
    client = MagicMock()
    # Both hotkeys fail on pass 1 and again on the retry pass.
    client.get_neuron.side_effect = [
        RuntimeError("x"), RuntimeError("y"),  # pass 1
        RuntimeError("x"), RuntimeError("y"),  # retry pass
    ]
    result = take_snapshot(
        conn=seeded_db,
        client=client,
        rate_limiter=_no_op_bucket(),
        subnet_id=56,
        request_interval_seconds=0,
    )
    assert result.status == "failed"
    assert result.fail_count == 2
