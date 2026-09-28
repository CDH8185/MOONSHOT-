import json
from decimal import Decimal
from pathlib import Path

import pytest

from cof_bot.market.order_book import OrderBook
from cof_bot.market.volume import Trade, VolumeTracker, parse_ts, trade_from_message
from cof_bot.market.ws_feed import MarketState

FIXTURE = Path(__file__).parent / "fixtures" / "ws_live_sample.jsonl"


# order book ---------------------------------------------------------------

def lvl(side, price, qty):
    return {"side": side, "price_level": price, "new_quantity": qty}


def test_book_snapshot_update_and_stats():
    b = OrderBook("X-USD")
    assert b.apply("update", [lvl("bid", "1", "5")]) == 0  # no snapshot yet
    b.apply("snapshot", [lvl("bid", "0.99", "100"), lvl("bid", "0.98", "50"), lvl("offer", "1.01", "40")])
    b.apply("update", [lvl("offer", "1.02", "10"), lvl("bid", "0.98", "0")])
    assert b.bids == {Decimal("0.99"): Decimal("100")}
    s = b.stats(depth_bps=Decimal("200"))
    assert s.best_bid == Decimal("0.99") and s.best_ask == Decimal("1.01")
    assert s.spread_bps == Decimal("200")  # 0.02 / 1.00 x 10,000
    # bid depth 99; ask depth 1.01 x 40 + 1.02 x 10 = 50.6; (99 - 50.6) / 149.6
    assert s.imbalance == (Decimal("99") - Decimal("50.6")) / Decimal("149.6")


def test_book_rejects_bad_levels_and_crossed_book():
    b = OrderBook("X-USD")
    b.apply("snapshot", [lvl("bid", "abc", "1"), lvl("bid", "-1", "1"), lvl("weird", "1", "1"), lvl("bid", "1", "1"), lvl("offer", "0.9", "1")])
    assert list(b.bids) == [Decimal("1")]
    assert b.stats() is None  # crossed
    b.reset()
    assert not b.synced and b.stats() is None


# volume -------------------------------------------------------------------

def test_parse_ts_nanoseconds_and_bad():
    assert parse_ts("2026-09-27T01:40:08.309688615Z") == pytest.approx(1790473208.309688, abs=1e-6)
    assert parse_ts("garbage") is None and parse_ts("") is None


def test_trade_parsing_and_taker_side():
    t = trade_from_message({"product_id": "X-USD", "trade_id": "1", "price": "2", "size": "3", "side": "SELL", "time": "2026-09-27T00:00:00Z"})
    assert t.notional == Decimal("6") and t.taker_buy  # maker sold -> taker bought
    assert trade_from_message({"product_id": "X-USD", "trade_id": "1", "price": "0", "size": "3", "time": "2026-09-27T00:00:00Z"}) is None
    assert trade_from_message({"price": "1"}) is None


def T(i, ts, price="1", size="100", maker="SELL", pid="X-USD"):
    return Trade(pid, str(i), Decimal(price), Decimal(size), maker, ts)


def test_volume_ratio_buy_share_and_dedupe():
    v = VolumeTracker(window_s=300, baseline_s=3600)
    start = 1_000_000 * 60.0
    # baseline: 60 minutes at $100 per minute
    for m in range(60):
        v.add(T(f"b{m}", start + m * 60, maker="BUY"))
    now = start + 65 * 60 - 1
    # window: 5 minutes, $600 per minute, 5/6 taker buys, price 1.00 -> 1.10
    i = 0
    for m in range(60, 65):
        for k in range(6):
            i += 1
            v.add(T(f"w{i}", start + m * 60 + k, price=str(1 + (i / 300)), size="100", maker="SELL" if k else "BUY"))
    assert not v.add(T("w1", start + 3600))  # duplicate trade_id ignored
    s = v.stats("X-USD", now)
    assert s.baseline_usd_per_window == Decimal("500")  # $6,000 over 60 min scaled to 5 min
    assert float(s.ratio) == pytest.approx(float(s.window_usd) / 500)
    assert float(s.buy_share) == pytest.approx(5 / 6, rel=0.02)
    assert s.price_change_pct > 0 and s.trades == 30


def test_volume_ratio_needs_full_baseline_observation():
    v = VolumeTracker(window_s=300, baseline_s=3600)
    v.add(T(1, 1000.0))
    assert v.stats("X-USD", 1100.0).ratio is None
    assert v.stats("NONE-USD", 1100.0).window_usd == 0


def test_volume_prune_and_validation():
    v = VolumeTracker(300, 3600)
    v.add(T(1, 0.0))
    v.prune(10_000.0)
    assert v.stats("X-USD", 10_000.0).trades == 0
    with pytest.raises(ValueError):
        VolumeTracker(600, 300)


# market state from real messages ------------------------------------------

def test_live_fixture_replays_cleanly():
    st = MarketState()
    st.l2_allowed = {"DOGE-USD", "XRP-USD", "LINK-USD"}
    for line in FIXTURE.read_text().splitlines():
        st.handle_message(line)
    assert st.bad_messages == 0 and st.sequence_gaps == 0 and not st.server_errors
    assert {"DOGE-USD", "ADA-USD", "XRP-USD", "LINK-USD"} <= set(st.tickers)
    assert st.books and all(b.synced for b in st.books.values())
    assert st.last_heartbeat_at is not None
    assert "heartbeats" in st.subscriptions


def msg(seq, channel="heartbeats", **kw):
    return json.dumps({"channel": channel, "sequence_num": seq, "events": kw.pop("events", [{}]), **kw})


def test_sequence_gap_flags_resync_and_stale_ignored():
    st = MarketState()
    st.handle_message(msg(1))
    st.handle_message(msg(2))
    st.handle_message(msg(5))
    assert st.sequence_gaps == 1 and st.take_resync() and not st.take_resync()
    before = st.messages
    st.handle_message(msg(3))  # late duplicate: ignored, not a gap
    assert st.sequence_gaps == 1 and st.expected_seq == 6 and st.messages == before + 1
    st.new_connection()
    st.handle_message(msg(0))  # new connection restarts numbering
    assert st.sequence_gaps == 1


def test_error_message_and_garbage_do_not_raise():
    st = MarketState()
    st.handle_message('{"type":"error","message":"too many L2 streams requested in a single session"}')
    st.handle_message("not json")
    st.handle_message(json.dumps({"channel": "ticker", "events": [{"tickers": [{"product_id": "X-USD", "price": "NaN"}]}]}))
    assert st.server_errors == ["too many L2 streams requested in a single session"]
    assert st.bad_messages == 1 and "X-USD" not in st.tickers


def test_l2_for_unlisted_product_is_ignored():
    st = MarketState()
    event = {"type": "snapshot", "product_id": "X-USD", "updates": [lvl("bid", "1", "1")]}
    st.handle_message(msg(1, "l2_data", events=[event]))
    assert st.books == {}
    st.l2_allowed = {"X-USD"}
    st.handle_message(msg(2, "l2_data", events=[event]))
    assert st.books["X-USD"].synced
