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
    client = TaoStatsClient(api_key="test", base_url=BASE_URL, retry_backoff=0)
    info = client.get_neuron(subnet_id=56, hotkey=HOTKEY)
    assert info.uid == 1
    assert route.call_count == 3


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
