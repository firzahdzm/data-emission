import httpx
import pytest
import respx

from emission_tracker.taostats_client import AccountInfo, NeuronInfo, TaoStatsClient


# Verified against live TaoStats API on 2026-11-20. The old
# /api/neuron/latest/v1 answers 410 Gone since 2026-11-16.
BASE_URL = "https://api.taostats.io"
NEURON_PATH = "/v1/subnets/metagraph"
HOTKEY = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"


@respx.mock
def test_get_neuron_reads_one_hotkey_on_one_subnet():
    """The metagraph endpoint lists a whole subnet unless filtered; an
    unfiltered first row would be some other miner's emission."""
    route = respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    params = route.calls.last.request.url.params
    assert params["netuid"] == "56"
    assert params["hotkey"] == HOTKEY


@respx.mock
def test_get_neuron_parses_the_live_metagraph_row():
    # Trimmed from a live response, 2026-11-20. Emission is rao per
    # epoch as a decimal string: daily_mining_alpha is exactly 20x it.
    respx.get(f"{BASE_URL}{NEURON_PATH}").mock(
        return_value=httpx.Response(200, json={
            "pagination": {"current_page": 1, "total_items": 1},
            "data": [{
                "block_number": 9548600,
                "netuid": 56,
                "uid": 59,
                "hotkey": HOTKEY,
                "emission": "2399144392",
                "daily_mining_alpha": "47982887840",
            }],
        })
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info == NeuronInfo(uid=59, emission=2399144392.0, block=9548600)


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


COLDKEY = "5CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC1"


@respx.mock
def test_get_account_reads_the_v1_account_path():
    """The address is part of the /v1 path; the old
    /api/account/latest/v1?address= answers 410 Gone since 2026-11-16."""
    # Trimmed from a live response, 2026-11-20.
    route = respx.get(f"{BASE_URL}/v1/accounts/{COLDKEY}").mock(
        return_value=httpx.Response(200, json={"data": [{
            "address": COLDKEY,
            "balance_free": "8502249111",
            "balance_staked": "41013735",
            "balance_total": "8543262846",
            "alpha_balances": [
                {"netuid": 56, "balance": "2933277185", "balance_as_tao": "41013735"},
                {"netuid": 3, "balance": "5", "balance_as_tao": "1"},
            ],
        }]})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    account = client.get_account(COLDKEY, subnet_id=56)
    assert route.called
    assert account == AccountInfo(
        free_rao=8502249111,
        staked_rao=41013735,
        total_rao=8543262846,
        stake_alpha_rao=2933277185,
        stake_alpha_as_tao_rao=41013735,
    )


@respx.mock
def test_get_account_returns_none_for_an_unknown_address():
    respx.get(f"{BASE_URL}/v1/accounts/{COLDKEY}").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    client = TaoStatsClient(api_key="test", base_url=BASE_URL)
    assert client.get_account(COLDKEY) is None


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
