import httpx
import pytest

from emission_tracker.rate_limiter import TokenBucket
from emission_tracker.taostats_client import AlphaPrice, TaoStatsClient
from emission_tracker.web.price import AlphaPriceCache

# Shapes verified against the live TaoStats API on 2026-09-11.
POOL_OK = {"data": [{"price": 0.016145951, "name": "Gradients", "symbol": "ج"}]}
MARKET_OK = {"data": [{"price": 236.898689707984, "symbol": "TAO"}]}


def _client(handler) -> TaoStatsClient:
    c = TaoStatsClient(api_key="k")
    c._client = httpx.Client(
        base_url="https://api.taostats.io", transport=httpx.MockTransport(handler)
    )
    return c


def _both_ok(request: httpx.Request) -> httpx.Response:
    if "pool" in request.url.path:
        return httpx.Response(200, json=POOL_OK)
    return httpx.Response(200, json=MARKET_OK)


class TestAlphaPriceFetch:
    def test_multiplies_the_two_quotes(self):
        price = _client(_both_ok).get_alpha_price(56)
        assert price.alpha_in_tao == pytest.approx(0.016145951)
        assert price.tao_in_usd == pytest.approx(236.898689707984)
        assert price.alpha_in_usd == pytest.approx(3.8249, abs=1e-4)
        assert price.subnet_name == "Gradients"

    def test_asks_for_the_subnet_it_was_given(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return _both_ok(request)

        _client(handler).get_alpha_price(56)
        assert any("netuid=56" in u for u in seen)

    def test_missing_market_quote_yields_no_price(self):
        """Half a quote must not become a settlement price."""

        def handler(request):
            if "pool" in request.url.path:
                return httpx.Response(200, json=POOL_OK)
            return httpx.Response(200, json={"data": []})

        assert _client(handler).get_alpha_price(56) is None

    def test_missing_pool_quote_yields_no_price(self):
        def handler(request):
            if "pool" in request.url.path:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json=MARKET_OK)

        assert _client(handler).get_alpha_price(56) is None
        # And it must not have bothered asking for the TAO price after that.


class _FakeClient:
    def __init__(self, price=None, boom=None):
        self.calls = 0
        self._price = price
        self._boom = boom

    def get_alpha_price(self, subnet_id):
        self.calls += 1
        if self._boom:
            raise self._boom
        return self._price


def _cache(client, ttl=60, clock=None):
    ticks = {"t": 0.0}
    return (
        AlphaPriceCache(
            client=client,
            rate_limiter=TokenBucket(capacity=100, refill_per_second=100),
            subnet_id=56,
            ttl_seconds=ttl,
            clock=clock or (lambda: ticks["t"]),
        ),
        ticks,
    )


class TestAlphaPriceCache:
    PRICE = AlphaPrice(alpha_in_tao=0.016, tao_in_usd=200.0, subnet_name="Gradients")

    def test_second_read_inside_the_ttl_does_not_refetch(self):
        """Opening the dialog twice must not spend the snapshot's API budget."""
        client = _FakeClient(price=self.PRICE)
        cache, _ = _cache(client)
        first, second = cache.get(), cache.get()
        assert client.calls == 1
        assert first["alpha_in_usd"] == second["alpha_in_usd"] == pytest.approx(3.2)

    def test_it_refetches_once_the_ttl_expires(self):
        client = _FakeClient(price=self.PRICE)
        cache, ticks = _cache(client, ttl=60)
        cache.get()
        ticks["t"] = 61.0
        cache.get()
        assert client.calls == 2

    def test_age_is_reported_so_the_admin_can_see_staleness(self):
        client = _FakeClient(price=self.PRICE)
        cache, ticks = _cache(client, ttl=60)
        cache.get()
        ticks["t"] = 30.0
        assert cache.get()["age_seconds"] == pytest.approx(30.0)

    def test_a_failed_fetch_returns_none_rather_than_a_guess(self):
        """The number is frozen into a settlement that cannot be edited, so
        'unknown' must stay distinguishable from a real quote."""
        cache, _ = _cache(_FakeClient(boom=RuntimeError("network gone")))
        assert cache.get() is None

    def test_an_unavailable_price_returns_none(self):
        cache, _ = _cache(_FakeClient(price=None))
        assert cache.get() is None

    def test_a_failure_is_not_cached_as_success(self):
        """A blip must not poison the next minute of reads."""
        client = _FakeClient(boom=RuntimeError("blip"))
        cache, _ = _cache(client)
        assert cache.get() is None
        client._boom = None
        client._price = self.PRICE
        assert cache.get()["alpha_in_usd"] == pytest.approx(3.2)
