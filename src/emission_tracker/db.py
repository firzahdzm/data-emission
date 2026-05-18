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
        ss58       TEXT PRIMARY KEY,
        person_id  INTEGER NOT NULL REFERENCES persons(id),
        subnet_id  INTEGER NOT NULL
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
        total_cumulative_rao        INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlement_lines (
        settlement_id   INTEGER NOT NULL REFERENCES settlements(id) ON DELETE CASCADE,
        hotkey_ss58     TEXT    NOT NULL,
        person_name     TEXT    NOT NULL,
        cumulative_rao  INTEGER NOT NULL,
        PRIMARY KEY (settlement_id, hotkey_ss58)
    )
    """,
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


def init_schema(conn: sqlite3.Connection) -> None:
    # WAL lets reads run concurrently with the snapshot worker's writes —
    # without this, a 5-minute snapshot loop blocks any other write (e.g.,
    # an admin clicking Close Period during the run) with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
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
        for ss58 in person.hotkeys:
            conn.execute(
                """
                INSERT INTO hotkeys (ss58, person_id, subnet_id)
                VALUES (?, ?, ?)
                ON CONFLICT(ss58) DO UPDATE SET
                    person_id = excluded.person_id,
                    subnet_id = excluded.subnet_id
                """,
                (ss58, person_id, subnet_id),
            )
    conn.commit()
