import time
from dataclasses import dataclass

import httpx

# Verified against the live Gradients API on 2026-09-06. No authentication.
# Response shape for a coldkey that has deposited:
#   {"coldkey": "5H…", "balance_rao": 2709999999, "total_sent_rao": 195459999999,
#    "transfer_count": 69, "last_transfer_at": "2026-08-31T11:12:12Z", ...}
# A coldkey that has never deposited returns 404 with a "detail" message.
DEFAULT_BASE_URL = "https://api.gradients.io"
TOURNAMENT_BALANCE_PATH = "/tournament/balance/{coldkey}"


@dataclass(frozen=True)
class TournamentBalance:
    """A coldkey's tournament deposit, in rao.

    `balance_rao` is what is left to spend on buy-ins; `total_sent_rao` is
    everything ever deposited, so the difference is what the tournaments
    have consumed.
    """

    balance_rao: int
    total_sent_rao: int
    transfer_count: int
    last_transfer_at: str | None


class GradientsClient:
    """Reads tournament deposits from the Gradients API.

    Mirrors TaoStatsClient's retry behaviour so the two fetchers in the
    balance job behave the same way under a flaky network.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 25.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
    ):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GradientsClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def get_tournament_balance(self, coldkey: str) -> TournamentBalance | None:
        """Tournament deposit for one coldkey.

        Returns None when the coldkey has no tournament account — the API
        404s in that case, which is an ordinary state (the wallet simply
        never paid a buy-in), not a failure. A network or server error
        raises instead, so the caller can tell the two apart.
        """
        path = TOURNAMENT_BALANCE_PATH.format(coldkey=coldkey)
        response = self._request_with_retry("GET", path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return _parse_tournament_balance(response.json())

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
            if attempt < self._max_retries:
                time.sleep(self._retry_backoff * (2 ** attempt))
        if last_exc:
            raise last_exc
        resp.raise_for_status()
        return resp  # unreachable; keep type checker happy


def _parse_tournament_balance(payload: dict) -> TournamentBalance:
    return TournamentBalance(
        balance_rao=int(payload["balance_rao"]),
        total_sent_rao=int(payload["total_sent_rao"]),
        transfer_count=int(payload.get("transfer_count", 0)),
        last_transfer_at=payload.get("last_transfer_at"),
    )
