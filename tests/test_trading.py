import io
import json
from decimal import Decimal as D

import pytest

from cof_bot.cli import main
from cof_bot.config import load_settings, load_trade_settings
from cof_bot.errors import ConfigError, CredentialError, ExchangeError
from cof_bot.exchange.client import CoinbaseGateway
from cof_bot.exchange.universe import UsdPair
from cof_bot.market.order_book import OrderBook
from cof_bot.market.ws_feed import MarketState, Ticker
from cof_bot.risk.guard import RiskGuard
from cof_bot.risk.limits import RiskLimits
from cof_bot.signals.correlator import Signal
from cof_bot.trading.engine import TradingEngine
from cof_bot.trading.executor import LiveExecutor, ShadowExecutor
from cof_bot.trading.store import StateStore
from conftest import make_http_error

L = RiskLimits()
PID = "ABC-USD"
NOW = 1_790_000_000.0  # 2026-09-21, a fixed wall clock for the engine


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def market(bids=(("0.99", "5000"),), asks=(("1.00", "5000"),)):
    clock = Clock()
    state = MarketState(clock=clock)
    book = OrderBook(PID)
    book.apply("snapshot", [{"side": "bid", "price_level": p, "new_quantity": q} for p, q in bids]
               + [{"side": "offer", "price_level": p, "new_quantity": q} for p, q in asks])
    state.books[PID] = book
    state.last_message_at = clock()
    return state, clock


def set_book(state, bids, asks):
    book = OrderBook(PID)
    book.apply("snapshot", [{"side": "bid", "price_level": p, "new_quantity": q} for p, q in bids]
               + [{"side": "offer", "price_level": p, "new_quantity": q} for p, q in asks])
    state.books[PID] = book


PAIR = UsdPair(PID, "ABC", "Abc Token", "ABC", D("0.1"), D("0.0001"), D("1"), D("1"), D("1"), D("500000"))


def signal(strength=0.5):
    return Signal(PID, NOW, 2, 0.6, ("h1",), ("ABC surges",), ("src",), 3.0, 9000.0, 0.7, 2.0, 60.0, strength)


def engine(state, tmp_path, cash="10000", executor=None):
    events = []
    eng = TradingEngine(limits=L, guard=RiskGuard(L, tmp_path / "KILL"),
                        executor=executor or ShadowExecutor(state, D("0.006")), pairs=[PAIR], state=state,
                        cash_usd=D(cash), store=StateStore(tmp_path / "state.json"), emit=events.append)
    return eng, events


# Shadow executor -------------------------------------------------------------------


def test_shadow_buy_walks_asks_up_to_limit():
    state, _ = market(asks=(("1.00", "10"), ("1.005", "10"), ("1.02", "100")))
    fill = ShadowExecutor(state, D("0.01")).buy(PID, D("50"), D("1.0075"))
    assert fill.base_size == D("20") and fill.avg_price == D("1.0025")
    assert fill.fees_usd == D("20.05") * D("0.01") and fill.note == "partial fill"


def test_shadow_buy_nothing_under_limit():
    state, _ = market(asks=(("1.10", "10"),))
    fill = ShadowExecutor(state, D("0.01")).buy(PID, D("5"), D("1.05"))
    assert fill.base_size == 0 and fill.status == "CANCELLED"


def test_shadow_sell_exhausts_depth():
    state, _ = market(bids=(("0.99", "10"), ("0.98", "10")))
    fill = ShadowExecutor(state, D("0")).sell(PID, D("30"))
    assert fill.base_size == D("30") and fill.avg_price == (D("9.9") + D("9.8") + D("9.8")) / 30
    assert "exhausted" in fill.note


def test_shadow_uses_ticker_when_no_book():
    state = MarketState()
    state.tickers[PID] = Ticker(PID, D("2"), D("1.99"), D("2.01"), None, 0.0)
    fill = ShadowExecutor(state, D("0")).sell(PID, D("3"))
    assert fill.avg_price == D("1.99")


# Gateway order calls ---------------------------------------------------------------


class OrderClient:
    def __init__(self, **kw):
        self.calls = []
        self.fail = []
        self.status = ["OPEN", "FILLED"]

    def limit_order_ioc_buy(self, client_order_id, product_id, base_size, limit_price):
        self.calls.append(("buy", client_order_id, product_id, base_size, limit_price))
        if self.fail:
            raise self.fail.pop(0)
        return {"success": True, "success_response": {"order_id": "o-1"}}

    def market_order_sell(self, client_order_id, product_id, base_size):
        self.calls.append(("sell", client_order_id, product_id, base_size))
        return {"success": False, "error_response": {"error": "INSUFFICIENT_FUND", "message": "Insufficient balance"}}

    def get_order(self, order_id):
        return {"order": {"status": self.status.pop(0) if len(self.status) > 1 else self.status[0],
                          "filled_size": "10", "average_filled_price": "1.001", "total_fees": "0.06"}}

    def cancel_orders(self, ids):
        self.calls.append(("cancel", ids))

    def get_transaction_summary(self, **kw):
        return {"fee_tier": {"taker_fee_rate": "0.006", "maker_fee_rate": "0.004"}}


