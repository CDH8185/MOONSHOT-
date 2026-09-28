from decimal import Decimal

import pytest

from cof_bot.market.order_book import OrderBook
from cof_bot.market.volume import Trade, VolumeTracker
from cof_bot.news.feeds import Headline
from cof_bot.sentiment.analyzer import SentimentScore
from cof_bot.signals.correlator import CorrelatorSettings, SentimentVolumeCorrelator

START = 1_000_000 * 60.0
NOW = START + 65 * 60 - 1  # baseline minutes 0..59, window minutes 60..64
P = "ABC-USD"


def volume(window_per_min=600, taker_buy=True, rising=True):
    v = VolumeTracker(300, 3600)
    for m in range(60):
        v.add(Trade(P, f"b{m}", Decimal("1"), Decimal("100"), "BUY", START + m * 60))
    for m in range(60, 65):
        px = Decimal("1") + (Decimal(m - 59) / 100 if rising else -Decimal(m - 59) / 100)
        v.add(Trade(P, f"w{m}", px, Decimal(window_per_min) / px, "SELL" if taker_buy else "BUY", START + m * 60 + 1))
    return v


def headline(i, at, title="ABC surges"):
    return Headline(f"h{i}", "src", title, "", at, at)


def score(c):
    return SentimentScore(c, 0, 0, 0)


def corr(**kw):
    return SentimentVolumeCorrelator(CorrelatorSettings(min_window_usd=1000, **kw))


def test_signal_fires_with_both_conditions():
    c = corr()
    c.add(headline(1, NOW - 600), [P], score(0.7))
    ev = c.evaluate(NOW, volume(), product_ids=[P, "OTHER-USD"])
    assert len(ev.signals) == 1
    s = ev.signals[0]
    assert s.product_id == P and s.mentions == 1 and s.volume_ratio == pytest.approx(6.0)
    assert s.window_usd == pytest.approx(3000) and s.buy_share == 1.0 and s.price_change_pct > 0
    assert s.lag_s == 600 and s.strength == pytest.approx(0.7 * 2.585, rel=1e-3)
    assert ev.hot_products[0] == P and ev.sentiment_spikes == 1 and ev.volume_surges == 1
    assert s.to_dict()["titles"] == ["ABC surges"]


def test_cooldown_blocks_repeat():
    c = corr(cooldown_s=900)
    c.add(headline(1, NOW - 60), [P], score(0.7))
    v = volume()
    assert c.evaluate(NOW, v, product_ids=[P]).signals
    assert not c.evaluate(NOW + 10, v, product_ids=[P]).signals


@pytest.mark.parametrize(
    "setup, vol_kw",
    [
        ("no_news", {}),
        ("negative_veto", {}),
        ("low_mean", {}),
        ("stale_news", {}),
        ("ok_news", {"window_per_min": 150}),  # ratio 1.5, below 2.0
        ("ok_news", {"taker_buy": False}),  # sellers driving volume
        ("ok_news", {"rising": False}),  # price falling
    ],
)
def test_each_gate_blocks(setup, vol_kw):
    c = corr()
    if setup == "negative_veto":
        c.add(headline(1, NOW - 60), [P], score(0.9))
        c.add(headline(2, NOW - 30, "ABC exploit"), [P], score(-0.6))
    elif setup == "low_mean":
        c.add(headline(1, NOW - 60), [P], score(0.1))
    elif setup == "stale_news":
        c.add(headline(1, NOW - 2400), [P], score(0.9))  # within the hour, but older than max_lag 1800
    elif setup == "ok_news":
        c.add(headline(1, NOW - 60), [P], score(0.9))
    assert c.evaluate(NOW, volume(**vol_kw), product_ids=[P]).signals == []


def test_min_window_usd_blocks_thin_market():
    c = SentimentVolumeCorrelator(CorrelatorSettings(min_window_usd=5000))
    c.add(headline(1, NOW - 60), [P], score(0.9))
    assert c.evaluate(NOW, volume(), product_ids=[P]).signals == []  # $3,000 < $5,000


def test_frequently_mentioned_coin_needs_a_real_spike():
    c = corr()
    # 24 mentions over the prior 24 h = 1 per hour baseline; 1 mention now is not 2x.
    for i in range(24):
        c.add(headline(100 + i, NOW - 3600 - i * 3600 - 10), [P], score(0.5))
    c.add(headline(1, NOW - 60), [P], score(0.8))
    st = c.sentiment_state(P, NOW)
    assert st.reason == "mentions not above baseline"
    c.add(headline(2, NOW - 50), [P], score(0.8))
    assert c.sentiment_state(P, NOW).spike


def test_duplicate_headline_and_prune():
    c = corr()
    h = headline(1, NOW - 60)
    assert c.add(h, [P], score(0.8)) == 1 and c.add(h, [P], score(0.8)) == 0
    c.evaluate(NOW + 10 * 86400, VolumeTracker(), product_ids=[P])
    assert c.sentiment_state(P, NOW + 10 * 86400).mentions == 0


def test_book_stats_attached_when_available():
    c = corr()
    c.add(headline(1, NOW - 60), [P], score(0.8))
    b = OrderBook(P)
    b.apply("snapshot", [{"side": "bid", "price_level": "1.04", "new_quantity": "100"}, {"side": "offer", "price_level": "1.06", "new_quantity": "50"}])
    s = c.evaluate(NOW, volume(), books={P: b}, product_ids=[P]).signals[0]
    assert s.spread_bps == pytest.approx(190.48, abs=0.01) and s.book_imbalance > 0
