import sqlite3
from contextlib import contextmanager
from pathlib import Path

from emission_tracker.config import PersonConfig

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
    "CREATE INDEX IF NOT EXISTS idx_neuron_snap_hotkey ON neuron_snapshots(hotkey_ss58)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_taken_at ON snapshots(taken_at DESC)",
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
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()


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
