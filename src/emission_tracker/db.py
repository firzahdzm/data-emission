import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from emission_tracker.config import PersonConfig

# Python 3.12+ deprecated the default datetime ↔ TIMESTAMP adapters; register
# explicit ones at module import. Storage is ISO 8601 with timezone; reads
# return tz-aware datetime objects. Done here so every connection sees them,
# including ones the scheduler builds and the ones tests open via conftest.


def _adapt_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _convert_timestamp(b: bytes) -> datetime:
    return datetime.fromisoformat(b.decode())


sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_timestamp)

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS persons (
        id   INTEGER PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hotkeys (
        ss58          TEXT PRIMARY KEY,
        person_id     INTEGER NOT NULL REFERENCES persons(id),
        subnet_id     INTEGER NOT NULL,
        -- Owning coldkey. Nullable so a wallet can be recorded before its
        -- owner is known; the pre-rotation hotkeys share a single coldkey.
        coldkey_ss58  TEXT,
        -- Operator's name for the wallet ("I", "II", "(old)"); NULL if
        -- unlabelled. This, not the coldkey, marks which rotation era a
        -- wallet belongs to.
        label         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id            INTEGER PRIMARY KEY,
        taken_at      TIMESTAMP NOT NULL,
        block_number  INTEGER,
        status        TEXT NOT NULL CHECK (status IN ('in_progress','ok','partial','failed'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS neuron_snapshots (
        snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        hotkey_ss58    TEXT    NOT NULL REFERENCES hotkeys(ss58),
        uid            INTEGER,
        emission       REAL,
        is_registered  INTEGER NOT NULL CHECK (is_registered IN (0, 1)),
        PRIMARY KEY (snapshot_id, hotkey_ss58)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlements (
        id                          INTEGER PRIMARY KEY,
        settled_at                  TIMESTAMP NOT NULL,
        settled_through_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        note                        TEXT,
        total_cumulative_rao        INTEGER NOT NULL,
        total_idr                   INTEGER,
        base_salary_idr             INTEGER,
        paid_at                     TIMESTAMP  -- NULL = unpaid; set when admin marks paid
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlement_lines (
        settlement_id        INTEGER NOT NULL REFERENCES settlements(id) ON DELETE CASCADE,
        hotkey_ss58          TEXT    NOT NULL,
        person_name          TEXT    NOT NULL,
        cumulative_rao       INTEGER NOT NULL,
        personal_share_idr   INTEGER NOT NULL DEFAULT 0,   -- legacy, unused after v0.4
        reward_idr           INTEGER NOT NULL DEFAULT 0,   -- personal reward (30% × emission_idr)
        kas_contribution_idr INTEGER NOT NULL DEFAULT 0,   -- 70% × emission_idr → kas bersama
        PRIMARY KEY (settlement_id, hotkey_ss58)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kas_distributions (
        id                     INTEGER PRIMARY KEY,
        distributed_at         TIMESTAMP NOT NULL,
        amount_idr             INTEGER NOT NULL,
        note                   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kas_distribution_lines (
        distribution_id     INTEGER NOT NULL REFERENCES kas_distributions(id) ON DELETE CASCADE,
        person_name         TEXT    NOT NULL,
        all_time_emission_rao INTEGER NOT NULL,
        share_idr           INTEGER NOT NULL,
        PRIMARY KEY (distribution_id, person_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS salary_payments (
        id                       INTEGER PRIMARY KEY,
        paid_at                  TIMESTAMP NOT NULL,
        amount_per_person_idr    INTEGER NOT NULL,
        headcount                INTEGER NOT NULL,
        total_idr                INTEGER NOT NULL,
        note                     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS salary_payment_lines (
        payment_id    INTEGER NOT NULL REFERENCES salary_payments(id) ON DELETE CASCADE,
        person_name   TEXT    NOT NULL,
        amount_idr    INTEGER NOT NULL,
        PRIMARY KEY (payment_id, person_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS coldkey_balances (
        coldkey_ss58              TEXT      NOT NULL,
        fetched_at                TIMESTAMP NOT NULL,
        -- Wallet balances from TaoStats, in rao. NULL when that fetch failed,
        -- which is distinct from a real zero.
        balance_free_rao          INTEGER,
        balance_staked_rao        INTEGER,
        balance_total_rao         INTEGER,
        -- Gradients tournament deposit, in rao. NULL when the coldkey has no
        -- tournament account at all (the API 404s) or the fetch failed;
        -- tournament_seen tells those two apart.
        tournament_balance_rao    INTEGER,
        tournament_total_sent_rao INTEGER,
        -- Alpha staked on the tracked subnet only, and its value in TAO.
        -- Deliberately not balance_staked: that spans every subnet, and
        -- several of our coldkeys hold alpha elsewhere.
        stake_alpha_rao           INTEGER,
        stake_alpha_as_tao_rao    INTEGER,
        tournament_seen           INTEGER NOT NULL DEFAULT 0
                                  CHECK (tournament_seen IN (0, 1)),
        PRIMARY KEY (coldkey_ss58, fetched_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signed_actions (
        id            INTEGER PRIMARY KEY,
        coldkey_ss58  TEXT      NOT NULL,
        op            TEXT      NOT NULL,
        types         TEXT,
        amount_rao    INTEGER   NOT NULL DEFAULT 0,
        -- 'pending' is written before the signer is called, so a crash
        -- mid-flight leaves evidence rather than silence.
        status        TEXT      NOT NULL CHECK (status IN ('pending','ok','failed')),
        tx_hash       TEXT,
        error         TEXT,
        requested_by  TEXT      NOT NULL,
        requested_at  TIMESTAMP NOT NULL,
        finished_at   TIMESTAMP,
        -- Recorded as 'failed' so the guards behave, but flagged: btcli
        -- said neither success nor failure, so the money may have moved.
        -- A retry here can pay twice, which is why it must not read as an
        -- ordinary failure in the UI.
        outcome_unknown INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signed_actions_coldkey "
    "ON signed_actions(coldkey_ss58, status)",
    # Enforces at the database level what the pending_action check only
    # verifies optimistically: two concurrent requests for the same
    # coldkey cannot both land a pending row.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_signed_actions_one_pending "
    "ON signed_actions(coldkey_ss58) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_coldkey_bal_fetched ON coldkey_balances(fetched_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_neuron_snap_hotkey ON neuron_snapshots(hotkey_ss58)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_taken_at ON snapshots(taken_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_settlements_through ON settlements(settled_through_snapshot_id)",
]


@contextmanager
def connect(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


MIGRATIONS = [
    # Idempotent ALTER TABLE statements for existing DBs that pre-date a
    # column addition. SQLite errors with "duplicate column name" if the
    # column is already there — we catch and ignore that case.
    "ALTER TABLE settlements ADD COLUMN total_idr INTEGER",
    "ALTER TABLE settlements ADD COLUMN base_salary_idr INTEGER",
    "ALTER TABLE settlements ADD COLUMN token_price_idr INTEGER",
    "ALTER TABLE settlement_lines ADD COLUMN personal_share_idr INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settlement_lines ADD COLUMN reward_idr INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settlement_lines ADD COLUMN kas_contribution_idr INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE settlements ADD COLUMN paid_at TIMESTAMP",
    "ALTER TABLE hotkeys ADD COLUMN coldkey_ss58 TEXT",
    "ALTER TABLE hotkeys ADD COLUMN label TEXT",
    "ALTER TABLE coldkey_balances ADD COLUMN stake_alpha_rao INTEGER",
    "ALTER TABLE coldkey_balances ADD COLUMN stake_alpha_as_tao_rao INTEGER",
    "ALTER TABLE signed_actions ADD COLUMN outcome_unknown INTEGER NOT NULL DEFAULT 0",
]


def init_schema(conn: sqlite3.Connection) -> None:
    # WAL lets reads run concurrently with the snapshot worker's writes —
    # without this, a 5-minute snapshot loop blocks any other write (e.g.,
    # an admin clicking Close Period during the run) with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    conn.commit()


def cleanup_orphaned_snapshots(conn: sqlite3.Connection) -> int:
    """Mark any snapshot still flagged 'in_progress' as 'failed'.

    Called once at app startup. The only legitimate writer is the snapshot
    worker, which transitions in_progress → ok/partial/failed at the end
    of its run. Any in_progress row found at startup therefore belongs to
    a worker that died mid-flight (process killed, OS suspend, etc.) and
    will never be completed. Marking it failed:
      - lets web queries (status IN ('ok','partial')) ignore it correctly
      - preserves the historical fact that an attempt was made and lost
      - makes the captures/dashboard histograms readable

    Returns the number of rows updated, so the caller can log it.
    """
    cursor = conn.execute(
        "UPDATE snapshots SET status = 'failed' WHERE status = 'in_progress'"
    )
    conn.commit()
    return cursor.rowcount


def sync_team(
    conn: sqlite3.Connection,
    team: list[PersonConfig],
    subnet_id: int,
) -> None:
    """Upsert persons and hotkeys from config. Never deletes existing rows."""
    for person in team:
        conn.execute(
            "INSERT INTO persons (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
            (person.name,),
        )
        person_id = conn.execute(
            "SELECT id FROM persons WHERE name = ?",
            (person.name,),
        ).fetchone()["id"]
        for wallet in person.hotkeys:
            conn.execute(
                """
                INSERT INTO hotkeys (ss58, person_id, subnet_id, coldkey_ss58, label)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ss58) DO UPDATE SET
                    person_id    = excluded.person_id,
                    subnet_id    = excluded.subnet_id,
                    coldkey_ss58 = excluded.coldkey_ss58,
                    label        = excluded.label
                """,
                (
                    wallet.hotkey,
                    person_id,
                    subnet_id,
                    wallet.coldkey,
                    wallet.label,
                ),
            )
    conn.commit()


def cleanup_stranded_actions(conn: sqlite3.Connection) -> int:
    """Fail any signed action still 'pending' at startup.

    Mirrors cleanup_orphaned_snapshots, for the same reason and with a
    sharper consequence. A row goes 'pending' before the signer is called
    and is resolved when it answers; if the tracker dies in between — or
    the signer hangs and is killed — nothing will ever resolve it. The
    duplicate-click guard then refuses every future action on that coldkey,
    so one crash silently retires a wallet.

    Marking it failed is the honest record: we know the attempt was made
    and do not know its outcome. Returns the number of rows updated so the
    caller can log it, since a non-zero count means someone should check
    the chain for what actually happened.
    """
    cursor = conn.execute(
        "UPDATE signed_actions SET status = 'failed', finished_at = ?, "
        "error = COALESCE(error, 'interrupted — outcome unknown, check the chain') "
        "WHERE status = 'pending'",
        (datetime.now(timezone.utc),),
    )
    conn.commit()
    return cursor.rowcount
