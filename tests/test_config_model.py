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


class TestWalletConfig:
    def test_bare_string_hotkey_coerces_to_wallet_without_coldkey(self):
        person = PersonConfig(
            name="Legacy",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
        )
        assert person.hotkeys[0].hotkey == "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
        assert person.hotkeys[0].coldkey is None
        assert person.hotkeys[0].label is None

    def test_mapping_form_keeps_coldkey_and_label(self):
        person = PersonConfig(
            name="Firza",
            hotkeys=[
                {
                    "hotkey": "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
                    "coldkey": "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
                    "label": "I",
                }
            ],
        )
        assert person.hotkeys[0].coldkey == "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
        assert person.hotkeys[0].label == "I"

    def test_invalid_coldkey_rejected(self):
        with pytest.raises(ValidationError):
            PersonConfig(
                name="X",
                hotkeys=[
                    {
                        "hotkey": "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
                        "coldkey": "not-an-ss58",
                    }
                ],
            )

    def test_mixed_legacy_and_new_wallets_under_one_person(self):
        person = PersonConfig(
            name="Firza",
            hotkeys=[
                "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
                {
                    "hotkey": "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
                    "coldkey": "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3",
                },
            ],
        )
        assert [w.coldkey for w in person.hotkeys] == [
            None,
            "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3",
        ]
