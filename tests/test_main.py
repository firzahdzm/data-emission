from pathlib import Path

import pytest

from emission_tracker.main import create_app


def test_create_app_initializes(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)
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
    # Don't trigger lifespan here — just verify factory builds an app
