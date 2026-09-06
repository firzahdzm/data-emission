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


def _write_config(tmp_path: Path, *, run_on_startup: bool) -> tuple[Path, Path]:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
subnet_id: 56
polling:
  interval_minutes: 72
  request_interval_seconds: 15
  run_on_startup: {startup}
database:
  path: {db}
web:
  host: 127.0.0.1
  port: 8000
team:
  - name: Test
    hotkeys:
      - hotkey: 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1
        coldkey: 5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2
""".format(startup=str(run_on_startup).lower(), db=str(tmp_path / "test.db"))
    )
    env = tmp_path / ".env"
    env.write_text("TAOSTATS_API_KEY=test-key\n")
    return config, env


@pytest.mark.parametrize("run_on_startup", [True, False])
def test_startup_seeds_balances_alongside_the_snapshot(
    tmp_path: Path, monkeypatch, run_on_startup: bool
):
    """Balances refresh daily, and an interval job's first run is a full day
    away — so a restart must seed them, or the coldkey cards sit empty for
    24 hours.

    Asserts on the jobs the lifespan registers rather than on them having
    run: the scheduler fires them on its own thread, and racing it would
    make this test flaky. Nothing here touches the network.
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from fastapi.testclient import TestClient

    monkeypatch.delenv("TAOSTATS_API_KEY", raising=False)

    registered: list[str] = []
    real_add_job = BackgroundScheduler.add_job

    def spy_add_job(self, func, *args, **kwargs):
        registered.append(kwargs.get("id"))
        return real_add_job(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundScheduler, "add_job", spy_add_job)
    # Never let the scheduler actually fire a job during the test. Shutdown
    # is stubbed too, since it raises on a scheduler that never started.
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self, *a, **kw: None)
    monkeypatch.setattr(BackgroundScheduler, "shutdown", lambda self, *a, **kw: None)

    config, env = _write_config(tmp_path, run_on_startup=run_on_startup)
    app = create_app(config_path=config, env_path=env)
    with TestClient(app) as client:
        assert client.get("/api/healthz").status_code == 200

    # Both recurring jobs exist regardless of the startup setting.
    assert "take_snapshot" in registered
    assert "refresh_balances" in registered

    if run_on_startup:
        assert "initial_run" in registered
        assert "initial_balance_run" in registered
    else:
        assert "initial_run" not in registered
        assert "initial_balance_run" not in registered
