import time
from dataclasses import dataclass

import httpx


# Verified against live TaoStats API on 2026-05-17. Response shape:
#   {"pagination": {...}, "data": [{"uid", "emission" (string), "block_number", ...}]}
# Emission is returned as a string; _parse_neuron converts to float.
DEFAULT_BASE_URL = "https://api.taostats.io"
NEURON_PATH = "/api/neuron/latest/v1"
AUTH_HEADER = "Authorization"


@dataclass(frozen=True)
class NeuronInfo:
    uid: int
    emission: float
    block: int | None


class TaoStatsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 2,
        retry_backoff: float = 5.0,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={AUTH_HEADER: api_key},
        )
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

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
