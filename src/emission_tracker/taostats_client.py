import time
from dataclasses import dataclass

import httpx


# Verified against live TaoStats API on 2026-05-17. Response shape:
#   {"pagination": {...}, "data": [{"uid", "emission" (string), "block_number", ...}]}
# Emission is returned as a string; _parse_neuron converts to float.
DEFAULT_BASE_URL = "https://api.taostats.io"
NEURON_PATH = "/api/neuron/latest/v1"
ACCOUNT_PATH = "/api/account/latest/v1"
POOL_PATH = "/api/dtao/pool/latest/v1"
TAO_PRICE_PATH = "/api/price/latest/v1"
AUTH_HEADER = "Authorization"

# One token refills every 12s at the configured 5/min. Retrying a 429 any
# sooner than that is guaranteed to arrive before the budget has recovered.
RATE_LIMIT_BACKOFF = 15.0


@dataclass(frozen=True)
class NeuronInfo:
    uid: int
    emission: float
    block: int | None


@dataclass(frozen=True)
class AlphaPrice:
    """What one alpha of a subnet is worth, right now.

    Two numbers from two endpoints: the pool gives alpha priced in TAO,
    and the market gives TAO priced in USD. Both are kept alongside the
    product so a settlement can record what it was derived from, not just
    the final figure.
    """

    alpha_in_tao: float
    tao_in_usd: float
    subnet_name: str | None = None

    @property
    def alpha_in_usd(self) -> float:
        return self.alpha_in_tao * self.tao_in_usd


@dataclass(frozen=True)
class AccountInfo:
    """A coldkey's wallet balances, all in rao.

    `staked_rao` is stake across every subnet; `stake_alpha_rao` and
    `stake_alpha_as_tao_rao` narrow that to the one subnet we track. The
    two differ whenever a coldkey holds alpha elsewhere, which several of
    ours do, so a dashboard about one subnet must not quote the total.
    """

    free_rao: int
    staked_rao: int
    total_rao: int
    stake_alpha_rao: int = 0
    stake_alpha_as_tao_rao: int = 0


class TaoStatsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
        rate_limit_backoff: float = RATE_LIMIT_BACKOFF,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={AUTH_HEADER: api_key},
        )
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._rate_limit_backoff = rate_limit_backoff

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TaoStatsClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get_neuron(self, subnet_id: int, hotkey: str) -> NeuronInfo | None:
        params = {"netuid": subnet_id, "hotkey": hotkey}
        response = self._request_with_retry("GET", NEURON_PATH, params=params)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _parse_neuron(response.json())

    def get_account(self, coldkey: str, subnet_id: int | None = None) -> AccountInfo | None:
        """Wallet balances for one coldkey. None if the address is unknown.

        `subnet_id` selects which subnet's alpha stake to total up from the
        response's per-hotkey `alpha_balances`; without it those fields stay
        zero.
        """
        response = self._request_with_retry(
            "GET", ACCOUNT_PATH, params={"address": coldkey}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _parse_account(response.json(), subnet_id=subnet_id)

    def get_alpha_price(self, subnet_id: int) -> AlphaPrice | None:
        """Live price of one alpha on `subnet_id`, in TAO and in USD.

        Two calls, because TaoStats prices alpha against TAO and TAO
        against USD in different places. Returns None if either is
        unavailable — a settlement must not be priced off half a quote.
        """
        pool = self._request_with_retry(
            "GET", POOL_PATH, params={"netuid": subnet_id}
        )
        pool.raise_for_status()
        pool_rows = (pool.json() or {}).get("data") or []
        if not pool_rows:
            return None

        market = self._request_with_retry(
            "GET", TAO_PRICE_PATH, params={"asset": "tao"}
        )
        market.raise_for_status()
        market_rows = (market.json() or {}).get("data") or []
        if not market_rows:
            return None

        alpha_in_tao = pool_rows[0].get("price")
        tao_in_usd = market_rows[0].get("price")
        if alpha_in_tao is None or tao_in_usd is None:
            return None
        return AlphaPrice(
            alpha_in_tao=float(alpha_in_tao),
            tao_in_usd=float(tao_in_usd),
            subnet_name=pool_rows[0].get("name"),
        )

    def _request_with_retry(self, method: str, path: str, **kw) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.request(method, path, **kw)
                # Retry on 5xx and 429 (rate-limit); everything else is final.
                if resp.status_code != 429 and resp.status_code < 500:
                    return resp
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                resp = None
            if attempt < self._max_retries:
                # A 429 means the shared token budget is already spent, and
                # a retry does not go through the limiter — so retrying
                # sooner than a token takes to refill (12s at 5/min) just
                # adds to the flood that caused it. Back off past that.
                rate_limited = resp is not None and resp.status_code == 429
                backoff = (
                    self._rate_limit_backoff if rate_limited
                    else self._retry_backoff
                )
                time.sleep(backoff * (2 ** attempt))
        if last_exc:
            raise last_exc
        resp.raise_for_status()
        return resp  # unreachable; keep type checker happy


def _parse_neuron(payload: dict) -> NeuronInfo | None:
    data = payload.get("data")
    if not data:
        return None
    if isinstance(data, list):
        if not data:
            return None
        item = data[0]
    else:
        item = data
    return NeuronInfo(
        uid=int(item["uid"]),
        emission=float(item["emission"]),
        block=item.get("block_number"),
    )


def _parse_account(payload: dict, subnet_id: int | None = None) -> AccountInfo | None:
    data = payload.get("data")
    if not data:
        return None
    item = data[0] if isinstance(data, list) else data

    # `alpha_balances` lists one entry per (hotkey, subnet). Summing the
    # entries for our subnet gives the coldkey's stake there; entries for
    # other subnets are deliberately ignored.
    alpha = as_tao = 0
    if subnet_id is not None:
        for entry in item.get("alpha_balances") or []:
            if entry.get("netuid") == subnet_id:
                alpha += int(entry.get("balance") or 0)
                as_tao += int(entry.get("balance_as_tao") or 0)

    # Balances come back as decimal strings, not numbers.
    return AccountInfo(
        free_rao=int(item["balance_free"]),
        staked_rao=int(item["balance_staked"]),
        total_rao=int(item["balance_total"]),
        stake_alpha_rao=alpha,
        stake_alpha_as_tao_rao=as_tao,
    )
