"""Rolling USD trade volume per product from the ``market_trades`` channel.

The ticker channel only carries a rolling 24 hour volume, which cannot show a
surge inside minutes, so volume comes from individual trades. Trades are
bucketed by minute of their own timestamp and de duplicated by trade_id (the
subscription snapshot replays recent trades, and a reconnect replays them
again).
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0")


@dataclass(frozen=True)
class Trade:
    product_id: str
    trade_id: str
    price: Decimal
    size: Decimal
    maker_side: str  # Coinbase reports the MAKER's side (market-trades reference)
    ts: float  # epoch seconds

    @property
    def notional(self) -> Decimal:
        return self.price * self.size

    @property
    def taker_buy(self) -> bool:
        # The maker sold, so the aggressor bought.
        return self.maker_side == "SELL"


@dataclass(frozen=True)
class VolumeStats:
    product_id: str
    window_usd: Decimal
    baseline_usd_per_window: Decimal
    ratio: Decimal | None  # None when there is no baseline to compare against
    buy_share: Decimal | None  # share of window USD volume from aggressive (taker) buys
    price_change_pct: Decimal | None
    trades: int


def parse_ts(value: str) -> float | None:
    """Parse Coinbase RFC 3339 timestamps (nanosecond precision allowed)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Python accepts at most 6 fractional digits.
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits):]
        text = f"{head}.{digits[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def trade_from_message(raw: dict) -> Trade | None:
    try:
        price = Decimal(str(raw["price"]))
        size = Decimal(str(raw["size"]))
    except (KeyError, InvalidOperation):
        return None
    ts = parse_ts(raw.get("time", ""))
    if ts is None or not price.is_finite() or not size.is_finite() or price <= 0 or size <= 0:
        return None
    product_id = raw.get("product_id")
    trade_id = raw.get("trade_id")
    if not product_id or not trade_id:
        return None
    return Trade(product_id, str(trade_id), price, size, str(raw.get("side", "")).upper(), ts)


class _Bucket:
    __slots__ = ("usd", "buy_usd", "trades", "first_px", "first_ts", "last_px", "last_ts")

    def __init__(self):
        self.usd = ZERO
        self.buy_usd = ZERO
        self.trades = 0
        self.first_px = self.last_px = None
        self.first_ts = self.last_ts = None

    def add(self, t: Trade) -> None:
        self.usd += t.notional
        if t.taker_buy:
            self.buy_usd += t.notional
        self.trades += 1
        if self.first_ts is None or t.ts < self.first_ts:
            self.first_ts, self.first_px = t.ts, t.price
        if self.last_ts is None or t.ts >= self.last_ts:
            self.last_ts, self.last_px = t.ts, t.price


class VolumeTracker:
    def __init__(self, window_s: int = 300, baseline_s: int = 3600, dedupe_size: int = 200_000):
        if window_s <= 0 or baseline_s < window_s:
            raise ValueError("need 0 < window_s <= baseline_s")
        self.window_s = window_s
        self.baseline_s = baseline_s
        self._buckets: dict[str, dict[int, _Bucket]] = defaultdict(dict)
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._dedupe_size = dedupe_size
        self.first_seen: dict[str, float] = {}

    def add(self, trade: Trade) -> bool:
        key = (trade.product_id, trade.trade_id)
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._dedupe_size:
            self._seen.popitem(last=False)
        minute = int(trade.ts // 60)
        self._buckets[trade.product_id].setdefault(minute, _Bucket()).add(trade)
        self.first_seen.setdefault(trade.product_id, trade.ts)
        return True

    def prune(self, now: float) -> None:
        oldest = int((now - self.window_s - self.baseline_s) // 60) - 1
        for buckets in self._buckets.values():
            for minute in [m for m in buckets if m < oldest]:
                del buckets[minute]

    def stats(self, product_id: str, now: float) -> VolumeStats:
        buckets = self._buckets.get(product_id, {})
        now_min = int(now // 60)
        win_minutes = self.window_s // 60 or 1
        base_minutes = self.baseline_s // 60
        window = [buckets[m] for m in range(now_min - win_minutes + 1, now_min + 1) if m in buckets]
        base = [
            buckets[m]
            for m in range(now_min - win_minutes - base_minutes + 1, now_min - win_minutes + 1)
            if m in buckets
        ]
        window_usd = sum((b.usd for b in window), ZERO)
        buy_usd = sum((b.buy_usd for b in window), ZERO)
        base_usd = sum((b.usd for b in base), ZERO)
        per_window = base_usd * win_minutes / base_minutes if base_minutes else ZERO

        # A baseline only counts once the tracker has seen trades for the full
        # baseline period; otherwise a fresh start reads every pair as a surge.
        first = self.first_seen.get(product_id)
        earliest_baseline_min = now_min - win_minutes - base_minutes + 1
        observed = first is not None and int(first // 60) <= earliest_baseline_min
        ratio = (window_usd / per_window) if observed and per_window > 0 else None

        firsts = [b for b in window if b.first_ts is not None]
        change = None
        if firsts:
            open_b = min(firsts, key=lambda b: b.first_ts)
            close_b = max(firsts, key=lambda b: b.last_ts)
            if open_b.first_px:
                change = (close_b.last_px - open_b.first_px) / open_b.first_px * 100
        return VolumeStats(
            product_id=product_id,
            window_usd=window_usd,
            baseline_usd_per_window=per_window,
            ratio=ratio,
            buy_share=(buy_usd / window_usd) if window_usd > 0 else None,
            price_change_pct=change,
            trades=sum(b.trades for b in window),
        )
