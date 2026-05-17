from pathlib import Path

import pytest

from emission_tracker.config import AppConfig, _read_env_key


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


def test_load_reads_yaml_and_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(YAML_CONTENT)
    env_path = tmp_path / ".env"
    env_path.write_text(ENV_CONTENT)

    cfg = AppConfig.load(yaml_path=yaml_path, env_path=env_path)

    assert cfg.subnet_id == 56
    assert cfg.team[0].name == "Alice"
    assert cfg.taostats_api_key.get_secret_value() == "tao-test-key"


def test_load_fails_when_api_key_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(YAML_CONTENT)
    env_path = tmp_path / ".env"
    env_path.write_text("# empty\n")

    with pytest.raises(ValueError, match="TAOSTATS_API_KEY"):
        AppConfig.load(yaml_path=yaml_path, env_path=env_path)


def test_load_fails_when_yaml_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(ENV_CONTENT)

    with pytest.raises(FileNotFoundError):
        AppConfig.load(yaml_path=tmp_path / "missing.yaml", env_path=env_path)


def test_read_env_key_supports_export_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("export TAOSTATS_API_KEY=exported-value\n")
    assert _read_env_key(env, "TAOSTATS_API_KEY") == "exported-value"


def test_read_env_key_strips_matched_quotes_only(tmp_path, monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAR", raising=False)
    env = tmp_path / ".env"
    env.write_text('FOO="abc"\nBAR=\'xyz\'\n')
    assert _read_env_key(env, "FOO") == "abc"
    assert _read_env_key(env, "BAR") == "xyz"


def test_read_env_key_falls_back_to_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("FROM_SHELL", "shell-val")
    env = tmp_path / ".env"
    env.write_text("# nothing here\n")
    # env file has no FROM_SHELL → falls back to os.environ
    assert _read_env_key(env, "FROM_SHELL") == "shell-val"
    # also works when env_path is None
    assert _read_env_key(None, "FROM_SHELL") == "shell-val"
