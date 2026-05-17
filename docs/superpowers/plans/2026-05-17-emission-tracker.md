# Gradient Emission Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python bot that polls TaoStats every Bittensor tempo to record emission data for 22 team hotkeys on subnet 56, persists to SQLite, and serves a FastAPI dashboard with per-person accumulation, date-range filter, and deregistration detection.

**Architecture:** Single Python process. FastAPI hosts the web layer and starts an APScheduler `BackgroundScheduler` in its `lifespan` hook. Snapshot worker polls TaoStats one hotkey at a time through a 5-req/min token bucket, writing rows to SQLite. Web routes read SQLite directly via small SQL query functions. Worker only writes; web only reads — they share no state besides the DB file.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, APScheduler, httpx, Pydantic v2 + pydantic-settings, PyYAML, Jinja2, stdlib `sqlite3`. Dev: pytest, respx (httpx mock), freezegun (time mock).

**Spec:** [docs/superpowers/specs/2026-05-17-emission-tracker-design.md](../specs/2026-05-17-emission-tracker-design.md)

---

## Task 0: Pre-flight — TaoStats API Endpoint Verification

This is a **manual verification step** before any code. The spec flagged the exact endpoint/field shape as unknown (Open Items #1–4). Confirm them now so Task 5 codes against real responses.

**Files:** none (creates notes in this file).

- [ ] **Step 1: Obtain TaoStats API key**

Go to <https://taostats.io/pro/>, sign in, click "Create API key". Save the key to a temporary scratchpad. You'll put it in `.env` in Task 1.

- [ ] **Step 2: Discover the correct neuron endpoint**

The current TaoStats API base is `https://api.taostats.io`. Try the neurons endpoint for one known hotkey on subnet 56:

```bash
export TAOSTATS_API_KEY="<paste your key>"

# Try v1 neuron endpoint
curl -s "https://api.taostats.io/api/dtao/neuron/v1?netuid=56&hotkey=5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1" \
  -H "Authorization: $TAOSTATS_API_KEY" | head -c 2000
```

If that 404s, also try:

```bash
curl -s "https://api.taostats.io/api/v1/neuron?netuid=56&hotkey=5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1" \
  -H "Authorization: $TAOSTATS_API_KEY" | head -c 2000

curl -s "https://api.taostats.io/api/neuron/latest/v1?netuid=56&hotkey=5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1" \
  -H "Authorization: $TAOSTATS_API_KEY" | head -c 2000
```

If none work, browse <https://docs.taostats.io/reference> in a browser and find the neuron-by-hotkey endpoint. Use that URL.

- [ ] **Step 3: Record the verified contract in this plan**

Edit this step in the plan to fill in the actual values you confirmed. **These values feed Task 5.** Replace the placeholders below with verified info:

```yaml
# VERIFIED ENDPOINT CONTRACT (fill in after Step 2):
base_url: https://api.taostats.io
auth_header: Authorization        # or "x-api-key" — whichever the docs say
neuron_path: /api/dtao/neuron/v1  # exact path that returned data
query_params:
  netuid: int                     # subnet id
  hotkey: str                     # ss58
not_registered_signal: ...        # what does the API return for a hotkey not on the subnet?
                                  # e.g. "404 status", or "200 with data: []", or "200 with empty object"
emission_field_path: data.emission # JSON path to the emission value in the response
emission_unit: ...                # is it TAO/day, alpha/tempo, etc?
block_field_path: data.block_number  # may be null
uid_field_path: data.uid
```

- [ ] **Step 4: Verify emission field semantics**

Take 2 readings ~5 minutes apart for the same hotkey. If the value is identical, it's likely a *rate* (per-day/per-block). If it changes, it could still be a rate that drifts. To be sure:

- Check the docs at <https://docs.taostats.io/reference> for the field description.
- If emission is a rate (e.g., TAO/day), the accumulation math in the spec (Section 3.2) needs adjustment — flag this and discuss with user before proceeding to Task 6.

If accumulation semantics need a different field/endpoint, update this plan: change `emission_field_path` above and re-confirm with user.

- [ ] **Step 5: Commit verification notes**

```bash
git add docs/superpowers/plans/2026-05-17-emission-tracker.md
git commit -m "docs: verify TaoStats API contract for emission tracker"
```

---

## Task 1: Project Scaffolding

Set up directory structure, dependency manifest, and ignore rules. No tests yet — pure setup.

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config.example.yaml`
- Create: `src/emission_tracker/__init__.py`
- Create: `src/emission_tracker/bot/__init__.py`
- Create: `src/emission_tracker/web/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "emission-tracker"
version = "0.1.0"
description = "Gradient subnet (56) emission tracker"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "httpx>=0.27",
    "apscheduler>=3.10",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "pyyaml>=6.0",
    "jinja2>=3.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.20",
    "freezegun>=1.4",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.env
data/
.pytest_cache/
.coverage
*.egg-info/
build/
dist/
```

- [ ] **Step 3: Create `.env.example`**

```bash
TAOSTATS_API_KEY=tao-replace-with-your-key
LOG_LEVEL=INFO
```

- [ ] **Step 4: Create `config.example.yaml`**

```yaml
subnet_id: 56

polling:
  interval_minutes: 72
  request_interval_seconds: 15
  run_on_startup: true

database:
  path: data/emissions.db

web:
  host: 127.0.0.1
  port: 8000

team:
  - name: ExamplePerson
    hotkeys:
      - 5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB1
      - 5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2
```

- [ ] **Step 5: Create empty package init files**

```bash
mkdir -p src/emission_tracker/bot src/emission_tracker/web/templates src/emission_tracker/web/static tests data
touch src/emission_tracker/__init__.py
touch src/emission_tracker/bot/__init__.py
touch src/emission_tracker/web/__init__.py
touch tests/__init__.py
```

- [ ] **Step 6: Create `tests/conftest.py` with shared fixtures**

```python
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def memory_db():
    """In-memory SQLite connection — mirrors production: foreign keys + decltype parsing."""
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"
```

- [ ] **Step 7: Create virtualenv and install**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Expected: install completes; `pip show emission-tracker` shows the package.

- [ ] **Step 8: Verify pytest discovers no tests yet**

Run: `pytest -v`
Expected: `no tests ran in X.XXs`.

- [ ] **Step 9: Commit**

```bash
git init
git add .
git commit -m "chore: scaffold project structure and dependencies"
```

---

## Task 2: Config Model + Validation (Pydantic)

Define the data model for `config.yaml` + `.env`. Validate SS58 format, unique names, and no duplicate hotkeys across people. Loading from files happens in Task 3.

**Files:**
- Create: `src/emission_tracker/config.py`
- Create: `tests/test_config_model.py`

- [ ] **Step 1: Write failing test — SS58 format validation**

`tests/test_config_model.py`:

```python
import pytest
from pydantic import ValidationError

from emission_tracker.config import PersonConfig


def test_person_accepts_valid_ss58():
    person = PersonConfig(
        name="Alice",
        hotkeys=[
            "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
            "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
        ],
    )
    assert person.name == "Alice"
    assert len(person.hotkeys) == 2


def test_person_rejects_non_ss58_hotkey():
    with pytest.raises(ValidationError):
        PersonConfig(name="X", hotkeys=["not-an-ss58"])


def test_person_rejects_hotkey_not_starting_with_5():
    with pytest.raises(ValidationError):
        PersonConfig(
            name="X",
            hotkeys=["7GpcTKW7Mjbz82xwzQUWY8ze9UNtdWmZrSWLBrfRwZpDuF7h"],
        )
```

- [ ] **Step 2: Run test — confirm fail**

Run: `pytest tests/test_config_model.py -v`
Expected: ImportError / collection failure (`emission_tracker.config` doesn't exist).

- [ ] **Step 3: Create `src/emission_tracker/config.py` with `PersonConfig`**

```python
import re

from pydantic import BaseModel, Field, field_validator

SS58_REGEX = re.compile(r"^5[1-9A-HJ-NP-Za-km-z]{46,47}$")


class PersonConfig(BaseModel):
    name: str = Field(min_length=1)
    hotkeys: list[str] = Field(min_length=1)

    @field_validator("hotkeys")
    @classmethod
    def validate_ss58(cls, v: list[str]) -> list[str]:
        for hk in v:
            if not SS58_REGEX.match(hk):
                raise ValueError(f"Invalid SS58 address: {hk!r}")
        return v
```

- [ ] **Step 4: Run test — confirm pass**

Run: `pytest tests/test_config_model.py -v`
Expected: 3 passed.

- [ ] **Step 5: Write failing tests for `PollingConfig`, `DatabaseConfig`, `WebConfig` defaults**

Append to `tests/test_config_model.py`:

```python
from emission_tracker.config import (
    DatabaseConfig,
    PollingConfig,
    WebConfig,
)


def test_polling_defaults():
    cfg = PollingConfig()
    assert cfg.interval_minutes == 72
    assert cfg.request_interval_seconds == 15
    assert cfg.run_on_startup is True


def test_polling_rejects_request_interval_too_fast():
    # 5 req/min = 12s minimum; require at least 12s
    with pytest.raises(ValidationError):
        PollingConfig(request_interval_seconds=5)


def test_database_requires_path():
    with pytest.raises(ValidationError):
        DatabaseConfig()


def test_web_defaults():
    cfg = WebConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
```

- [ ] **Step 6: Run tests — confirm 3 fail (PollingConfig etc. not defined)**

Run: `pytest tests/test_config_model.py -v`
Expected: ImportError on the new imports.

- [ ] **Step 7: Add `PollingConfig`, `DatabaseConfig`, `WebConfig` to `config.py`**

Append to `src/emission_tracker/config.py`:

```python
class PollingConfig(BaseModel):
    interval_minutes: int = Field(default=72, gt=0)
    request_interval_seconds: int = Field(default=15, ge=12)
    run_on_startup: bool = True


class DatabaseConfig(BaseModel):
    path: str = Field(min_length=1)


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)
```

- [ ] **Step 8: Run tests — confirm pass**

Run: `pytest tests/test_config_model.py -v`
Expected: 7 passed.

- [ ] **Step 9: Write failing tests for `AppConfig` cross-team validation**

Append to `tests/test_config_model.py`:

```python
from emission_tracker.config import AppConfig


