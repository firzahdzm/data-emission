import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from emission_tracker.bot.scheduler import build_scheduler
from emission_tracker.bot.snapshot import take_snapshot
from emission_tracker.config import AppConfig
from emission_tracker.db import cleanup_orphaned_snapshots, init_schema, sync_team
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
    # Allow overriding config + env paths via env vars — useful for running
    # a parallel dev server with config.dev.yaml + data/dev.db while the
    # production tracker keeps running unchanged.
    config_path = Path(os.environ.get("EMISSION_CONFIG_PATH") or config_path)
    env_path = Path(os.environ.get("EMISSION_ENV_PATH") or env_path)
    config = AppConfig.load(yaml_path=config_path, env_path=env_path)

    db_path = config.database.path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    rate_limiter = TokenBucket(capacity=5, refill_per_second=5 / 60)
    client = TaoStatsClient(api_key=config.taostats_api_key.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Long-lived connection for web routes; check_same_thread=False because
        # FastAPI runs sync handlers on a thread pool.
        long_lived_conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        long_lived_conn.row_factory = sqlite3.Row
        long_lived_conn.execute("PRAGMA foreign_keys = ON")
        init_schema(long_lived_conn)
        cleaned = cleanup_orphaned_snapshots(long_lived_conn)
        if cleaned:
            log.info("marked %d orphaned in_progress snapshot(s) as failed", cleaned)
        sync_team(long_lived_conn, config.team, subnet_id=config.subnet_id)
        app.state.db_conn = long_lived_conn
        # Expose config so admin gating (web/auth.py) can read admin_users
        app.state.config = config

        # Scheduler with its own connection per job
        def conn_factory():
            c = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            return c

        scheduler = build_scheduler(config, conn_factory, client, rate_limiter)
        scheduler.start()

        if config.polling.run_on_startup:
            scheduler.add_job(
                lambda: _safe_snapshot(
                    conn_factory, client, rate_limiter,
                    config.subnet_id, config.polling.request_interval_seconds,
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


def _safe_snapshot(conn_factory, client, rate_limiter, subnet_id, request_interval_seconds):
    """Run a single snapshot, swallowing exceptions (don't crash scheduler)."""
    conn = conn_factory()
    try:
        take_snapshot(
            conn=conn,
            client=client,
            rate_limiter=rate_limiter,
            subnet_id=subnet_id,
            request_interval_seconds=request_interval_seconds,
        )
    except Exception:
        log.exception("initial snapshot run failed")
    finally:
        conn.close()
