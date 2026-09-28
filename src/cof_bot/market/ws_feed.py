"""Live market data over the Coinbase Advanced WebSocket, via the official SDK.

Why this module wraps the SDK's ``WSClient`` (coinbase-advanced-py 1.8.4,
coinbase/websocket/websocket_base.py, read 2026-09-27):

1. TLS. ``WSBase.open_async`` connects with ``ssl=ssl.SSLContext()``, which
   performs no certificate or hostname verification (measured: verify_mode 0,
   check_hostname False). ``VerifiedWSClient`` overrides only that method to
   use ``ssl.create_default_context()``. The feed is also built without
   credentials: every channel it uses is public, so no JWT is ever sent.
2. Reconnection. The SDK retries 5 times, then parks the failure in a
   background exception, and it cannot see a connection that goes silent.
   ``MarketDataFeed`` disables SDK retry and supervises the connection itself:
   a staleness watchdog (heartbeats arrive every second), unlimited reconnects
   with capped exponential backoff, and resubscription on every reconnect.
3. Integrity. ``sequence_num`` rises by exactly 1 per message on a connection
   (Coinbase WebSocket overview; confirmed live). A gap means dropped
   messages, so every order book is resynced from a fresh snapshot.
4. Order book limit. Coinbase answered a level2 subscription for more than
   30 products on one unauthenticated connection with "too many L2 streams
   requested in a single session" (measured 2026-09-27; unsubscribing frees
   slots). Level2 therefore covers only a capped hot list that callers set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

import websockets
from coinbase.constants import USER_AGENT
from coinbase.websocket import WSClient, WSClientException

from cof_bot.market.order_book import OrderBook
from cof_bot.market.volume import VolumeTracker, trade_from_message

log = logging.getLogger(__name__)

L2_SESSION_LIMIT = 30  # measured 2026-09-27, see module docstring
STREAM_CHANNELS = ("ticker", "market_trades")


class VerifiedWSClient(WSClient):
    """SDK WSClient with certificate verification switched on."""

    async def open_async(self) -> None:
        # Mirrors WSBase.open_async from coinbase-advanced-py 1.8.4 exactly,
        # except for the SSL context. Re-check this override on any SDK upgrade.
        self._ensure_websocket_not_open()
        headers = self._set_headers()
        try:
            self.websocket = await websockets.connect(
                self.base_url,
                open_timeout=self.timeout,
                max_size=self.max_size,
                user_agent_header=USER_AGENT,
                extra_headers=headers,
                ssl=ssl.create_default_context() if self.base_url.startswith("wss://") else None,
            )
            if self.on_open:
                self.on_open()
            if not self._retrying:
                self._task = asyncio.create_task(self._message_handler())
        except asyncio.TimeoutError as exc:
            self.websocket = None
            raise WSClientException("Connection attempt timed out") from exc
        except (websockets.exceptions.WebSocketException, OSError) as exc:
            self.websocket = None
            raise WSClientException(f"Failed to establish WebSocket connection: {exc}") from exc


@dataclass(frozen=True)
class Ticker:
    product_id: str
    price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    volume_24h: Decimal | None
    received_at: float


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


class MarketState:
    """Thread safe store of everything the feed has received.

    ``handle_message`` runs on the SDK's event loop thread and never raises;
    readers take the same lock.
    """

    def __init__(self, volume: VolumeTracker | None = None, clock: Callable[[], float] = time.monotonic):
        self.lock = threading.RLock()
        self.clock = clock
        self.volume = volume or VolumeTracker()
        self.books: dict[str, OrderBook] = {}
        # Only these products may hold a book; late l2 messages for a product
        # already unsubscribed must not re-create it (found in live test 2026-09-27).
        self.l2_allowed: set[str] = set()
        self.tickers: dict[str, Ticker] = {}
        self.subscriptions: dict[str, list[str]] = {}
        self.last_message_at: float | None = None
        self.last_heartbeat_at: float | None = None
        self.expected_seq: int | None = None
        self.sequence_gaps = 0
        self.resync_needed = False
        self.server_errors: list[str] = []
        self.bad_messages = 0
        self.messages = 0

    def new_connection(self) -> None:
        with self.lock:
            self.expected_seq = None
            self.last_message_at = self.clock()
            self.last_heartbeat_at = None
            for book in self.books.values():
                book.reset()

    def handle_message(self, raw: str) -> None:
        try:
            self._handle(json.loads(raw))
        except Exception:  # noqa: BLE001, a bad message must never kill the feed thread
            with self.lock:
                self.bad_messages += 1
            log.exception("Unparseable market data message")

    def _handle(self, msg: dict) -> None:
        now = self.clock()
        with self.lock:
            self.messages += 1
            self.last_message_at = now
            if msg.get("type") == "error":
                text = str(msg.get("message", "unknown error"))
                self.server_errors.append(text)
                del self.server_errors[:-50]
                log.error("Coinbase WebSocket error: %s", text)
                return

            seq = msg.get("sequence_num")
            if isinstance(seq, int):
                if self.expected_seq is not None and seq > self.expected_seq:
                    self.sequence_gaps += 1
                    self.resync_needed = True
                    log.warning("Sequence gap: expected %d, got %d", self.expected_seq, seq)
                if self.expected_seq is None or seq >= self.expected_seq:
                    self.expected_seq = seq + 1
                else:
                    return  # stale or duplicate message; Coinbase says these may be ignored

            channel = msg.get("channel")
            events = msg.get("events") or []
            if channel == "heartbeats":
                self.last_heartbeat_at = now
            elif channel == "ticker":
                for event in events:
                    for t in event.get("tickers") or []:
                        price = _dec(t.get("price"))
                        if t.get("product_id") and price is not None:
                            self.tickers[t["product_id"]] = Ticker(
                                t["product_id"],
                                price,
                                _dec(t.get("best_bid")),
                                _dec(t.get("best_ask")),
                                _dec(t.get("volume_24_h")),
                                now,
                            )
            elif channel == "market_trades":
                for event in events:
                    for raw_trade in event.get("trades") or []:
                        trade = trade_from_message(raw_trade)
                        if trade is not None:
                            self.volume.add(trade)
            elif channel == "l2_data":
                for event in events:
                    pid = event.get("product_id")
                    if not pid or pid not in self.l2_allowed:
                        continue
                    book = self.books.setdefault(pid, OrderBook(pid))
                    book.apply(event.get("type", ""), event.get("updates") or [])
            elif channel == "subscriptions":
                for event in events:
                    subs = event.get("subscriptions")
                    if isinstance(subs, dict):
                        self.subscriptions = {k: list(v) for k, v in subs.items()}

    def seconds_since_message(self) -> float | None:
        with self.lock:
            return None if self.last_message_at is None else self.clock() - self.last_message_at

    def take_resync(self) -> bool:
        with self.lock:
            needed, self.resync_needed = self.resync_needed, False
            return needed


class MarketDataFeed:
    """Supervises one market data connection for a product universe."""

    def __init__(
        self,
        product_ids: Iterable[str],
        state: MarketState,
        *,
        l2_max_products: int = 25,
        stale_after_s: float = 15.0,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
        healthy_after_s: float = 60.0,
        client_factory: Callable[..., WSClient] = VerifiedWSClient,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ):
        self.product_ids = sorted(set(product_ids))
        if not self.product_ids:
            raise ValueError("MarketDataFeed needs at least one product")
        if not 0 <= l2_max_products <= L2_SESSION_LIMIT:
            raise ValueError(f"l2_max_products must be between 0 and {L2_SESSION_LIMIT}")
        self.state = state
        self.l2_max_products = l2_max_products
        self.stale_after_s = stale_after_s
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.healthy_after_s = healthy_after_s
        self._client_factory = client_factory
        self._clock = clock
        self._rng = rng
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._l2_wanted: list[str] = []
        self._l2_active: set[str] = set()
        self.client: WSClient | None = None
        self.connects = 0
        self.failures = 0

    # public API -----------------------------------------------------------

    def set_l2_products(self, product_ids: Iterable[str]) -> list[str]:
        """Choose which products get an order book. Truncated to the cap, order kept."""
        allowed = set(self.product_ids)
        wanted: list[str] = []
        for pid in product_ids:
            if pid in allowed and pid not in wanted:
                wanted.append(pid)
            if len(wanted) >= self.l2_max_products:
                break
        with self._lock:
            self._l2_wanted = wanted
        return wanted

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="market-data-feed", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    # supervision ----------------------------------------------------------

    def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            connected_at = None
            try:
                self._connect()
                connected_at = self._clock()
                self._monitor(connected_at)
            except Exception as exc:  # noqa: BLE001, every failure leads to a reconnect
                self.failures += 1
                log.warning("Market data connection lost: %s", exc)
            finally:
                self._teardown()
            if self._stop.is_set():
                break
            if connected_at is not None and self._clock() - connected_at >= self.healthy_after_s:
                attempt = 0
            delay = min(self.backoff_cap_s, self.backoff_base_s * (2**attempt)) * (0.5 + self._rng() / 2)
            attempt += 1
            log.info("Reconnecting market data in %.1fs (attempt %d)", delay, attempt)
            self._stop.wait(delay)

    def _connect(self) -> None:
        self.state.new_connection()
        with self._lock:
            self._l2_active = set()
        # No api_key/api_secret: public channels only, so no JWT is ever sent.
        self.client = self._client_factory(
            api_key=None,
            api_secret=None,
            on_message=self.state.handle_message,
            retry=False,
            timeout=15,
        )
        self.client.open()
        self.connects += 1
        # Subscribe within 5 seconds or Coinbase disconnects; one channel per message.
        self.client.subscribe([], ["heartbeats"])
        self.client.subscribe(self.product_ids, list(STREAM_CHANNELS))
        self._sync_l2(force=True)
        log.info("Market data connected: %d products", len(self.product_ids))

    def _monitor(self, connected_at: float) -> None:
        while not self._stop.is_set():
            self._stop.wait(1.0)
            self.client.raise_background_exception()
            idle = self.state.seconds_since_message()
            if idle is not None and idle > self.stale_after_s:
                raise WSClientException(f"no market data for {idle:.0f}s")
            if self.state.take_resync():
                self._resync_books()
            self._sync_l2()

    def _sync_l2(self, force: bool = False) -> None:
        with self._lock:
            wanted = set(self._l2_wanted)
            active = set(self._l2_active)
        remove = sorted(active - wanted)
        add = sorted(wanted - active)
        with self.state.lock:
            self.state.l2_allowed = set(wanted)
        if remove:
            self.client.unsubscribe(remove, ["level2"])
            with self.state.lock:
                for pid in remove:
                    self.state.books.pop(pid, None)
        if add:
            self.client.subscribe(add, ["level2"])
        if add or remove or force:
            with self._lock:
                self._l2_active = wanted

    def _resync_books(self) -> None:
        with self._lock:
            active = sorted(self._l2_active)
        if not active:
            return
        log.warning("Resyncing %d order books after a sequence gap", len(active))
        with self.state.lock:
            for pid in active:
                if pid in self.state.books:
                    self.state.books[pid].reset()
        self.client.unsubscribe(active, ["level2"])
        self.client.subscribe(active, ["level2"])

    def _teardown(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        try:
            client.close()
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("close() failed (%s); stopping the SDK loop directly", exc)
        # WSBase.close raises before stopping its loop when the socket is
        # already gone, which would leak the SDK's thread. Stop it here.
        loop = getattr(client, "loop", None)
        thread = getattr(client, "thread", None)
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(5)
            if not loop.is_running():
                loop.close()
