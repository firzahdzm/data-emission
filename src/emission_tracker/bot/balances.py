import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from emission_tracker.gradients_client import GradientsClient
from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import TaoStatsClient

log = logging.getLogger(__name__)


@dataclass
class BalanceRefreshResult:
    fetched_at: datetime
    coldkey_count: int
    wallet_ok: int
    wallet_fail: int
    tournament_ok: int
    tournament_absent: int
    tournament_fail: int


def refresh_balances(
    conn: sqlite3.Connection,
    taostats: TaoStatsClient,
    gradients: GradientsClient,
    rate_limiter: TokenBucket,
    request_interval_seconds: float,
) -> BalanceRefreshResult:
    """Fetch wallet and tournament balances for every known coldkey.

    Runs on its own schedule, well apart from the emission snapshot: balances
    move slowly and the snapshot loop is already long, so folding these
    requests into it would stretch it for no gain.

    Every coldkey gets a row, even when a fetch fails — the row then carries
    NULLs, which keeps the dashboard honest about what is missing instead of
    silently showing yesterday's number as today's.
    """
    fetched_at = datetime.now(timezone.utc)
    coldkeys = [
        row["coldkey_ss58"]
        for row in conn.execute(
            "SELECT DISTINCT coldkey_ss58 FROM hotkeys "
            "WHERE coldkey_ss58 IS NOT NULL ORDER BY coldkey_ss58"
        ).fetchall()
    ]

    wallet_ok = wallet_fail = 0
    tourn_ok = tourn_absent = tourn_fail = 0

    for i, coldkey in enumerate(coldkeys):
        if i > 0 and request_interval_seconds > 0:
            time.sleep(request_interval_seconds)

        # TaoStats is the rate-limited one; Gradients needs no key and is
        # only throttled by the same pacing loop.
        rate_limiter.acquire()
        try:
            account = taostats.get_account(coldkey)
            wallet_ok += 1
        except Exception as exc:
            log.warning("coldkey=%s wallet fetch failed: %s", coldkey, exc)
            account = None
            wallet_fail += 1

        try:
            tournament = gradients.get_tournament_balance(coldkey)
            tournament_seen = 1
            if tournament is None:
                tourn_absent += 1
            else:
                tourn_ok += 1
        except Exception as exc:
            log.warning("coldkey=%s tournament fetch failed: %s", coldkey, exc)
            tournament = None
            # Unknown, not "no account" — keep the two apart so the UI can
            # show a blank rather than claiming the wallet never deposited.
            tournament_seen = 0
            tourn_fail += 1

        conn.execute(
            """
            INSERT INTO coldkey_balances (
                coldkey_ss58, fetched_at,
                balance_free_rao, balance_staked_rao, balance_total_rao,
                tournament_balance_rao, tournament_total_sent_rao, tournament_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(coldkey_ss58, fetched_at) DO NOTHING
            """,
            (
                coldkey,
                fetched_at,
                account.free_rao if account else None,
                account.staked_rao if account else None,
                account.total_rao if account else None,
                tournament.balance_rao if tournament else None,
                tournament.total_sent_rao if tournament else None,
                tournament_seen,
            ),
        )
        conn.commit()

    log.info(
        "balance refresh — %d coldkeys, wallet %d ok / %d fail, "
        "tournament %d ok / %d none / %d fail",
        len(coldkeys), wallet_ok, wallet_fail, tourn_ok, tourn_absent, tourn_fail,
    )
    return BalanceRefreshResult(
        fetched_at=fetched_at,
        coldkey_count=len(coldkeys),
        wallet_ok=wallet_ok,
        wallet_fail=wallet_fail,
        tournament_ok=tourn_ok,
        tournament_absent=tourn_absent,
        tournament_fail=tourn_fail,
    )


class BalanceRunner:
    """Runs `refresh_balances` on demand, one at a time.

    The admin button and the daily schedule both land here, so the lock is
    what stops a click during the nightly run from opening a second pass
    over the same coldkeys and burning double the API quota.
    """

    def __init__(
        self,
        conn_factory,
        taostats: TaoStatsClient,
        gradients: GradientsClient,
        rate_limiter: TokenBucket,
        request_interval_seconds: float,
    ):
        self._conn_factory = conn_factory
        self._taostats = taostats
        self._gradients = gradients
        self._rate_limiter = rate_limiter
        self._request_interval_seconds = request_interval_seconds
        self._lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    def estimate_seconds(self, coldkey_count: int) -> int:
        """Roughly how long a run takes: the pacing gaps plus per-coldkey
        request time (two APIs, measured at ~1.5s together)."""
        if coldkey_count <= 0:
            return 0
        gaps = (coldkey_count - 1) * self._request_interval_seconds
        return int(gaps + coldkey_count * 1.5)

    def coldkey_count(self) -> int:
        conn = self._conn_factory()
        try:
            return conn.execute(
                "SELECT COUNT(DISTINCT coldkey_ss58) AS n FROM hotkeys "
                "WHERE coldkey_ss58 IS NOT NULL"
            ).fetchone()["n"]
        finally:
            conn.close()

    def start(self) -> bool:
        """Kick off a refresh in the background.

        Returns False when one is already in flight — the caller should tell
        the user to wait rather than queue a second pass.
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started_at = datetime.now(timezone.utc)
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        conn = self._conn_factory()
        try:
            refresh_balances(
                conn=conn,
                taostats=self._taostats,
                gradients=self._gradients,
                rate_limiter=self._rate_limiter,
                request_interval_seconds=self._request_interval_seconds,
            )
        except Exception:
            log.exception("manual balance refresh failed")
        finally:
            conn.close()
            with self._lock:
                self._running = False
