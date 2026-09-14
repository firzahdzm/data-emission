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


class _ChainAccount:
    """Shapes a signer reading like a TaoStats account.

    Only the fields the row below writes. `staked_rao` and `total_rao`
    are left unknown rather than guessed: the chain read covers the
    configured subnet, and a wallet can hold stake on others — writing
    the subnet figure into a column that means "everywhere" would be a
    wrong number rather than a missing one.
    """

    def __init__(self, entry: dict):
        self.free_rao = entry.get("free_rao")
        self.stake_alpha_rao = entry.get("stake_alpha_rao")
        self.stake_alpha_as_tao_rao = entry.get("stake_alpha_as_tao_rao")
        self.staked_rao = None
        self.total_rao = None


def _chain_balances(signer) -> dict:
    """Every wallet's figures from the signer, or {} if it cannot answer.

    Never raises: a signer that is down must slow the refresh back to
    the TaoStats path, not stop it.
    """
    from emission_tracker.signer.protocol import OP_BALANCES, SignRequest

    try:
        result = signer.send(SignRequest(OP_BALANCES, ""))
    except Exception as exc:
        log.warning("chain balances unavailable, falling back to TaoStats: %s", exc)
        return {}
    if not result.ok:
        log.warning("chain balances refused: %s", result.error)
        return {}
    return {k: v for k, v in (result.balances or {}).items() if v}


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
    subnet_id: int,
    coldkeys: list[str] | None = None,
    signer=None,
) -> BalanceRefreshResult:
    """Fetch wallet and tournament balances for known coldkeys.

    `coldkeys` narrows the run to those addresses — the per-card refresh
    button uses it to re-read one wallet in seconds instead of minutes.
    Unknown addresses are dropped rather than fetched, so a stale page
    cannot make the tracker query arbitrary accounts. None means all.

    Runs on its own schedule, well apart from the emission snapshot: balances
    move slowly and the snapshot loop is already long, so folding these
    requests into it would stretch it for no gain.

    Wallet figures come from the chain through the signer when one is
    reachable, and from TaoStats otherwise. The chain is both faster —
    thirty-five seconds for fifteen wallets against four minutes
    against a five-per-minute limit — and authoritative: TaoStats
    reported an empty alpha position for a wallet that held 44 α, which
    put a stale zero on a card next to a working unstake button. The
    TaoStats path stays for deployments with no signer, and for a
    coldkey whose wallet is not on this host.

    Tournament balances have no second source and always come from the
    Gradients API.

    Every coldkey gets a row, even when a fetch fails — the row then carries
    NULLs, which keeps the dashboard honest about what is missing instead of
    silently showing yesterday's number as today's.
    """
    fetched_at = datetime.now(timezone.utc)
    known = [
        row["coldkey_ss58"]
        for row in conn.execute(
            "SELECT DISTINCT coldkey_ss58 FROM hotkeys "
            "WHERE coldkey_ss58 IS NOT NULL ORDER BY coldkey_ss58"
        ).fetchall()
    ]
    if coldkeys is None:
        coldkeys = known
    else:
        wanted = set(coldkeys)
        coldkeys = [ck for ck in known if ck in wanted]

    wallet_ok = wallet_fail = 0
    tourn_ok = tourn_absent = tourn_fail = 0

    # One call for every wallet, rather than one per coldkey in the loop
    # below: reading the chain is not rate limited, and asking once
    # keeps the whole set consistent with a single moment.
    from_chain = _chain_balances(signer) if signer is not None else {}

    for i, coldkey in enumerate(coldkeys):
        chain = from_chain.get(coldkey)
        if chain and chain.get("free_rao") is not None:
            account = _ChainAccount(chain)
            wallet_ok += 1
        else:
            if i > 0 and request_interval_seconds > 0:
                time.sleep(request_interval_seconds)
            # TaoStats is the rate-limited one; Gradients needs no key and is
            # only throttled by the same pacing loop.
            rate_limiter.acquire()
            try:
                account = taostats.get_account(coldkey, subnet_id=subnet_id)
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
                tournament_balance_rao, tournament_total_sent_rao, tournament_seen,
                stake_alpha_rao, stake_alpha_as_tao_rao
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                account.stake_alpha_rao if account else None,
                account.stake_alpha_as_tao_rao if account else None,
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
        subnet_id: int,
        signer=None,
    ):
        self._conn_factory = conn_factory
        self._signer = signer
        self._taostats = taostats
        self._gradients = gradients
        self._rate_limiter = rate_limiter
        self._request_interval_seconds = request_interval_seconds
        self._subnet_id = subnet_id
        self._lock = threading.Lock()
        self._running = False
        self._started_at: datetime | None = None
        # Which coldkeys the in-flight run covers; None means all of them.
        self._target: list[str] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def target(self) -> list[str] | None:
        """Coldkeys the running refresh covers, or None for a full run.

        The dashboard uses this to spin only the card being refreshed
        instead of freezing all fifteen.
        """
        return self._target

    # Measured on the host: one wallet balance and one stake list take
    # about 2.4s together over the chain, and nothing paces them.
    CHAIN_SECONDS_PER_COLDKEY = 2.4

    def estimate_seconds(self, coldkey_count: int) -> int:
        """Roughly how long a run takes.

        With a signer the chain answers at its own speed — no pacing
        gaps, no rate limit — and the old estimate would have promised
        four minutes for a thirty-five second job, which is its own kind
        of wrong: it decides how long the progress bar claims to need.
        """
        if coldkey_count <= 0:
            return 0
        if self._signer is not None:
            # Plus the tournament call per coldkey, which still goes out
            # over HTTP and is paced with the rest.
            return int(coldkey_count * (self.CHAIN_SECONDS_PER_COLDKEY + 1.0))
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

    def start(self, coldkeys: list[str] | None = None) -> bool:
        """Kick off a refresh in the background.

        `coldkeys` limits the run to those addresses; None refreshes all.
        Returns False when one is already in flight — the caller should tell
        the user to wait rather than queue a second pass. A single-coldkey
        run takes the same lock as a full one, because both draw on the one
        TaoStats rate limiter and overlapping them would only make each
        slower.
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started_at = datetime.now(timezone.utc)
            # `is not None`, not truthiness: an empty selection means
            # "nothing", and must never widen into a full sweep.
            self._target = list(coldkeys) if coldkeys is not None else None
        threading.Thread(target=self._run, args=(coldkeys,), daemon=True).start()
        return True

    def _run(self, coldkeys: list[str] | None = None) -> None:
        conn = self._conn_factory()
        try:
            refresh_balances(
                conn=conn,
                taostats=self._taostats,
                gradients=self._gradients,
                rate_limiter=self._rate_limiter,
                request_interval_seconds=self._request_interval_seconds,
                subnet_id=self._subnet_id,
                coldkeys=coldkeys,
                signer=self._signer,
            )
        except Exception:
            log.exception("manual balance refresh failed")
        finally:
            conn.close()
            with self._lock:
                self._running = False
                self._target = None