def _valid_app_kwargs(**overrides):
    base = dict(
        subnet_id=56,
        polling=PollingConfig(),
        database=DatabaseConfig(path="data/test.db"),
        web=WebConfig(),
        team=[
            PersonConfig(
                name="A",
                hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
            ),
            PersonConfig(
                name="B",
                hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"],
            ),
        ],
        taostats_api_key="tao-test-key",
    )
    base.update(overrides)
    return base


def test_app_config_accepts_valid_team():
    cfg = AppConfig(**_valid_app_kwargs())
    assert len(cfg.team) == 2


def test_app_config_rejects_duplicate_name():
    team = [
        PersonConfig(
            name="Same",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
        ),
        PersonConfig(
            name="Same",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"],
        ),
    ]
    with pytest.raises(ValidationError, match="Duplicate person name"):
        AppConfig(**_valid_app_kwargs(team=team))


def test_app_config_rejects_duplicate_hotkey_across_persons():
    shared = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
    team = [
        PersonConfig(name="A", hotkeys=[shared]),
        PersonConfig(name="B", hotkeys=[shared]),
    ]
    with pytest.raises(ValidationError, match="Duplicate hotkey"):
        AppConfig(**_valid_app_kwargs(team=team))
```

- [ ] **Step 10: Run tests — confirm 3 fail (AppConfig not defined)**

Run: `pytest tests/test_config_model.py -v`
Expected: ImportError on AppConfig.

- [ ] **Step 11: Add `AppConfig` to `config.py`**

Append to `src/emission_tracker/config.py`:

```python
from pydantic import SecretStr, model_validator


class AppConfig(BaseModel):
    subnet_id: int = Field(gt=0)
    polling: PollingConfig
    database: DatabaseConfig
    web: WebConfig
    team: list[PersonConfig] = Field(min_length=1)
    taostats_api_key: SecretStr

    @model_validator(mode="after")
    def _validate_unique_names_and_hotkeys(self) -> "AppConfig":
        names = [p.name for p in self.team]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate person name in team: {names}")

        all_hotkeys: list[str] = []
        for p in self.team:
            all_hotkeys.extend(p.hotkeys)
        if len(all_hotkeys) != len(set(all_hotkeys)):
            raise ValueError("Duplicate hotkey across persons")
        return self
```

- [ ] **Step 12: Run all config tests — confirm pass**

Run: `pytest tests/test_config_model.py -v`
Expected: 10 passed.

- [ ] **Step 13: Commit**

```bash
git add src/emission_tracker/config.py tests/test_config_model.py
git commit -m "feat(config): add Pydantic models with SS58 + cross-team validation"
```

---

## Task 3: Config Loader (YAML + env merge)

Add a class method `AppConfig.load(yaml_path, env_path)` that reads `config.yaml`, reads `.env` for the API key, and constructs the validated model. Fail-fast on missing/invalid input.

**Files:**
- Modify: `src/emission_tracker/config.py`
- Create: `tests/test_config_loader.py`

- [ ] **Step 1: Write failing test — loads from YAML + env**

`tests/test_config_loader.py`:

```python
from pathlib import Path

import pytest

from emission_tracker.config import AppConfig


YAML_CONTENT = """
subnet_id: 56
polling:
  interval_minutes: 72
  request_interval_seconds: 15
  run_on_startup: true
database:
  path: data/test.db
web:
  host: 127.0.0.1
  port: 8000
team:
  - name: Alice
    hotkeys:
      - 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1
      - 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2
"""

ENV_CONTENT = "TAOSTATS_API_KEY=tao-test-key\n"


