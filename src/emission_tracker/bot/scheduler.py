import logging
import sqlite3
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from emission_tracker.bot.balances import refresh_balances
from emission_tracker.bot.snapshot import take_snapshot
from emission_tracker.config import AppConfig
from emission_tracker.gradients_client import GradientsClient
from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import TaoStatsClient

log = logging.getLogger(__name__)


def build_scheduler(
    config: AppConfig,
    conn_factory: Callable[[], sqlite3.Connection],
    client: TaoStatsClient,
    rate_limiter: TokenBucket,
    gradients: GradientsClient | None = None,
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

    if gradients is not None:
        def balance_job():
            conn = conn_factory()
            try:
                refresh_balances(
                    conn=conn,
                    taostats=client,
                    gradients=gradients,
                    rate_limiter=rate_limiter,
                    request_interval_seconds=config.polling.request_interval_seconds,
                    subnet_id=config.subnet_id,
                )
            except Exception:
                log.exception("balance refresh failed")
            finally:
                conn.close()

        scheduler.add_job(
            balance_job,
            "interval",
            hours=config.polling.balance_interval_hours,
            max_instances=1,
            coalesce=True,
            id="refresh_balances",
        )

    return scheduler