CREDS = {"COINBASE_API_KEY": "organizations/o/apiKeys/k1", "COINBASE_API_SECRET": "s", "COF_REST_MAX_RPS": "1000"}


def gw(mode):
    client = OrderClient()
    g = CoinbaseGateway(load_settings({**CREDS, "COF_TRADING_MODE": mode}), client_factory=lambda **kw: client,
                        sleep=lambda s: None)
    return g, client


def test_gateway_refuses_orders_outside_live_mode():
    g, client = gw("shadow")
    with pytest.raises(CredentialError):
        g.place_limit_ioc_buy("c", PID, D("1"), D("1"))
    assert client.calls == []


def test_gateway_retries_create_with_same_client_order_id():
    g, client = gw("live")
    client.fail = [make_http_error(503)]
    assert g.place_limit_ioc_buy("cid-1", PID, D("10.0"), D("1.0075")) == "o-1"
    assert [c[1] for c in client.calls] == ["cid-1", "cid-1"]
    assert client.calls[0][3:] == ("10.0", "1.0075")


def test_gateway_rejected_order_and_fee_rate():
    g, _ = gw("live")
    with pytest.raises(ExchangeError, match="Insufficient balance"):
        g.place_market_sell("c", PID, D("1"))
    assert g.taker_fee_rate() == D("0.006")
    assert g.get_order("o-1")["status"] == "OPEN"


# Live executor ---------------------------------------------------------------------


class FakeGateway:
    def __init__(self, statuses, filled="10"):
        self.statuses = list(statuses)
        self.filled = filled
        self.cancelled = []

    def place_limit_ioc_buy(self, cid, pid, size, price):
        return "o-1"

    def place_market_sell(self, cid, pid, size):
        return "o-2"

    def get_order(self, oid):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "filled_size": D(self.filled), "average_filled_price": D("1.001"),
                "total_fees": D("0.06"), "filled_value": D("10.01")}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self.statuses = ["CANCELLED"]


def test_live_executor_waits_for_terminal_status():
    ex = LiveExecutor(FakeGateway(["PENDING", "OPEN", "FILLED"]), D("0.006"), sleep=lambda s: None)
    fill = ex.buy(PID, D("10"), D("1.01"))
    assert fill.status == "FILLED" and fill.base_size == D("10") and fill.order_id == "o-1"


def test_live_executor_cancels_stuck_ioc():
    gwy = FakeGateway(["OPEN"], filled="4")
    ex = LiveExecutor(gwy, D("0.006"), sleep=lambda s: None, max_polls=2)
    fill = ex.buy(PID, D("10"), D("1.01"))
    assert gwy.cancelled == ["o-1"] and fill.base_size == D("4") and fill.status == "CANCELLED"


def test_live_executor_sell_that_fills_nothing_raises():
    ex = LiveExecutor(FakeGateway(["FAILED"], filled="0"), D("0.006"), sleep=lambda s: None)
    with pytest.raises(ExchangeError):
        ex.sell(PID, D("10"))


# Engine ----------------------------------------------------------------------------


def test_entry_then_hard_stop_exit(tmp_path):
    state, _ = market()
    eng, events = engine(state, tmp_path)
    fill = eng.on_signal(signal(), NOW)
    assert fill is not None and PID in eng.positions
    entry = events[-1]
    assert entry["type"] == "entry" and entry["sizing"]["binding"] == "risk_budget"
    pos = eng.positions[PID]
    assert pos.stop_price == pos.entry_price * D("0.94")
    assert json.loads((tmp_path / "state.json").read_text())["positions"][PID]["base_size"] == str(pos.base_size)

    set_book(state, bids=(("0.94", "100000"),), asks=(("0.95", "100000"),))
    eng.on_tick(NOW + 60)
    exit_ = [e for e in events if e["type"] == "exit"][0]
    assert exit_["reason"] == "hard_stop" and D(exit_["pnl_usd"]) < 0
    assert eng.positions == {}
    # Sold at the stop, the loss stays inside the risk budget: 0.5% of 10,000.
    assert -D(exit_["pnl_usd"]) <= D("50")


def test_gap_through_stop_can_exceed_budget(tmp_path):
    # Known limit: a stop is a trigger, not a guaranteed price. A gap from
    # above the stop to below it sells at the lower bid.
    state, _ = market()
    eng, events = engine(state, tmp_path)
    eng.on_signal(signal(), NOW)
    set_book(state, bids=(("0.90", "100000"),), asks=(("0.91", "100000"),))
    eng.on_tick(NOW + 60)
    exit_ = [e for e in events if e["type"] == "exit"][0]
    assert exit_["reason"] == "hard_stop" and -D(exit_["pnl_usd"]) > D("50")


