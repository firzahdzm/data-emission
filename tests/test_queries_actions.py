"""The audit row for a signed action.

These rows are the only durable record that money moved. What they fail
to record cannot be reconstructed later — the chain knows, but nobody
reading this dashboard does.
"""

import sqlite3

import pytest

from emission_tracker.db import init_schema
from emission_tracker.web import queries

PARENT = "5HERhLCKSpmTiRD6EpnsY7DUnVqUThaANhYgXYAWqZZ28fLB"
MEMBER = "5GxjPJokWhd8sZ7kecxQ9JWiqX8vV8R6Hg29SVNPcs6mu8YL"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    init_schema(c)
    yield c
    c.close()


def test_a_distribution_records_who_received_the_money(conn):
    """Every distribution is signed by the treasury, so the signing
    coldkey identifies nobody. Without the counterparty the history
    cannot answer the one question anyone will ask of it."""
    queries.record_action(
        conn, PARENT, "distribute", [], 2_000_000_000, "admin",
        counterparty=MEMBER,
    )
    row = queries.recent_actions(conn)[0]
    assert row["coldkey_ss58"] == PARENT
    assert row["counterparty_ss58"] == MEMBER


def test_a_sweep_records_where_the_money_went(conn):
    queries.record_action(
        conn, MEMBER, "sweep", [], 1_985_000_000, "admin", counterparty=PARENT,
    )
    assert queries.recent_actions(conn)[0]["counterparty_ss58"] == PARENT


def test_actions_with_no_counterparty_still_work(conn):
    """Unstaking moves stake into the wallet's own free balance; there
    is no other side to name."""
    queries.record_action(conn, MEMBER, "unstake_all", [], 0, "admin")
    assert queries.recent_actions(conn)[0]["counterparty_ss58"] is None


def test_an_older_database_gains_the_column(conn):
    """Deployment is `git pull` + restart against a live database, so a
    column that only exists in a fresh schema exists nowhere that
    matters."""
    conn.execute("ALTER TABLE signed_actions DROP COLUMN counterparty_ss58")
    init_schema(conn)          # the migration runs again on startup
    queries.record_action(
        conn, PARENT, "distribute", [], 1, "admin", counterparty=MEMBER,
    )
    assert queries.recent_actions(conn)[0]["counterparty_ss58"] == MEMBER
