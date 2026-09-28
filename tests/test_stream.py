import io
import json
from decimal import Decimal

import pytest

from cof_bot.config import load_stream_settings
from cof_bot.errors import ConfigError
from cof_bot.exchange.universe import UsdPair, Universe
from cof_bot.market.volume import Trade
from cof_bot.news.feeds import Headline, PollResult
from cof_bot.runtime import StreamRunner


def test_stream_defaults_and_overrides():
    s = load_stream_settings({})
    assert s.l2_max_products == 25 and s.correlator.volume_ratio_min == 2.0
    s = load_stream_settings({"COF_L2_MAX_PRODUCTS": "30", "COF_NEGATIVE_VETO": "-0.5", "COF_MIN_BUY_SHARE": "0.6"})
    assert s.l2_max_products == 30 and s.correlator.negative_veto == -0.5 and s.correlator.min_buy_share == 0.6


@pytest.mark.parametrize(
    "env",
    [
        {"COF_L2_MAX_PRODUCTS": "31"},
        {"COF_VOLUME_WINDOW_S": "90"},
        {"COF_VOLUME_WINDOW_S": "600", "COF_VOLUME_BASELINE_S": "300"},
        {"COF_MIN_BUY_SHARE": "1.5"},
        {"COF_NEGATIVE_VETO": "0.2"},
        {"COF_NEGATIVE_VETO": "x"},
        {"COF_MIN_MEAN_SENTIMENT": "2"},
        {"COF_NEWS_POLL_S": "10"},
    ],
)
def test_stream_invalid(env):
    with pytest.raises(ConfigError):
        load_stream_settings(env)


class FakeFeed:
    def __init__(self, product_ids, state, **kw):
        self.product_ids, self.state, self.kw = product_ids, state, kw
        self.connects = self.failures = 0
        self.l2 = []

    def start(self):
        self.connects = 1

    def stop(self, timeout=10):
        pass

    def set_l2_products(self, ids):
        self.l2 = list(ids)[:25]
        return self.l2


class FakeCollector:
    def __init__(self, items):
        self.items = items

    def poll(self):
        items, self.items = self.items, []
        return PollResult(new=items)


def test_runner_end_to_end_emits_headline_and_signal():
    start = 1_000_000 * 60.0
    now = start + 65 * 60 - 1
    d = Decimal("1")
    universe = Universe([UsdPair("ABC-USD", "ABC", "Abcoin", "ABC", d, d, d, d, None, None)], True, "test", 1)
    out = io.StringIO()
    h = Headline("h1", "src", "Abcoin surges on major exchange listing", "", now - 120, now - 120)
    settings = load_stream_settings({"COF_MIN_WINDOW_USD": "1000"})
    r = StreamRunner(universe, settings, out=out, clock=lambda: now, feed_factory=FakeFeed, collector=FakeCollector([h]))
    with r.state.lock:
        for m in range(60):
            r.volume.add(Trade("ABC-USD", f"b{m}", d, Decimal("100"), "BUY", start + m * 60))
        for m in range(60, 65):
            r.volume.add(Trade("ABC-USD", f"w{m}", d + Decimal(m - 59) / 100, Decimal("600"), "SELL", start + m * 60 + 1))
    for item in r.collector.poll().new:
        r.headlines.put(item)
    assert r.process_headlines() == 1
    r.evaluate()
    recs = [json.loads(line) for line in out.getvalue().splitlines()]
    assert recs[0]["type"] == "headline" and recs[0]["products"] == ["ABC-USD"] and recs[0]["label"] == "positive"
    sig = recs[1]
    assert sig["type"] == "signal" and sig["product_id"] == "ABC-USD" and sig["volume_ratio"] > 2
    assert r.feed.l2 == ["ABC-USD"]
    r.status(r.correlator.evaluate(now, r.volume, {}, ["ABC-USD"]))
    assert json.loads(out.getvalue().splitlines()[-1])["signals_total"] == 1


def test_runner_rejects_empty_universe():
    with pytest.raises(ValueError):
        StreamRunner(Universe([], True, "t", 0), load_stream_settings({}), feed_factory=FakeFeed)
