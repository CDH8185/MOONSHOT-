import ssl
import threading
import time

import pytest
from coinbase.websocket import WSClientConnectionClosedException

from cof_bot.market.ws_feed import L2_SESSION_LIMIT, MarketDataFeed, MarketState, VerifiedWSClient


class FakeWS:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.sent = []
        self.closed = False
        self.fail_open = False
        self.background = None
        self.close_raises = False
        FakeWS.instances.append(self)

    def open(self):
        if self.fail_open:
            raise RuntimeError("cannot connect")

    def subscribe(self, product_ids, channels):
        for c in channels:
            self.sent.append(("sub", c, tuple(product_ids)))

    def unsubscribe(self, product_ids, channels):
        for c in channels:
            self.sent.append(("unsub", c, tuple(product_ids)))

    def raise_background_exception(self):
        if self.background:
            exc, self.background = self.background, None
            raise exc

    def close(self):
        if self.close_raises:
            raise RuntimeError("already closed")
        self.closed = True


@pytest.fixture(autouse=True)
def reset():
    FakeWS.instances = []


def make_feed(**kw):
    st = MarketState()
    feed = MarketDataFeed(["A-USD", "B-USD", "C-USD"], st, client_factory=FakeWS, backoff_base_s=0.01, backoff_cap_s=0.02, rng=lambda: 1.0, **kw)
    return feed, st


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_verified_client_uses_certificate_checking(monkeypatch):
    captured = {}

    async def fake_connect(url, **kw):
        captured.update(kw)
        raise OSError("stop here")

    monkeypatch.setattr("cof_bot.market.ws_feed.websockets.connect", fake_connect)
    client = VerifiedWSClient(on_message=lambda m: None, retry=False)
    with pytest.raises(Exception):
        client.open()
    ctx = captured["ssl"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    client.loop.call_soon_threadsafe(client.loop.stop)


def test_connect_subscribes_without_credentials():
    feed, st = make_feed()
    feed.set_l2_products(["B-USD", "ZZZ-USD", "B-USD"])
    feed._connect()
    ws = FakeWS.instances[0]
    assert ws.kwargs["api_key"] is None and ws.kwargs["api_secret"] is None and ws.kwargs["retry"] is False
    assert ws.sent[0] == ("sub", "heartbeats", ())
    assert ("sub", "ticker", ("A-USD", "B-USD", "C-USD")) in ws.sent
    assert ("sub", "market_trades", ("A-USD", "B-USD", "C-USD")) in ws.sent
    assert ("sub", "level2", ("B-USD",)) in ws.sent  # unknown product dropped, dupes removed
    assert st.l2_allowed == {"B-USD"}


def test_l2_diff_and_cap():
    feed, st = make_feed(l2_max_products=2)
    assert feed.set_l2_products(["A-USD", "B-USD", "C-USD"]) == ["A-USD", "B-USD"]
    feed._connect()
    ws = FakeWS.instances[0]
    feed.set_l2_products(["C-USD", "A-USD"])
    feed._sync_l2()
    assert ("unsub", "level2", ("B-USD",)) in ws.sent and ("sub", "level2", ("C-USD",)) in ws.sent
    assert st.l2_allowed == {"A-USD", "C-USD"}
    with pytest.raises(ValueError):
        MarketDataFeed(["A-USD"], st, l2_max_products=L2_SESSION_LIMIT + 1)
    with pytest.raises(ValueError):
        MarketDataFeed([], st)


def test_resync_resubscribes_books():
    feed, st = make_feed()
    feed.set_l2_products(["A-USD"])
    feed._connect()
    ws = FakeWS.instances[0]
    feed._resync_books()
    assert ws.sent[-2:] == [("unsub", "level2", ("A-USD",)), ("sub", "level2", ("A-USD",))]


def test_background_exception_triggers_reconnect_and_resubscribe():
    feed, st = make_feed(stale_after_s=100)
    feed.start()
    assert wait_for(lambda: feed.connects == 1)
    FakeWS.instances[0].background = WSClientConnectionClosedException("gone")
    assert wait_for(lambda: feed.connects == 2)
    feed.stop()
    assert feed.failures >= 1
    assert FakeWS.instances[0].closed
    assert ("sub", "ticker", ("A-USD", "B-USD", "C-USD")) in FakeWS.instances[1].sent


def test_silent_connection_is_detected_by_watchdog():
    now = [0.0]
    st = MarketState(clock=lambda: now[0])
    feed = MarketDataFeed(["A-USD"], st, client_factory=FakeWS, stale_after_s=5, backoff_base_s=0.01, backoff_cap_s=0.01)
    feed.start()
    assert wait_for(lambda: feed.connects == 1)
    now[0] = 100.0  # no message for 100 s
    assert wait_for(lambda: feed.connects == 2, timeout=6)
    feed.stop()


def test_failed_open_backs_off_and_retries_forever():
    feed, st = make_feed()
    orig = FakeWS.__init__

    def failing(self, **kw):
        orig(self, **kw)
        self.fail_open = len(FakeWS.instances) <= 3

    FakeWS.__init__ = failing
    try:
        feed.start()
        assert wait_for(lambda: feed.connects == 1)
        feed.stop()
    finally:
        FakeWS.__init__ = orig
    assert feed.failures >= 3


def test_teardown_stops_sdk_loop_when_close_fails():
    import asyncio

    feed, st = make_feed()
    feed._connect()
    ws = FakeWS.instances[0]
    ws.close_raises = True
    ws.loop = asyncio.new_event_loop()
    ws.thread = threading.Thread(target=ws.loop.run_forever, daemon=True)
    ws.thread.start()
    feed._teardown()
    assert not ws.thread.is_alive() and ws.loop.is_closed() and feed.client is None
