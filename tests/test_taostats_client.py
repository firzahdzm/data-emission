import httpx
import pytest
import respx

from emission_tracker.taostats_client import NeuronInfo, TaoStatsClient


# Verified against live TaoStats API on 2026-05-17
BASE_URL = "https://api.taostats.io"
NEURON_PATH = "/api/neuron/latest/v1"
HOTKEY = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"


@respx.mock
def test_get_neuron_returns_info_on_200():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{
                    "uid": 42,
                    "emission": 0.521,
                    "block_number": 5_123_456,
                }]
            },
        )
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info == NeuronInfo(uid=42, emission=0.521, block=5_123_456)


@respx.mock
def test_get_neuron_returns_none_on_empty_data():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    assert client.get_neuron(subnet_id=56, hotkey=HOTKEY) is None


@respx.mock
def test_get_neuron_returns_none_on_404():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    assert client.get_neuron(subnet_id=56, hotkey=HOTKEY) is None


@respx.mock
def test_get_neuron_retries_on_5xx_then_succeeds():
    route = respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"data": [{"uid": 1, "emission": 0.1, "block_number": 1}]}),
        ]
    )
    client = TaoStatsClient(
        api_key="test", base_url=BASE_URL,
        retry_backoff=0, rate_limit_backoff=0,
    )
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info.uid == 1
    assert route.call_count == 3


@respx.mock
def test_get_neuron_retries_on_429_then_succeeds():
    route = respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"data": [{"uid": 7, "emission": 0.2, "block_number": 9}]}),
        ]
    )
    client = TaoStatsClient(
        api_key="test", base_url=BASE_URL,
        retry_backoff=0, rate_limit_backoff=0,
    )
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info.uid == 7
    assert route.call_count == 2


@respx.mock
def test_get_neuron_raises_after_max_retries():
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(503)
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL, retry_backoff=0)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_neuron(subnet_id=56, hotkey=HOTKEY)


@respx.mock
def test_get_neuron_sends_auth_header():
    route = respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = TaoStatsClient(api_key="secret-key", base_url=BASE_URL)
    client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "secret-key"


def test_a_429_backs_off_past_the_token_refill_interval(monkeypatch):
    """A retry does not go through the rate limiter, so retrying sooner
    than a token takes to refill (12s at 5/min) adds to the flood that
    caused the 429. Four of fifteen balance reads failed this way."""
    import httpx

    from emission_tracker.taostats_client import (
        RATE_LIMIT_BACKOFF,
        TaoStatsClient,
    )

    slept: list[float] = []
    monkeypatch.setattr(
        "emission_tracker.taostats_client.time.sleep", slept.append
    )

    c = TaoStatsClient(api_key="k", max_retries=2, retry_backoff=5.0)
    c._client = httpx.Client(
        base_url="https://api.taostats.io",
        transport=httpx.MockTransport(lambda r: httpx.Response(429)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        c.get_neuron(subnet_id=56, hotkey="5F")

    assert slept, "a 429 must back off before retrying"
    assert min(slept) >= RATE_LIMIT_BACKOFF
    # And not the short backoff meant for transient 5xx/network blips.
    assert min(slept) > 5.0


def test_a_server_error_still_uses_the_short_backoff(monkeypatch):
    """Only rate limiting needs the long wait; a 5xx is worth retrying
    promptly."""
    import httpx

    from emission_tracker.taostats_client import TaoStatsClient

    slept: list[float] = []
    monkeypatch.setattr(
        "emission_tracker.taostats_client.time.sleep", slept.append
    )

    c = TaoStatsClient(api_key="k", max_retries=1, retry_backoff=5.0)
    c._client = httpx.Client(
        base_url="https://api.taostats.io",
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        c.get_neuron(subnet_id=56, hotkey="5F")
    assert slept == [5.0]
