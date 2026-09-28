"""Runtime: live market data + news sentiment -> signals -> (optionally) trades.

Everything is reported as JSON lines. Without a trading engine (the ``stream``
command) nothing here places an order. With one (the ``trade`` command) each
signal goes to the engine, which applies the risk limits and trades in shadow
or live mode, and every evaluation also runs the engine's stops and breakers.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from typing import Callable, TextIO

from cof_bot.config import StreamSettings
from cof_bot.exchange.universe import Universe
from cof_bot.market.volume import VolumeTracker
from cof_bot.market.ws_feed import MarketDataFeed, MarketState
from cof_bot.news.feeds import Headline, NewsCollector
from cof_bot.sentiment.analyzer import HeadlineSentiment
from cof_bot.sentiment.entities import AssetMatcher
from cof_bot.signals.correlator import SentimentVolumeCorrelator

log = logging.getLogger(__name__)


class StreamRunner:
    def __init__(
        self,
        universe: Universe,
        settings: StreamSettings,
        *,
        out: TextIO = sys.stdout,
        clock: Callable[[], float] = time.time,
        feed_factory: Callable[..., MarketDataFeed] = MarketDataFeed,
        collector: NewsCollector | None = None,
        state: MarketState | None = None,
        engine_factory: Callable[["StreamRunner"], object] | None = None,
    ):
        if not universe.pairs:
            raise ValueError("empty universe; nothing to stream")
        self.universe = universe
        self.settings = settings
        self.out = out
        self.clock = clock
        self.volume = VolumeTracker(settings.volume_window_s, settings.volume_baseline_s)
        self.state = state or MarketState(self.volume)
        self.state.volume = self.volume
        self.feed = feed_factory(
            universe.product_ids,
            self.state,
            l2_max_products=settings.l2_max_products,
            stale_after_s=settings.ws_stale_after_s,
            backoff_cap_s=settings.ws_backoff_cap_s,
        )
        self.collector = collector or NewsCollector(
            poll_interval_s=settings.news_poll_s, max_age_s=settings.news_max_age_s
        )
        self.sentiment = HeadlineSentiment()
        self.matcher = AssetMatcher(universe.pairs)
        self.correlator = SentimentVolumeCorrelator(settings.correlator)
        self.headlines: queue.Queue[Headline] = queue.Queue()
        self.stop_event = threading.Event()
        self.signals_emitted = 0
        self.engine = engine_factory(self) if engine_factory else None

    def emit(self, record: dict) -> None:
        self.out.write(json.dumps(record, default=str) + "\n")
        self.out.flush()

    def _news_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = self.collector.poll()
                for h in result.new:
                    self.headlines.put(h)
            except Exception:  # noqa: BLE001, news failure must not stop market data
                log.exception("News poll failed")
            self.stop_event.wait(5.0)

    def process_headlines(self) -> int:
        n = 0
        while True:
            try:
                h = self.headlines.get_nowait()
            except queue.Empty:
                return n
            score = self.sentiment.score(h.title)
            products = self.matcher.match(h.title)
            self.correlator.add(h, products, score)
            n += 1
            self.emit(
                {
                    "type": "headline",
                    "at": h.event_time,
                    "source": h.source,
                    "id": h.id,
                    "title": h.title,
                    "compound": score.compound,
                    "label": score.label,
                    "products": products,
                }
            )

    def evaluate(self) -> None:
        now = self.clock()
        with self.state.lock:
            result = self.correlator.evaluate(now, self.volume, self.state.books, self.universe.product_ids)
            self.volume.prune(now)
        for sig in result.signals:
            self.signals_emitted += 1
            self.emit({"type": "signal", **sig.to_dict()})
        hot = result.hot_products
        if self.engine is not None:
            # Stops and breakers first, so a tripped breaker also blocks this round's entries.
            self.engine.on_tick(now)
            for sig in sorted(result.signals, key=lambda s: s.strength, reverse=True):
                self.engine.on_signal(sig, now)
            # Held products keep their order books so exits are priced from the book.
            held = self.engine.held_products
            hot = held + [p for p in hot if p not in held]
        self.feed.set_l2_products(hot)
        return result

    def status(self, result) -> None:
        s = self.state
        self.emit(
            {
                "type": "status",
                "at": self.clock(),
                "ws_connects": self.feed.connects,
                "ws_failures": self.feed.failures,
                "messages": s.messages,
                "sequence_gaps": s.sequence_gaps,
                "bad_messages": s.bad_messages,
                "server_errors": s.server_errors[-3:],
                "books_synced": sum(1 for b in s.books.values() if b.synced),
                "sentiment_spikes": result.sentiment_spikes,
                "volume_surges": result.volume_surges,
                "signals_total": self.signals_emitted,
                **({"trading": self.engine.summary()} if self.engine is not None else {}),
            }
        )

    def run(self, duration_s: float | None = None, status_every_s: float = 60.0) -> None:
        started = self.clock()
        self.feed.start()
        news = threading.Thread(target=self._news_loop, name="news-poller", daemon=True)
        news.start()
        last_status = started
        try:
            while not self.stop_event.is_set():
                self.process_headlines()
                result = self.evaluate()
                now = self.clock()
                if now - last_status >= status_every_s:
                    self.status(result)
                    last_status = now
                if duration_s is not None and now - started >= duration_s:
                    self.status(result)
                    break
                self.stop_event.wait(self.settings.eval_interval_s)
        finally:
            self.stop_event.set()
            self.feed.stop()
            news.join(timeout=20)