def test_load_reads_yaml_and_env(tmp_path: Path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(YAML_CONTENT)
    env_path = tmp_path / ".env"
    env_path.write_text(ENV_CONTENT)

    cfg = AppConfig.load(yaml_path=yaml_path, env_path=env_path)

    assert cfg.subnet_id == 56
    assert cfg.team[0].name == "Alice"
    assert cfg.taostats_api_key.get_secret_value() == "tao-test-key"


def test_load_fails_when_api_key_missing(tmp_path: Path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(YAML_CONTENT)
    env_path = tmp_path / ".env"
    env_path.write_text("# empty\n")

    with pytest.raises(ValueError, match="TAOSTATS_API_KEY"):
        AppConfig.load(yaml_path=yaml_path, env_path=env_path)


def test_load_fails_when_yaml_missing(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(ENV_CONTENT)

    with pytest.raises(FileNotFoundError):
        AppConfig.load(yaml_path=tmp_path / "missing.yaml", env_path=env_path)
```

- [ ] **Step 2: Run test — confirm fail**

Run: `pytest tests/test_config_loader.py -v`
Expected: `AttributeError: type object 'AppConfig' has no attribute 'load'`.

- [ ] **Step 3: Add `AppConfig.load` classmethod + `_read_env_key` helper to `config.py`**

Add these imports at the top of `src/emission_tracker/config.py` (after existing imports):

```python
import os
from pathlib import Path

import yaml
```

Add this method inside the existing `AppConfig` class (after `_validate_unique_names_and_hotkeys`):

```python
    @classmethod
    def load(cls, yaml_path: Path, env_path: Path | None = None) -> "AppConfig":
        yaml_path = Path(yaml_path)
        raw = yaml.safe_load(yaml_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{yaml_path} must contain a YAML mapping")

        api_key = _read_env_key(env_path, "TAOSTATS_API_KEY")
        if not api_key:
            raise ValueError(
                "TAOSTATS_API_KEY is missing. Set it in .env or environment."
            )
        raw["taostats_api_key"] = api_key
        return cls(**raw)
```

Add this module-level helper at the bottom of `config.py`:

```python
def _read_env_key(env_path: Path | None, key: str) -> str | None:
    if env_path is not None and Path(env_path).exists():
        for line in Path(env_path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return os.environ.get(key)
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest tests/test_config_loader.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run all config tests**

Run: `pytest tests/test_config_model.py tests/test_config_loader.py -v`
Expected: 13 passed.

- [ ] **Step 6: Commit**

```bash
git add src/emission_tracker/config.py tests/test_config_loader.py
git commit -m "feat(config): add YAML + env loader to AppConfig"
```

---

## Task 4: Database Module — Schema + Team Sync

Schema init (idempotent `CREATE TABLE IF NOT EXISTS`), connection helper, and `sync_team` that upserts `persons` and `hotkeys` from config.

**Files:**
- Create: `src/emission_tracker/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing test — schema creates all tables**

`tests/test_db.py`:

```python
import sqlite3

import pytest

from emission_tracker.config import DatabaseConfig, PersonConfig
from emission_tracker.db import connect, init_schema, sync_team


def test_init_schema_creates_all_tables(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    cursor = memory_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in cursor.fetchall()]
    assert tables == ["hotkeys", "neuron_snapshots", "persons", "snapshots"]


def test_init_schema_is_idempotent(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    init_schema(memory_db)  # second call must not raise


def test_connect_enables_foreign_keys(tmp_db_path):
    with connect(str(tmp_db_path)) as conn:
        cursor = conn.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1
```

- [ ] **Step 2: Run test — confirm fail**

Run: `pytest tests/test_db.py -v`
Expected: ImportError on `emission_tracker.db`.

- [ ] **Step 3: Create `src/emission_tracker/db.py` with `connect` and `init_schema`**

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path

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
        status        TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS neuron_snapshots (
        snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
        hotkey_ss58    TEXT    NOT NULL REFERENCES hotkeys(ss58),
        uid            INTEGER,
        emission       REAL,
        is_registered  INTEGER NOT NULL,
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
```

- [ ] **Step 4: Run tests — schema tests pass; `sync_team` test fails**

Run: `pytest tests/test_db.py -v`
Expected: 3 pass, 0 fail (we haven't written sync_team test yet).

- [ ] **Step 5: Write failing test for `sync_team`**

Append to `tests/test_db.py`:

```python
def test_sync_team_upserts_persons_and_hotkeys(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    team = [
        PersonConfig(
            name="Alice",
            hotkeys=[
                "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
                "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
            ],
        ),
    ]
    sync_team(memory_db, team, subnet_id=56)

    persons = list(memory_db.execute("SELECT name FROM persons"))
    assert [p["name"] for p in persons] == ["Alice"]

    hotkeys = list(
        memory_db.execute(
            "SELECT ss58, subnet_id FROM hotkeys ORDER BY ss58"
        )
    )
    assert {h["ss58"] for h in hotkeys} == {
        "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
        "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
    }
    assert all(h["subnet_id"] == 56 for h in hotkeys)


def test_sync_team_is_idempotent(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    team = [
        PersonConfig(
            name="Alice",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
        ),
    ]
    sync_team(memory_db, team, subnet_id=56)
    sync_team(memory_db, team, subnet_id=56)
    count = memory_db.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
    assert count == 1


def test_sync_team_preserves_removed_hotkeys(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(
            name="Alice",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
        )],
        subnet_id=56,
    )
    # second sync with no hotkeys for Alice
    sync_team(
        memory_db,
        [PersonConfig(
            name="Alice",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"],
        )],
        subnet_id=56,
    )
    rows = memory_db.execute("SELECT ss58 FROM hotkeys ORDER BY ss58").fetchall()
    assert len(rows) == 2  # both old and new preserved
```

- [ ] **Step 6: Run tests — confirm 3 fail**

Run: `pytest tests/test_db.py -v`
Expected: ImportError on `sync_team`.

- [ ] **Step 7: Implement `sync_team` in `db.py`**

Append to `src/emission_tracker/db.py`:

```python
from emission_tracker.config import PersonConfig


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
```

- [ ] **Step 8: Run tests — confirm pass**

Run: `pytest tests/test_db.py -v`
Expected: 6 passed.

- [ ] **Step 9: Commit**

```bash
git add src/emission_tracker/db.py tests/test_db.py
git commit -m "feat(db): add schema init and idempotent team sync"
```

---

## Task 5: Rate Limiter (Token Bucket)

5 tokens, refills at 5/min. `acquire()` blocks until a token is available. Sync implementation using `time.monotonic()` so we can freeze time in tests.

**Files:**
- Create: `src/emission_tracker/rate_limiter.py`
- Create: `tests/test_rate_limiter.py`

- [ ] **Step 1: Write failing tests**

`tests/test_rate_limiter.py`:

```python
import time

import pytest
from freezegun import freeze_time

from emission_tracker.rate_limiter import TokenBucket


def test_initial_capacity_allows_immediate_acquires():
    bucket = TokenBucket(capacity=5, refill_per_second=5 / 60)
    for _ in range(5):
        bucket.acquire()  # must not block


def test_acquire_blocks_when_empty(monkeypatch):
    """When bucket is empty, acquire sleeps until token refills."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    with freeze_time("2026-01-01 12:00:00") as frozen:
        bucket = TokenBucket(capacity=2, refill_per_second=1.0)
        bucket.acquire()
        bucket.acquire()  # bucket now empty

        bucket.acquire()  # must request sleep for ~1s
        assert len(sleeps) == 1
        assert 0.9 <= sleeps[0] <= 1.1


def test_refill_caps_at_capacity():
    with freeze_time("2026-01-01 12:00:00") as frozen:
        bucket = TokenBucket(capacity=3, refill_per_second=10.0)
        bucket.acquire()
        bucket.acquire()
        bucket.acquire()
        frozen.tick(60)  # 60 seconds elapse
        # bucket should be capped at 3, not 600
        for _ in range(3):
            bucket.acquire()  # must not block
```

- [ ] **Step 2: Run test — confirm fail (ImportError)**

Run: `pytest tests/test_rate_limiter.py -v`
Expected: ImportError on `emission_tracker.rate_limiter`.

- [ ] **Step 3: Implement `TokenBucket`**

`src/emission_tracker/rate_limiter.py`:

```python
import time
from threading import Lock


class TokenBucket:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, capacity: int, refill_per_second: float):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        self._capacity = capacity
        self._refill_rate = refill_per_second
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._refill_rate,
        )
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                missing = 1.0 - self._tokens
                wait = missing / self._refill_rate
            time.sleep(wait)
```

Note: `time.monotonic()` is mocked by freezegun via `freeze_time` only if we patch it. We need to make the tests use `time.time()` or accept freezegun behavior.

**Fix:** freezegun does freeze `time.monotonic` since v1.2. Verify:

- [ ] **Step 4: Run tests — confirm pass (or adjust)**

Run: `pytest tests/test_rate_limiter.py -v`
Expected: 3 passed.

If `freeze_time` doesn't freeze `time.monotonic`, adjust implementation to use `time.time()` instead — semantically OK for our use case since we don't need monotonic guarantees here, and tests are easier.

- [ ] **Step 5: Commit**

```bash
git add src/emission_tracker/rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add token bucket rate limiter"
```

---

## Task 6: TaoStats API Client

Wrap httpx. One public method: `get_neuron(subnet_id, hotkey) -> NeuronInfo | None`. Handles 404/empty as `None`, retries 5xx/timeout, raises on persistent failure. Uses values verified in Task 0.

**Files:**
- Create: `src/emission_tracker/taostats_client.py`
- Create: `tests/test_taostats_client.py`

> **Important:** before this task, update constants below with values from Task 0 Step 3 (`base_url`, `auth_header`, `neuron_path`, `emission_field_path`, `not_registered_signal`). The example code uses placeholders that match a common TaoStats v1 shape; substitute the real values.

- [ ] **Step 1: Write failing tests**

`tests/test_taostats_client.py`:

```python
import httpx
import pytest
import respx

from emission_tracker.taostats_client import NeuronInfo, TaoStatsClient


# Adjust to match the verified endpoint from Task 0
BASE_URL = "https://api.taostats.io"
NEURON_PATH = "/api/dtao/neuron/v1"
HOTKEY = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"


@respx.mock
def test_get_neuron_returns_info_on_200():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{
                    "uid": 42,
                    "emission": 0.521,
                    "block_number": 5_123_456,
                }]
            },
        )
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info == NeuronInfo(uid=42, emission=0.521, block=5_123_456)


@respx.mock
def test_get_neuron_returns_none_on_empty_data():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    assert client.get_neuron(subnet_id=56, hotkey=HOTKEY) is None


@respx.mock
def test_get_neuron_returns_none_on_404():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    assert client.get_neuron(subnet_id=56, hotkey=HOTKEY) is None


@respx.mock
def test_get_neuron_retries_on_5xx_then_succeeds():
    route = respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"data": [{"uid": 1, "emission": 0.1, "block_number": 1}]}),
        ]
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL, retry_backoff=0)
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info.uid == 1
    assert route.call_count == 3


@respx.mock
def test_get_neuron_raises_after_max_retries():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(503)
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL, retry_backoff=0)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_neuron(subnet_id=56, hotkey=HOTKEY)


@respx.mock
def test_get_neuron_sends_auth_header():
    route = respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = TaoStatsClient(api_key="secret-key", base_url=BASE_URL)
    client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "secret-key"
```

- [ ] **Step 2: Run tests — confirm fail (ImportError)**

Run: `pytest tests/test_taostats_client.py -v`
Expected: ImportError on `emission_tracker.taostats_client`.

- [ ] **Step 3: Implement `TaoStatsClient`**

`src/emission_tracker/taostats_client.py`:

```python
import time
from dataclasses import dataclass

import httpx


# These should match Task 0 verified values
DEFAULT_BASE_URL = "https://api.taostats.io"
NEURON_PATH = "/api/dtao/neuron/v1"
AUTH_HEADER = "Authorization"


@dataclass(frozen=True)
class NeuronInfo:
    uid: int
    emission: float
    block: int | None


class TaoStatsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={AUTH_HEADER: api_key},
        )
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TaoStatsClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get_neuron(self, subnet_id: int, hotkey: str) -> NeuronInfo | None:
        params = {"netuid": subnet_id, "hotkey": hotkey}
        response = self._request_with_retry("GET", NEURON_PATH, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _parse_neuron(response.json())

    def _request_with_retry(self, method: str, path: str, **kw) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.request(method, path, **kw)
                if resp.status_code < 500:
                    return resp
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
            if attempt < self._max_retries:
                time.sleep(self._retry_backoff * (2 ** attempt))
        if last_exc:
            raise last_exc
        resp.raise_for_status()
        return resp  # unreachable; keep type checker happy


def _parse_neuron(payload: dict) -> NeuronInfo | None:
    data = payload.get("data")
    if not data:
        return None
    if isinstance(data, list):
        if not data:
            return None
        item = data[0]
    else:
        item = data
    return NeuronInfo(
        uid=int(item["uid"]),
        emission=float(item["emission"]),
        block=item.get("block_number"),
    )
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest tests/test_taostats_client.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/emission_tracker/taostats_client.py tests/test_taostats_client.py
git commit -m "feat: add TaoStats API client with retry"
```

---

## Task 7: Snapshot Worker

`take_snapshot()` writes one `snapshots` row + one `neuron_snapshots` row per hotkey, computes status (`ok`/`partial`/`failed`), respects rate limiter, and detects deregistration (when client returns `None`).

**Files:**
- Create: `src/emission_tracker/bot/snapshot.py`
- Create: `tests/test_snapshot.py`

- [ ] **Step 1: Write failing test — happy path**

`tests/test_snapshot.py`:

```python
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
```

- [ ] **Step 2: Run test — confirm fail**

Run: `pytest tests/test_snapshot.py -v`
Expected: ImportError on `emission_tracker.bot.snapshot`.

- [ ] **Step 3: Implement minimal `take_snapshot`**

`src/emission_tracker/bot/snapshot.py`:

```python
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
            "SELECT ss58 FROM hotkeys WHERE subnet_id = ? ORDER BY ss58",
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
```

- [ ] **Step 4: Run test — confirm pass**

Run: `pytest tests/test_snapshot.py -v`
Expected: 1 passed.

- [ ] **Step 5: Add failing tests for deregistration and partial failure**

Append to `tests/test_snapshot.py`:

```python
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
```

- [ ] **Step 6: Run tests — confirm pass**

Run: `pytest tests/test_snapshot.py -v`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add src/emission_tracker/bot/snapshot.py tests/test_snapshot.py
git commit -m "feat(bot): add take_snapshot worker with status + deregister tracking"
```

---

## Task 8: Web SQL Queries Module

Pure functions that take a connection + parameters and return dicts/lists. Heart of the dashboard — must be tested with seeded data covering range filter edge cases.

**Files:**
- Create: `src/emission_tracker/web/queries.py`
- Create: `tests/test_queries.py`

- [ ] **Step 1: Write failing tests with seed data**

`tests/test_queries.py`:

```python
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.queries import (
    dashboard_summary,
    hotkey_series,
    latest_snapshot,
    person_series,
)


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
HK_I1 = "5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB1"


@pytest.fixture
def seeded_db(memory_db: sqlite3.Connection) -> sqlite3.Connection:
    init_schema(memory_db)
    sync_team(
        memory_db,
        [
            PersonConfig(name="Alice", hotkeys=[HK_F1, HK_F2]),
            PersonConfig(name="Bob", hotkeys=[HK_I1]),
        ],
        subnet_id=56,
    )
    # 3 snapshots, ascending time
    base = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    for i, dt in enumerate([base, base + timedelta(minutes=72), base + timedelta(minutes=144)]):
        memory_db.execute(
            "INSERT INTO snapshots (id, taken_at, block_number, status) VALUES (?, ?, ?, 'ok')",
            (i + 1, dt, 1000 + i),
        )
        # Alice HK1: 1.0, 2.0, 3.0 ; HK2: 0.5, 0.5, 0.5 ; Bob: 0.1, 0.2, 0.3
        memory_db.execute(
            "INSERT INTO neuron_snapshots VALUES (?, ?, 10, ?, 1)",
            (i + 1, HK_F1, [1.0, 2.0, 3.0][i]),
        )
        memory_db.execute(
            "INSERT INTO neuron_snapshots VALUES (?, ?, 11, ?, 1)",
            (i + 1, HK_F2, 0.5),
        )
        memory_db.execute(
            "INSERT INTO neuron_snapshots VALUES (?, ?, 12, ?, 1)",
            (i + 1, HK_I1, [0.1, 0.2, 0.3][i]),
        )
    memory_db.commit()
    return memory_db


def test_dashboard_summary_all_time(seeded_db: sqlite3.Connection):
    rows = dashboard_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alice"]["cumulative"] == pytest.approx(7.5)  # 1+2+3 + 0.5*3
    assert by_name["Bob"]["cumulative"] == pytest.approx(0.6)  # 0.1+0.2+0.3
    # ordering: highest first
    assert rows[0]["name"] == "Alice"


def test_dashboard_summary_with_range_filter(seeded_db: sqlite3.Connection):
    # Range covering only snapshots 2 and 3
    rows = dashboard_summary(
        seeded_db,
        from_dt=datetime(2026, 5, 17, 13, 0, tzinfo=timezone.utc),
        to_dt=datetime(2026, 5, 17, 15, 0, tzinfo=timezone.utc),
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alice"]["cumulative"] == pytest.approx(6.0)  # 2+3 + 0.5+0.5
    assert by_name["Bob"]["cumulative"] == pytest.approx(0.5)  # 0.2+0.3


def test_dashboard_summary_excludes_failed_snapshots(seeded_db: sqlite3.Connection):
    seeded_db.execute("UPDATE snapshots SET status = 'failed' WHERE id = 1")
    seeded_db.commit()
    rows = dashboard_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alice"]["cumulative"] == pytest.approx(6.0)


def test_hotkey_series_returns_cumulative_running_sum(seeded_db: sqlite3.Connection):
    series = hotkey_series(
        seeded_db,
        hotkey=HK_F1,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    cumulatives = [s["cumulative"] for s in series]
    assert cumulatives == [pytest.approx(1.0), pytest.approx(3.0), pytest.approx(6.0)]


def test_person_series_aggregates_hotkeys(seeded_db: sqlite3.Connection):
    series = person_series(
        seeded_db,
        name="Alice",
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    # at each snapshot, total emission = HK_F1 + HK_F2
    per_snap = [s["per_snapshot_emission"] for s in series]
    assert per_snap == [pytest.approx(1.5), pytest.approx(2.5), pytest.approx(3.5)]
    cum = [s["cumulative"] for s in series]
    assert cum == [pytest.approx(1.5), pytest.approx(4.0), pytest.approx(7.5)]


def test_latest_snapshot_returns_most_recent_ok(seeded_db: sqlite3.Connection):
    snap = latest_snapshot(seeded_db)
    assert snap["id"] == 3
    assert snap["status"] == "ok"
```

- [ ] **Step 2: Run tests — confirm fail (ImportError)**

Run: `pytest tests/test_queries.py -v`
Expected: ImportError on `emission_tracker.web.queries`.

- [ ] **Step 3: Implement `queries.py`**

`src/emission_tracker/web/queries.py`:

```python
import sqlite3
from datetime import datetime


def dashboard_summary(
    conn: sqlite3.Connection,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    """Return per-person cumulative emission in range, ordered desc."""
    cursor = conn.execute(
        """
        SELECT p.name,
               COALESCE(SUM(ns.emission), 0) AS cumulative
        FROM persons p
        LEFT JOIN hotkeys h           ON h.person_id = p.id
        LEFT JOIN neuron_snapshots ns ON ns.hotkey_ss58 = h.ss58
        LEFT JOIN snapshots s         ON s.id = ns.snapshot_id
                                      AND s.status IN ('ok', 'partial')
                                      AND s.taken_at >= ?
                                      AND s.taken_at <  ?
        GROUP BY p.name
        ORDER BY cumulative DESC, p.name ASC
        """,
        (from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def hotkey_series(
    conn: sqlite3.Connection,
    hotkey: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    cursor = conn.execute(
        """
        SELECT s.taken_at,
               ns.emission AS per_snapshot_emission,
               SUM(ns.emission) OVER (
                 PARTITION BY ns.hotkey_ss58
                 ORDER BY s.taken_at
               ) AS cumulative
        FROM neuron_snapshots ns
        JOIN snapshots s ON s.id = ns.snapshot_id
        WHERE ns.hotkey_ss58 = ?
          AND s.status IN ('ok', 'partial')
          AND s.taken_at >= ?
          AND s.taken_at <  ?
        ORDER BY s.taken_at
        """,
        (hotkey, from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def person_series(
    conn: sqlite3.Connection,
    name: str,
    from_dt: datetime,
    to_dt: datetime,
) -> list[dict]:
    cursor = conn.execute(
        """
        WITH per_snap AS (
            SELECT s.id AS snapshot_id,
                   s.taken_at,
                   SUM(COALESCE(ns.emission, 0)) AS per_snapshot_emission
            FROM persons p
            JOIN hotkeys h           ON h.person_id = p.id
            LEFT JOIN neuron_snapshots ns ON ns.hotkey_ss58 = h.ss58
            JOIN snapshots s         ON s.id = ns.snapshot_id
            WHERE p.name = ?
              AND s.status IN ('ok', 'partial')
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
        (name, from_dt, to_dt),
    )
    return [dict(row) for row in cursor.fetchall()]


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT id, taken_at, block_number, status FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest tests/test_queries.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/emission_tracker/web/queries.py tests/test_queries.py
git commit -m "feat(web): add SQL query helpers for dashboard + per-person series"
```

---

## Task 9: Web JSON API Routes

FastAPI router exposing endpoints from spec Section 6.1. Each endpoint is thin: parse query params, call `queries.*`, return JSON.

**Files:**
- Create: `src/emission_tracker/web/range_parse.py`
- Create: `src/emission_tracker/web/routes_api.py`
- Create: `tests/test_range_parse.py`
- Create: `tests/test_routes_api.py`

- [ ] **Step 1: Write failing tests for `parse_range`**

`tests/test_range_parse.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from emission_tracker.web.range_parse import parse_range


def test_parse_range_preset_24h():
    now = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)
    frm, to = parse_range(preset="24h", from_str=None, to_str=None, now=now)
    assert to == now
    assert frm == now - timedelta(hours=24)


def test_parse_range_preset_7d():
    now = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)
    frm, to = parse_range(preset="7d", from_str=None, to_str=None, now=now)
    assert (to - frm) == timedelta(days=7)


def test_parse_range_all_uses_epoch():
    now = datetime(2026, 5, 17, tzinfo=timezone.utc)
    frm, to = parse_range(preset="all", from_str=None, to_str=None, now=now)
    assert frm.year == 1970
    assert to == now


def test_parse_range_custom_dates():
    frm, to = parse_range(
        preset=None,
        from_str="2026-05-10",
        to_str="2026-05-17",
        now=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    assert frm == datetime(2026, 5, 10, tzinfo=timezone.utc)
    assert to == datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_parse_range_default_is_all():
    now = datetime(2026, 5, 17, tzinfo=timezone.utc)
    frm, to = parse_range(preset=None, from_str=None, to_str=None, now=now)
    assert frm.year == 1970


def test_parse_range_rejects_from_after_to():
    with pytest.raises(ValueError):
        parse_range(
            preset=None,
            from_str="2026-05-20",
            to_str="2026-05-10",
            now=datetime(2026, 5, 17, tzinfo=timezone.utc),
        )
```

- [ ] **Step 2: Run test — confirm fail**

Run: `pytest tests/test_range_parse.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `range_parse.py`**

`src/emission_tracker/web/range_parse.py`:

```python
from datetime import datetime, timedelta, timezone


PRESETS_HOURS = {
    "today": None,  # special: midnight UTC to now
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "mtd": None,    # special: first of month to now
    "all": None,    # special: epoch to now
}


def parse_range(
    preset: str | None,
    from_str: str | None,
    to_str: str | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    if from_str or to_str:
        frm = _parse_date(from_str) if from_str else datetime(1970, 1, 1, tzinfo=timezone.utc)
        to = _parse_date(to_str) if to_str else now
        if frm > to:
            raise ValueError(f"from ({frm}) must be <= to ({to})")
        return frm, to

    p = (preset or "all").lower()
    if p == "today":
        frm = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif p == "mtd":
        frm = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif p == "all":
        frm = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        hours = PRESETS_HOURS.get(p)
        if hours is None:
            raise ValueError(f"Unknown preset: {preset!r}")
        frm = now - timedelta(hours=hours)
    return frm, now


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
```

- [ ] **Step 4: Run tests — confirm pass**

Run: `pytest tests/test_range_parse.py -v`
Expected: 6 passed.

- [ ] **Step 5: Write failing tests for API routes**

`tests/test_routes_api.py`:

```python
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.routes_api import router as api_router


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"


@pytest.fixture
def app_with_db(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="Alice", hotkeys=[HK_F1, HK_F2])],
        subnet_id=56,
    )
    base = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    memory_db.execute(
        "INSERT INTO snapshots (id, taken_at, block_number, status) VALUES (1, ?, 1000, 'ok')",
        (base,),
    )
    memory_db.execute(
        "INSERT INTO neuron_snapshots VALUES (1, ?, 10, 1.0, 1)", (HK_F1,)
    )
    memory_db.execute(
        "INSERT INTO neuron_snapshots VALUES (1, ?, 11, 0.5, 1)", (HK_F2,)
    )
    memory_db.commit()

    app = FastAPI()
    app.state.db_conn = memory_db
    app.include_router(api_router, prefix="/api")
    return app


def test_get_persons_returns_dashboard_data(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/persons")
    assert resp.status_code == 200
    data = resp.json()
    assert data["persons"][0]["name"] == "Alice"
    assert data["persons"][0]["cumulative"] == pytest.approx(1.5)


def test_get_snapshots_latest(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/snapshots/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["status"] == "ok"


def test_get_person_series(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/persons/Alice/series")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["series"]) == 1
    assert data["series"][0]["cumulative"] == pytest.approx(1.5)


def test_invalid_range_returns_400(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/persons?from=2026-05-20&to=2026-05-10")
    assert resp.status_code == 400


def test_healthz(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/healthz")
    assert resp.status_code == 200
    assert resp.text.strip('"') == "ok"
```

- [ ] **Step 6: Run tests — confirm fail (ImportError)**

Run: `pytest tests/test_routes_api.py -v`
Expected: ImportError on `emission_tracker.web.routes_api`.

- [ ] **Step 7: Implement `routes_api.py`**

`src/emission_tracker/web/routes_api.py`:

```python
import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request

from emission_tracker.web import queries
from emission_tracker.web.range_parse import parse_range

router = APIRouter()


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db_conn


def _range(preset: str | None, frm: str | None, to: str | None):
    try:
        return parse_range(preset=preset, from_str=frm, to_str=to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/healthz")
def healthz():
    return "ok"


@router.get("/persons")
def get_persons(
    request: Request,
    range: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    from_dt, to_dt = _range(range, from_, to)
    rows = queries.dashboard_summary(_db(request), from_dt=from_dt, to_dt=to_dt)
    return {"persons": rows, "range": {"from": from_dt.isoformat(), "to": to_dt.isoformat()}}


@router.get("/persons/{name}/series")
def get_person_series(
    request: Request,
    name: str,
    range: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    from_dt, to_dt = _range(range, from_, to)
    series = queries.person_series(_db(request), name=name, from_dt=from_dt, to_dt=to_dt)
    return {"name": name, "series": series}


@router.get("/hotkeys/{ss58}/series")
def get_hotkey_series(
    request: Request,
    ss58: str,
    range: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    from_dt, to_dt = _range(range, from_, to)
    series = queries.hotkey_series(_db(request), hotkey=ss58, from_dt=from_dt, to_dt=to_dt)
    return {"hotkey": ss58, "series": series}


@router.get("/snapshots/latest")
def get_latest_snapshot(request: Request):
    snap = queries.latest_snapshot(_db(request))
    if snap is None:
        raise HTTPException(status_code=404, detail="No snapshots yet")
    return snap
```

- [ ] **Step 8: Run tests — confirm pass**

Run: `pytest tests/test_routes_api.py tests/test_range_parse.py -v`
Expected: 11 passed.

- [ ] **Step 9: Commit**

```bash
git add src/emission_tracker/web/range_parse.py src/emission_tracker/web/routes_api.py tests/test_range_parse.py tests/test_routes_api.py
git commit -m "feat(web): add JSON API endpoints with date range filter"
```

---

## Task 10: HTML Pages + Templates

Jinja2 templates for dashboard and per-person page. Pico.css + Chart.js loaded from CDN.

**Files:**
- Create: `src/emission_tracker/web/routes_pages.py`
- Create: `src/emission_tracker/web/templates/base.html`
- Create: `src/emission_tracker/web/templates/dashboard.html`
- Create: `src/emission_tracker/web/templates/person.html`
- Create: `src/emission_tracker/web/static/style.css`
- Create: `tests/test_routes_pages.py`

- [ ] **Step 1: Write failing tests**

`tests/test_routes_pages.py`:

```python
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.routes_pages import register_pages


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"


@pytest.fixture
def app(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="Alice", hotkeys=[HK_F1, HK_F2])],
        subnet_id=56,
    )
    memory_db.execute(
        "INSERT INTO snapshots (id, taken_at, status) VALUES (1, ?, 'ok')",
        (datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),),
    )
    memory_db.execute("INSERT INTO neuron_snapshots VALUES (1, ?, 10, 1.0, 1)", (HK_F1,))
    memory_db.execute("INSERT INTO neuron_snapshots VALUES (1, ?, 11, 0.5, 1)", (HK_F2,))
    memory_db.commit()
    a = FastAPI()
    a.state.db_conn = memory_db
    register_pages(a)
    return a


def test_dashboard_renders_with_person_row(app):
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Alice" in resp.text
    assert "1.5" in resp.text  # cumulative


def test_dashboard_with_range_preset(app):
    client = TestClient(app)
    resp = client.get("/?range=7d")
    assert resp.status_code == 200
    assert "Alice" in resp.text


def test_person_page_renders(app):
    client = TestClient(app)
    resp = client.get("/person/Alice")
    assert resp.status_code == 200
    assert "Alice" in resp.text
    assert HK_F1 in resp.text
```

- [ ] **Step 2: Run tests — confirm fail**

Run: `pytest tests/test_routes_pages.py -v`
Expected: ImportError on `emission_tracker.web.routes_pages`.

- [ ] **Step 3: Create `base.html` template**

`src/emission_tracker/web/templates/base.html`:

```html
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <title>{% block title %}Emission Tracker{% endblock %}</title>
    <meta http-equiv="refresh" content="300">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css">
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>
<main>
    <header>
        <h1>Gradient Emission Tracker</h1>
        {% if latest %}
        <p>Last snapshot: {{ latest.taken_at }} (#{{ latest.id }}, {{ latest.status }})</p>
        {% else %}
        <p>No snapshots yet.</p>
        {% endif %}
    </header>
    {% block content %}{% endblock %}
</main>
</body>
</html>
```

- [ ] **Step 4: Create `dashboard.html` template**

`src/emission_tracker/web/templates/dashboard.html`:

```html
{% extends "base.html" %}
{% block content %}

<section>
    <form method="get" action="/">
        <label>
            Range:
            <select name="range" onchange="this.form.submit()">
                <option value="all" {% if active_range == "all" %}selected{% endif %}>All time</option>
                <option value="today" {% if active_range == "today" %}selected{% endif %}>Today</option>
                <option value="24h" {% if active_range == "24h" %}selected{% endif %}>Last 24h</option>
                <option value="7d" {% if active_range == "7d" %}selected{% endif %}>Last 7 days</option>
                <option value="30d" {% if active_range == "30d" %}selected{% endif %}>Last 30 days</option>
                <option value="mtd" {% if active_range == "mtd" %}selected{% endif %}>Month to date</option>
            </select>
        </label>
    </form>
    <p>Period: {{ from_dt.isoformat() }} → {{ to_dt.isoformat() }}</p>
</section>

<section>
    <h2>Akumulasi ({{ active_range }}): {{ "%.4f"|format(total_cumulative) }} alpha</h2>
</section>

<table>
    <thead>
        <tr>
            <th>Name</th>
            <th>Akumulasi</th>
        </tr>
    </thead>
    <tbody>
        {% for p in persons %}
        <tr>
            <td><a href="/person/{{ p.name }}?range={{ active_range }}">{{ p.name }}</a></td>
            <td>{{ "%.4f"|format(p.cumulative) }}</td>
        </tr>
        {% endfor %}
    </tbody>
</table>

{% endblock %}
```

- [ ] **Step 5: Create `person.html` template**

`src/emission_tracker/web/templates/person.html`:

```html
{% extends "base.html" %}
{% block content %}

<p><a href="/?range={{ active_range }}">← Back to dashboard</a></p>

<h2>{{ name }}</h2>
<p>Akumulasi ({{ active_range }}): {{ "%.4f"|format(total_cumulative) }} alpha</p>

<canvas id="cumChart" width="800" height="300"></canvas>
<canvas id="perSnapChart" width="800" height="300"></canvas>

<h3>Hotkey breakdown</h3>
<ul>
    {% for hk in hotkeys %}
    <li>{{ hk }}</li>
    {% endfor %}
</ul>

<script>
const labels = {{ chart_labels|tojson }};
const cumulative = {{ chart_cumulative|tojson }};
const perSnap = {{ chart_per_snap|tojson }};

new Chart(document.getElementById('cumChart'), {
    type: 'line',
    data: { labels, datasets: [{ label: 'Cumulative', data: cumulative, fill: false }] },
});

new Chart(document.getElementById('perSnapChart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Per snapshot', data: perSnap }] },
});
</script>

{% endblock %}
```

- [ ] **Step 6: Create `style.css`**

`src/emission_tracker/web/static/style.css`:

```css
main { max-width: 1000px; margin: 0 auto; padding: 1rem; }
table { width: 100%; }
canvas { margin: 1rem 0; }
```

- [ ] **Step 7: Implement `routes_pages.py`**

`src/emission_tracker/web/routes_pages.py`:

```python
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from emission_tracker.web import queries
from emission_tracker.web.range_parse import parse_range

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db_conn


def _range(preset, frm, to):
    try:
        return parse_range(preset=preset, from_str=frm, to_str=to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def register_pages(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        range: str | None = Query(default="all"),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ):
        from_dt, to_dt = _range(range, from_, to)
        conn = _db(request)
        persons = queries.dashboard_summary(conn, from_dt=from_dt, to_dt=to_dt)
        total = sum(p["cumulative"] for p in persons)
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "persons": persons,
                "total_cumulative": total,
                "from_dt": from_dt,
                "to_dt": to_dt,
                "active_range": range or "all",
                "latest": latest,
            },
        )

    @app.get("/person/{name}", response_class=HTMLResponse)
    def person_detail(
        request: Request,
        name: str,
        range: str | None = Query(default="all"),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ):
        from_dt, to_dt = _range(range, from_, to)
        conn = _db(request)
        series = queries.person_series(conn, name=name, from_dt=from_dt, to_dt=to_dt)
        total = series[-1]["cumulative"] if series else 0.0
        hotkeys = [
            r["ss58"]
            for r in conn.execute(
                """
                SELECT h.ss58 FROM hotkeys h
                JOIN persons p ON p.id = h.person_id
                WHERE p.name = ? ORDER BY h.ss58
                """,
                (name,),
            ).fetchall()
        ]
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            "person.html",
            {
                "request": request,
                "name": name,
                "hotkeys": hotkeys,
                "total_cumulative": total,
                "chart_labels": [str(s["taken_at"]) for s in series],
                "chart_cumulative": [s["cumulative"] for s in series],
                "chart_per_snap": [s["per_snapshot_emission"] for s in series],
                "active_range": range or "all",
                "latest": latest,
            },
        )
```

- [ ] **Step 8: Run tests — confirm pass**

Run: `pytest tests/test_routes_pages.py -v`
Expected: 3 passed.

- [ ] **Step 9: Commit**

```bash
git add src/emission_tracker/web/routes_pages.py src/emission_tracker/web/templates src/emission_tracker/web/static tests/test_routes_pages.py
git commit -m "feat(web): add HTML dashboard + per-person detail pages"
```

---

## Task 11: App Factory, Scheduler, Lifespan

Tie it all together. `main.py` exposes `app` (the uvicorn target). Lifespan opens DB, runs schema init + team sync, builds client + rate limiter, starts APScheduler.

**Files:**
- Create: `src/emission_tracker/bot/scheduler.py`
- Create: `src/emission_tracker/main.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: Implement `scheduler.py`**

`src/emission_tracker/bot/scheduler.py`:

```python
import logging
import sqlite3
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from emission_tracker.bot.snapshot import take_snapshot
from emission_tracker.config import AppConfig
from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import TaoStatsClient

log = logging.getLogger(__name__)


def build_scheduler(
    config: AppConfig,
    conn_factory: Callable[[], sqlite3.Connection],
    client: TaoStatsClient,
    rate_limiter: TokenBucket,
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()

    def job():
        conn = conn_factory()
        try:
            take_snapshot(
                conn=conn,
                client=client,
                rate_limiter=rate_limiter,
                subnet_id=config.subnet_id,
                request_interval_seconds=config.polling.request_interval_seconds,
            )
        except Exception:
            log.exception("snapshot run failed")
        finally:
            conn.close()

    scheduler.add_job(
        job,
        "interval",
        minutes=config.polling.interval_minutes,
        max_instances=1,
        coalesce=True,
        id="take_snapshot",
    )
    return scheduler
```

- [ ] **Step 2: Implement `main.py`**

`src/emission_tracker/main.py`:

```python
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from emission_tracker.bot.scheduler import build_scheduler
from emission_tracker.bot.snapshot import take_snapshot
from emission_tracker.config import AppConfig
from emission_tracker.db import connect, init_schema, sync_team
from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import TaoStatsClient
from emission_tracker.web.routes_api import router as api_router
from emission_tracker.web.routes_pages import register_pages

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_app(
    config_path: Path = Path("config.yaml"),
    env_path: Path = Path(".env"),
) -> FastAPI:
    _setup_logging()
    config = AppConfig.load(yaml_path=config_path, env_path=env_path)

    db_path = config.database.path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    rate_limiter = TokenBucket(capacity=5, refill_per_second=5 / 60)
    client = TaoStatsClient(api_key=config.taostats_api_key.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Init DB
        long_lived_conn = sqlite3.connect(db_path, check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES)
        long_lived_conn.row_factory = sqlite3.Row
        long_lived_conn.execute("PRAGMA foreign_keys = ON")
        init_schema(long_lived_conn)
        sync_team(long_lived_conn, config.team, subnet_id=config.subnet_id)
        app.state.db_conn = long_lived_conn

        # Scheduler with its own connection per job
        def conn_factory():
            c = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            return c

        scheduler = build_scheduler(config, conn_factory, client, rate_limiter)
        scheduler.start()

        if config.polling.run_on_startup:
            # Fire once immediately in the scheduler so it runs off the main thread
            scheduler.add_job(
                lambda: take_snapshot(
                    conn=conn_factory(),
                    client=client,
                    rate_limiter=rate_limiter,
                    subnet_id=config.subnet_id,
                    request_interval_seconds=config.polling.request_interval_seconds,
                ),
                id="initial_run",
            )

        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            client.close()
            long_lived_conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(api_router, prefix="/api")
    register_pages(app)
    return app


# uvicorn entry point
app = None  # populated lazily to avoid eager load when imported by tests


def _get_app():
    global app
    if app is None:
        app = create_app()
    return app
```

- [ ] **Step 3: Adjust uvicorn entry to call factory**

Edit `pyproject.toml` — no change needed if we run `uvicorn emission_tracker.main:create_app --factory`. Update README accordingly in Task 12.

- [ ] **Step 4: Write a smoke test for `create_app`**

`tests/test_main.py`:

```python
from pathlib import Path

import pytest

from emission_tracker.main import create_app


def test_create_app_initializes(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
subnet_id: 56
polling:
  interval_minutes: 72
  request_interval_seconds: 15
  run_on_startup: false
database:
  path: {db}
web:
  host: 127.0.0.1
  port: 8000
team:
  - name: Test
    hotkeys:
      - 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1
""".format(db=str(tmp_path / "test.db"))
    )
    env = tmp_path / ".env"
    env.write_text("TAOSTATS_API_KEY=test-key\n")

    app = create_app(config_path=config, env_path=env)
    assert app is not None
    assert app.title == "FastAPI"
```

- [ ] **Step 5: Run test**

Run: `pytest tests/test_main.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add src/emission_tracker/main.py src/emission_tracker/bot/scheduler.py tests/test_main.py
git commit -m "feat: wire app factory with scheduler, lifespan, and routes"
```

---

## Task 12: README + End-to-End Smoke

Document setup and run, then perform one manual end-to-end run to confirm the dashboard renders real data.

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

````markdown
# Gradient Emission Tracker

Bot Python yang merekam emisi alpha/TAO untuk 22 hotkey tim Gradient di Bittensor subnet 56, dan menampilkannya di dashboard web sederhana.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env             # isi TAOSTATS_API_KEY
cp config.example.yaml config.yaml  # edit team kalau perlu
```

## Run

```bash
uvicorn emission_tracker.main:create_app --factory --host 0.0.0.0 --port 8000
```

Buka <http://127.0.0.1:8000>.

## Test

```bash
pytest -v
```

## Konfigurasi

- `.env` — `TAOSTATS_API_KEY` (wajib), `LOG_LEVEL` (default INFO).
- `config.yaml` — subnet_id, polling interval, team roster (lihat `config.example.yaml`).

## Arsitektur

Single-process: FastAPI + APScheduler. Snapshot worker tiap 72 menit fetch 22 hotkey (sequential, 5 req/min rate limit) lalu simpan ke SQLite. Web baca dari SQLite.

Spec lengkap: [docs/superpowers/specs/2026-05-17-emission-tracker-design.md](docs/superpowers/specs/2026-05-17-emission-tracker-design.md).
````

- [ ] **Step 2: Run full test suite**

Run: `pytest -v`
Expected: All tests pass (~37 tests across all modules).

- [ ] **Step 3: Run app manually**

```bash
uvicorn emission_tracker.main:create_app --factory --reload
```

Open <http://127.0.0.1:8000>. Wait for the initial snapshot (~5.5 minutes) — refresh after that. Expected: see your 11 team names with cumulative emission values from one real snapshot.

If you see errors:
- Check log output for `snapshot run failed`.
- If the API returns a shape different from Task 0's assumption, edit `taostats_client.py::_parse_neuron` to match the real shape and re-test.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add README with setup, run, and architecture summary"
```

- [ ] **Step 5: Final tag**

```bash
git tag v0.1.0
```

---

## Out-of-scope (explicitly deferred)

- Telegram/email notifications on deregistration.
- Multi-subnet tracking.
- Authentication on web UI.
- systemd unit / nginx config.
- Aggregate "team total" historical chart.

These can be added as follow-up plans once v0.1.0 is running stably.
