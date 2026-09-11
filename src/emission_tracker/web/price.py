import logging
import threading
import time

from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import TaoStatsClient

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60


class AlphaPriceCache:
    """Serves the live alpha price, re-fetching at most once per TTL.

    The price endpoints share the one TaoStats rate limiter with the
    emission snapshot, and opening the Close-period dialog must not spend
    the snapshot's budget every time someone looks at it. A minute of
    staleness is irrelevant to a figure the admin reviews and can override,
    and it keeps a reloaded page from re-billing the API.
    """

    def __init__(
        self,
        client: TaoStatsClient,
        rate_limiter: TokenBucket,
        subnet_id: int,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock=time.monotonic,
    ):
        self._client = client
        self._rate_limiter = rate_limiter
        self._subnet_id = subnet_id
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._cached_at: float = 0.0

    def get(self) -> dict | None:
        """Latest price as a plain dict, or None if it could not be read.

        None rather than a stale-but-silent number: the admin is about to
        freeze a price into a settlement that cannot be edited afterwards,
        so "we don't know" has to be distinguishable from "it's $3.82".
        """
        with self._lock:
            now = self._clock()
            if self._cached is not None and now - self._cached_at < self._ttl:
                fresh = dict(self._cached)
                fresh["age_seconds"] = round(now - self._cached_at, 1)
                return fresh

        try:
            self._rate_limiter.acquire()
            price = self._client.get_alpha_price(self._subnet_id)
        except Exception as exc:
            log.warning("alpha price fetch failed: %s", exc)
            return None
        if price is None:
            log.warning("alpha price unavailable for subnet %s", self._subnet_id)
            return None

        payload = {
            "alpha_in_usd": price.alpha_in_usd,
            "alpha_in_tao": price.alpha_in_tao,
            "tao_in_usd": price.tao_in_usd,
            "subnet_name": price.subnet_name,
            "subnet_id": self._subnet_id,
        }
        with self._lock:
            self._cached = payload
            self._cached_at = self._clock()
        out = dict(payload)
        out["age_seconds"] = 0.0
        return out
