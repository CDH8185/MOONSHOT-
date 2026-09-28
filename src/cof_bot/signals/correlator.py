"""Link headline sentiment spikes to volume surges, per product.

A signal fires for a product only when all of these hold at evaluation time:

Sentiment spike (headlines attributed to the product):
* at least ``min_mentions`` mentions in the last ``sentiment_window_s``, and at
  least ``spike_multiple`` times the product's own mention rate over the
  preceding ``mention_baseline_s`` (a coin in the news every hour needs more
  than one headline to count as a spike);
* mean compound score >= ``min_mean_compound``;
* no mention in the window at or below ``negative_veto`` (one "exploit" or
  "lawsuit" headline cancels any amount of positive coverage);
* the newest positive mention is no older than ``max_lag_s``.

Upward volume surge (from market_trades):
* rolling window USD volume >= ``min_window_usd``;
* window volume >= ``volume_ratio_min`` times its own baseline (a product
  with less than a full baseline of observation never qualifies);
* taker buy share >= ``min_buy_share`` and price change over the window > 0.

This module only reports signals. It places no orders; entry logic and every
risk limit belong to Phase 3.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field

from cof_bot.market.order_book import OrderBook
from cof_bot.market.volume import VolumeTracker
from cof_bot.news.feeds import Headline
from cof_bot.sentiment.analyzer import SentimentScore


@dataclass(frozen=True)
class CorrelatorSettings:
    sentiment_window_s: int = 3600
    mention_baseline_s: int = 24 * 3600
    min_mentions: int = 1
    spike_multiple: float = 2.0
    min_mean_compound: float = 0.3
    negative_veto: float = -0.3
    max_lag_s: int = 1800
    volume_ratio_min: float = 2.0
    min_window_usd: float = 5000.0
    min_buy_share: float = 0.55
    cooldown_s: int = 900
    hot_volume_ratio: float = 1.5


@dataclass(frozen=True)
class Mention:
    at: float
    headline_id: str
    source: str
    title: str
    compound: float


@dataclass(frozen=True)
class SentimentState:
    mentions: int
    baseline_per_window: float
    mean_compound: float
    min_compound: float
    newest_positive_at: float | None
    spike: bool
    reason: str


@dataclass(frozen=True)
class Signal:
    product_id: str
    at: float
    mentions: int
    mean_compound: float
    headline_ids: tuple[str, ...]
    titles: tuple[str, ...]
    sources: tuple[str, ...]
    volume_ratio: float
    window_usd: float
    buy_share: float
    price_change_pct: float
    lag_s: float
    strength: float
    spread_bps: float | None = None
    book_imbalance: float | None = None

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


@dataclass
class Evaluation:
    signals: list[Signal] = field(default_factory=list)
    hot_products: list[str] = field(default_factory=list)
    sentiment_spikes: int = 0
    volume_surges: int = 0


class SentimentVolumeCorrelator:
    def __init__(self, settings: CorrelatorSettings | None = None):
        self.settings = settings or CorrelatorSettings()
        self._mentions: dict[str, deque[Mention]] = defaultdict(deque)
        self._seen: set[tuple[str, str]] = set()
        self._last_signal_at: dict[str, float] = {}

    def add(self, headline: Headline, product_ids: list[str], score: SentimentScore) -> int:
        added = 0
        for pid in product_ids:
            key = (pid, headline.id)
            if key in self._seen:
                continue
            self._seen.add(key)
            self._mentions[pid].append(
                Mention(headline.event_time, headline.id, headline.source, headline.title, score.compound)
            )
            added += 1
        return added

    def _prune(self, now: float) -> None:
        horizon = now - self.settings.sentiment_window_s - self.settings.mention_baseline_s
        for pid, dq in self._mentions.items():
            while dq and dq[0].at < horizon:
                old = dq.popleft()
                self._seen.discard((pid, old.headline_id))

    def sentiment_state(self, product_id: str, now: float) -> SentimentState:
        s = self.settings
        win_start = now - s.sentiment_window_s
        base_start = win_start - s.mention_baseline_s
        mentions = self._mentions.get(product_id, ())
        window = [m for m in mentions if win_start <= m.at <= now + 300]
        base_count = sum(1 for m in mentions if base_start <= m.at < win_start)
        baseline = base_count * s.sentiment_window_s / s.mention_baseline_s if s.mention_baseline_s else 0.0
        if not window:
            return SentimentState(0, baseline, 0.0, 0.0, None, False, "no mentions")
        mean = sum(m.compound for m in window) / len(window)
        low = min(m.compound for m in window)
        positives = [m.at for m in window if m.compound >= s.min_mean_compound]
        newest_pos = max(positives) if positives else None

        reason = "ok"
        if len(window) < s.min_mentions:
            reason = "too few mentions"
        elif len(window) < s.spike_multiple * baseline:
            reason = "mentions not above baseline"
        elif low <= s.negative_veto:
            reason = "negative headline veto"
        elif mean < s.min_mean_compound:
            reason = "mean sentiment too low"
        elif newest_pos is None or now - newest_pos > s.max_lag_s:
            reason = "positive news too old"
        return SentimentState(len(window), baseline, mean, low, newest_pos, reason == "ok", reason)

    def evaluate(
        self,
        now: float,
        volume: VolumeTracker,
        books: dict[str, OrderBook] | None = None,
        product_ids: list[str] | None = None,
    ) -> Evaluation:
        s = self.settings
        self._prune(now)
        result = Evaluation()
        candidates = set(product_ids) if product_ids is not None else set(self._mentions)
        hot: list[tuple[float, str]] = []

        for pid in sorted(candidates):
            sent = self.sentiment_state(pid, now)
            vol = volume.stats(pid, now)
            ratio = float(vol.ratio) if vol.ratio is not None else None
            if sent.mentions:
                hot.append((2.0 + sent.mean_compound, pid))
            elif ratio is not None and ratio >= s.hot_volume_ratio:
                hot.append((min(ratio, 10.0) / 10.0, pid))

            surge = (
                ratio is not None
                and ratio >= s.volume_ratio_min
                and float(vol.window_usd) >= s.min_window_usd
                and vol.buy_share is not None
                and float(vol.buy_share) >= s.min_buy_share
                and vol.price_change_pct is not None
                and vol.price_change_pct > 0
            )
            result.sentiment_spikes += sent.spike
            result.volume_surges += surge
            if not (sent.spike and surge):
                continue
            last = self._last_signal_at.get(pid)
            if last is not None and now - last < s.cooldown_s:
                continue

            win = [m for m in self._mentions[pid] if m.at >= now - s.sentiment_window_s]
            spread = imbalance = None
            stats = books[pid].stats() if books and pid in books else None
            if stats is not None:
                spread, imbalance = float(stats.spread_bps), float(stats.imbalance)
            signal = Signal(
                product_id=pid,
                at=now,
                mentions=sent.mentions,
                mean_compound=round(sent.mean_compound, 4),
                headline_ids=tuple(m.headline_id for m in win),
                titles=tuple(m.title for m in win),
                sources=tuple(sorted({m.source for m in win})),
                volume_ratio=round(ratio, 3),
                window_usd=round(float(vol.window_usd), 2),
                buy_share=round(float(vol.buy_share), 4),
                price_change_pct=round(float(vol.price_change_pct), 4),
                lag_s=round(now - sent.newest_positive_at, 1),
                # Documented composite for ranking only: sentiment times log2 of the surge.
                strength=round(sent.mean_compound * math.log2(ratio), 4),
                spread_bps=None if spread is None else round(spread, 2),
                book_imbalance=None if imbalance is None else round(imbalance, 4),
            )
            self._last_signal_at[pid] = now
            result.signals.append(signal)

        hot.sort(reverse=True)
        result.hot_products = [pid for _, pid in hot]
        return result
