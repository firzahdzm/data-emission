import pytest
from pydantic import ValidationError

from emission_tracker.config import (
    AppConfig,
    DatabaseConfig,
    PersonConfig,
    PollingConfig,
    WebConfig,
)


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