def test_infinity_trailing_exit_in_profit(tmp_path):
    state, _ = market()
    eng, events = engine(state, tmp_path)
    eng.on_signal(signal(), NOW)
    for bid in ("1.10", "1.50", "2.00"):
        set_book(state, bids=((bid, "100000"),), asks=((str(D(bid) + D("0.01")), "100000"),))
        eng.on_tick(NOW + 60)
    assert eng.positions[PID].trailing and eng.positions[PID].stop_price == D("1.90")
    set_book(state, bids=(("1.89", "100000"),), asks=(("1.90", "100000"),))
    eng.on_tick(NOW + 120)
    exit_ = [e for e in events if e["type"] == "exit"][0]
    assert exit_["reason"] == "trailing_stop" and D(exit_["pnl_usd"]) > 0


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda st, clk: st.books.clear(), "no synced order book"),
        (lambda st, clk: setattr(clk, "t", clk.t + 60), "market data stale"),
        (lambda st, clk: set_book(st, (("0.90", "5000"),), (("1.00", "5000"),)), "spread too wide"),
    ],
)
def test_entry_gates(tmp_path, setup, reason):
    state, clock = market()
    eng, events = engine(state, tmp_path)
    setup(state, clock)
    assert eng.on_signal(signal(), NOW) is None
    assert events[-1]["type"] == "skip" and events[-1]["reason"] == reason


def test_duplicate_signal_and_halt_block_entries(tmp_path):
    state, _ = market()
    eng, events = engine(state, tmp_path)
    eng.on_signal(signal(), NOW)
    eng.on_signal(signal(), NOW + 1)
    assert events[-1]["reason"] == "position already open"
    (tmp_path / "KILL").write_text("")
    eng.on_tick(NOW + 2)
    assert any(e["type"] == "breaker" and e["reason"] == "kill_switch" for e in events)
    assert eng.positions == {}  # kill switch sells everything


def test_daily_drawdown_flattens(tmp_path):
    state, _ = market()
    eng, events = engine(state, tmp_path, cash="1000")
    eng.on_tick(NOW)
    eng.cash -= D("40")  # an outside loss of 4% of the day's equity
    eng.on_tick(NOW + 1)
    assert events[-1]["type"] == "breaker" and events[-1]["reason"] == "daily_drawdown"
    eng.on_signal(signal(), NOW + 2)
    assert events[-1]["reason"] == "halted: daily_drawdown"


def test_buy_failure_counts_toward_breaker(tmp_path):
    state, _ = market()

    class Broken:
        mode, fee_rate = "shadow", D("0.006")

        def buy(self, *a):
            raise ExchangeError("503 after retries")

    eng, events = engine(state, tmp_path, executor=Broken())
    for i in range(3):
        eng.on_signal(signal(), NOW + i)
    assert events[-1]["type"] == "breaker" and events[-1]["reason"] == "order_failures"


def test_restore_and_mode_mismatch(tmp_path):
    state, _ = market()
    eng, _ = engine(state, tmp_path)
    eng.on_signal(signal(), NOW)
    saved = StateStore(tmp_path / "state.json").load()
    eng2, events = engine(state, tmp_path)
    eng2.restore(saved, NOW + 10)
    assert PID in eng2.positions and eng2.cash == eng.cash and eng2.guard.state.trades == 1

    class LiveLike(ShadowExecutor):
        mode = "live"

    eng3, events3 = engine(state, tmp_path, executor=LiveLike(state, D("0.006")))
    eng3.restore(saved, NOW + 10)
    assert eng3.positions == {} and events3[-1]["type"] == "warning"


# Runner and CLI --------------------------------------------------------------------


def test_runner_feeds_engine_and_keeps_held_books(tmp_path):
    from cof_bot.config import load_stream_settings
    from cof_bot.exchange.universe import Universe
    from cof_bot.runtime import StreamRunner
    from test_stream import FakeCollector, FakeFeed

    state, _ = market()
    out = io.StringIO()

    def factory(runner):
        eng = TradingEngine(limits=L, guard=RiskGuard(L, tmp_path / "KILL"), executor=ShadowExecutor(state, D("0.006")),
                            pairs=[PAIR], state=runner.state, cash_usd=D("10000"), store=None, emit=runner.emit)
        eng.on_signal(signal(), NOW)
        return eng

    runner = StreamRunner(Universe([PAIR], True, "test", 1), load_stream_settings({}), out=out, clock=lambda: NOW,
                          feed_factory=FakeFeed, collector=FakeCollector([]), state=state, engine_factory=factory)
    result = runner.evaluate()
    assert runner.feed.l2[0] == PID
    runner.status(result)
    status = json.loads(out.getvalue().splitlines()[-1])
    assert PID in status["trading"]["open_positions"]


def test_trade_command_needs_both_live_switches(monkeypatch, capsys):
    monkeypatch.setenv("COF_TRADING_MODE", "shadow")
    assert main(["--env-file", "/nonexistent", "trade", "--live"]) == 2
    assert "BOTH" in capsys.readouterr().err
    monkeypatch.setenv("COF_TRADING_MODE", "live")
    monkeypatch.setenv("COINBASE_API_KEY", "")
    assert main(["--env-file", "/nonexistent", "trade"]) == 2


def test_trade_settings():
    t = load_trade_settings({})
    assert t.shadow_start_usd == D("1000") and t.state_file("shadow").endswith("state_shadow.json")
    with pytest.raises(ConfigError):
        load_trade_settings({"COF_SHADOW_FEE_RATE": "1.2"})
