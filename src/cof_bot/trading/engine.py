"""Trading engine: turns Phase 2 signals into positions, under the risk limits.

Order of authority, highest first:

1. Circuit breakers (kill switch, daily drawdown): sell everything, no entries.
2. Entry halts (consecutive losses, order failures, trades per day, open
   position cap).
3. Market quality gates (fresh price, synced book, spread).
4. Sizing, which can only shrink or refuse an entry.
5. The signal itself.

Each decision is reported as one JSON record through ``emit``: ``entry``,
``exit``, ``skip`` (with the reason), ``breaker``, ``new_day``, ``error``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Callable

from cof_bot.errors import CofBotError
from cof_bot.exchange.universe import UsdPair
from cof_bot.market.order_book import BookStats
from cof_bot.market.ws_feed import MarketState
from cof_bot.risk.guard import GuardEvent, RiskGuard
from cof_bot.risk.limits import RiskLimits
from cof_bot.risk.sizing import round_down, size_entry
from cof_bot.signals.correlator import Signal
from cof_bot.trading.executor import Fill
from cof_bot.trading.position import Position
from cof_bot.trading.store import StateStore

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        *,
        limits: RiskLimits,
        guard: RiskGuard,
        executor,
        pairs: list[UsdPair],
        state: MarketState,
        cash_usd: Decimal,
        store: StateStore | None,
        emit: Callable[[dict], None],
    ):
        self.limits = limits
        self.guard = guard
        self.executor = executor
        self.pairs = {p.product_id: p for p in pairs}
        self.state = state
        self.cash = cash_usd
        self.store = store
        self.emit = emit
        self.positions: dict[str, Position] = {}
        self.fee_rate: Decimal = executor.fee_rate

    # State ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self.executor.mode

    @property
    def held_products(self) -> list[str]:
        return sorted(self.positions)

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "cash_usd": str(self.cash),
            "positions": {pid: p.to_dict() for pid, p in self.positions.items()},
            "day": self.guard.state.to_dict() if self.guard.state else None,
            "limits_fingerprint": self.limits.fingerprint(),
        }

    def persist(self) -> None:
        if self.store is not None:
            self.store.save(self.snapshot())

    def restore(self, saved: dict | None, now: float) -> None:
        if not saved:
            return
        if saved.get("mode") != self.mode:
            self.emit({"type": "warning", "at": now,
                       "message": f"state file is from {saved.get('mode')} mode; ignored in {self.mode} mode"})
            return
        for pid, d in (saved.get("positions") or {}).items():
            self.positions[pid] = Position.from_dict(d)
        if self.mode == "shadow" and saved.get("cash_usd") is not None:
            self.cash = Decimal(saved["cash_usd"])
        self.guard.restore(saved.get("day"), now)
        self.emit({"type": "restored", "at": now, "positions": self.held_products, "cash_usd": str(self.cash)})

    # Prices -------------------------------------------------------------------

    def quote(self, product_id: str) -> tuple[BookStats | None, Decimal | None, float | None]:
        """(book stats, best bid, age of the best bid in seconds)."""
        with self.state.lock:
            book = self.state.books.get(product_id)
            stats = book.stats() if book is not None else None
            ticker = self.state.tickers.get(product_id)
            now = self.state.clock()
            last = self.state.last_message_at
        if stats is not None:
            return stats, stats.best_bid, None if last is None else now - last
        if ticker is not None:
            return None, ticker.best_bid or ticker.price, now - ticker.received_at
        return None, None, None

    def equity(self) -> Decimal:
        total = self.cash
        for pid, pos in self.positions.items():
            _, bid, _ = self.quote(pid)
            total += pos.base_size * (bid if bid is not None else pos.entry_price)
        return total

    # Loop ---------------------------------------------------------------------

    def _guard_event(self, event: GuardEvent | None, now: float) -> None:
        if event is not None:
            self.emit({"type": event.kind, "at": now, **event.detail})

    def on_tick(self, now: float) -> None:
        """Roll the day, run the breakers, and check every stop."""
        equity = self.equity()
        self._guard_event(self.guard.roll_day(now, equity), now)
        self._guard_event(self.guard.check(equity), now)
        if self.guard.state.flatten:
            for pid in list(self.positions):
                self._exit(pid, now, f"breaker:{self.guard.state.halted}")
            self.persist()
            return
        changed = False
        for pid in list(self.positions):
            pos = self.positions[pid]
            _, bid, _ = self.quote(pid)
            if bid is None:
                continue
            before = (pos.stop_price, pos.trailing)
            reason = pos.update(bid, self.limits)
            if reason:
                self._exit(pid, now, reason)
                changed = True
            elif (pos.stop_price, pos.trailing) != before:
                changed = True
        if changed:
            self.persist()

    def skip(self, signal: Signal, now: float, reason: str, **extra) -> None:
        self.emit({"type": "skip", "at": now, "product_id": signal.product_id, "reason": reason, **extra})

    def on_signal(self, signal: Signal, now: float) -> Fill | None:
        pid = signal.product_id
        if self.guard.state is None:
            self._guard_event(self.guard.roll_day(now, self.equity()), now)
        blocked = self.guard.can_enter(len(self.positions))
        if blocked:
            return self.skip(signal, now, blocked)
        if pid in self.positions:
            return self.skip(signal, now, "position already open")
        pair = self.pairs.get(pid)
        if pair is None:
            return self.skip(signal, now, "product not in the tradable universe")
        stats, _, age = self.quote(pid)
        if stats is None:
            return self.skip(signal, now, "no synced order book")
        if age is None or age > self.limits.max_price_age_s:
            return self.skip(signal, now, "market data stale", age_s=age)
        if stats.spread_bps > self.limits.max_spread_bps:
            return self.skip(signal, now, "spread too wide", spread_bps=str(round(stats.spread_bps, 2)))

        equity = self.equity()
        decision = size_entry(
            limits=self.limits,
            equity_usd=equity,
            cash_usd=self.cash,
            best_ask=stats.best_ask,
            ask_depth_usd=stats.ask_depth_usd,
            fee_rate=self.fee_rate,
            drawdown_headroom_usd=self.guard.headroom_usd(equity),
            base_increment=pair.base_increment,
            quote_increment=pair.quote_increment,
            base_min_size=pair.base_min_size,
            quote_min_size=pair.quote_min_size,
        )
        if not decision.ok:
            return self.skip(signal, now, decision.reason)

        try:
            fill = self.executor.buy(pid, decision.base_size, decision.limit_price)
        except CofBotError as exc:
            self.emit({"type": "error", "at": now, "product_id": pid, "action": "buy", "message": str(exc)})
            self._guard_event(self.guard.record_order_failure(), now)
            self.persist()
            return None
        if fill.base_size <= 0:
            return self.skip(signal, now, f"entry not filled: {fill.note or fill.status}")

        self.cash -= fill.quote_usd + fill.fees_usd
        self.positions[pid] = Position.open(
            product_id=pid,
            base_currency=pair.base_currency,
            base_size=fill.base_size,
            entry_price=fill.avg_price,
            entry_fees_usd=fill.fees_usd,
            opened_at=now,
            limits=self.limits,
            entry_order_id=fill.order_id,
        )
        self.guard.record_entry()
        pos = self.positions[pid]
        self.emit({
            "type": "entry",
            "at": now,
            "mode": self.mode,
            "product_id": pid,
            "fill": fill.to_dict(),
            "sizing": {"binding": decision.binding, "limit_price": str(decision.limit_price),
                       "notional_usd": str(round(decision.notional_usd, 2)),
                       "max_loss_usd": str(round(decision.max_loss_usd, 2)), "equity_usd": str(round(equity, 2))},
            "stop_price": str(pos.stop_price),
            "signal_strength": signal.strength,
            "headline_ids": list(signal.headline_ids),
        })
        self.persist()
        return fill

    def _exit(self, pid: str, now: float, reason: str) -> Fill | None:
        pos = self.positions[pid]
        pair = self.pairs.get(pid)
        size = round_down(pos.base_size, pair.base_increment) if pair else pos.base_size
        try:
            fill = self.executor.sell(pid, size)
        except CofBotError as exc:
            # The position stays open and is retried on the next tick.
            self.emit({"type": "error", "at": now, "product_id": pid, "action": "sell", "reason": reason,
                       "message": str(exc)})
            self._guard_event(self.guard.record_order_failure(), now)
            return None
        proceeds = fill.quote_usd - fill.fees_usd
        cost = fill.base_size * pos.entry_price + pos.entry_fees_usd * (fill.base_size / pos.base_size)
        pnl = proceeds - cost
        self.cash += proceeds
        remaining = pos.base_size - fill.base_size
        if pair is not None and remaining >= pair.base_min_size:
            pos.base_size = remaining
            pos.entry_fees_usd -= pos.entry_fees_usd * (fill.base_size / (fill.base_size + remaining))
        else:
            del self.positions[pid]
        self.emit({
            "type": "exit",
            "at": now,
            "mode": self.mode,
            "product_id": pid,
            "reason": reason,
            "fill": fill.to_dict(),
            "entry_price": str(pos.entry_price),
            "high_water": str(pos.high_water),
            "pnl_usd": str(round(pnl, 2)),
            "held_s": round(now - pos.opened_at, 1),
            "remaining_base": str(remaining) if pid in self.positions else "0",
        })
        self._guard_event(self.guard.record_exit(pnl), now)
        return fill

    def summary(self) -> dict:
        equity = self.equity()
        s = self.guard.state
        return {
            "mode": self.mode,
            "equity_usd": str(round(equity, 2)),
            "cash_usd": str(round(self.cash, 2)),
            "open_positions": {pid: {"size": str(p.base_size), "entry": str(p.entry_price),
                                     "stop": str(p.stop_price), "trailing": p.trailing}
                               for pid, p in self.positions.items()},
            "day": None if s is None else {"day": s.day, "trades": s.trades, "halted": s.halted,
                                           "realised_pnl_usd": str(round(s.realised_pnl, 2)),
                                           "drawdown_pct": str(round(self.guard.drawdown(equity) * 100, 3))},
            "limits_fingerprint": self.limits.fingerprint(),
        }
