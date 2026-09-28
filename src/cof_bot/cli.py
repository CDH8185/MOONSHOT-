"""Command line entry point.

    python -m cof_bot verify     check credentials, permissions and balances
    python -m cof_bot universe   list the compliant USD spot pairs
    python -m cof_bot headlines  score and attribute current news headlines
    python -m cof_bot stream     live signals from sentiment and volume (no orders)
    python -m cof_bot trade      signals plus the Phase 3 engine (shadow unless --live)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from cof_bot.config import load_settings, load_stream_settings
from cof_bot.errors import CofBotError
from cof_bot.exchange.client import CoinbaseGateway


def _cmd_verify(gateway: CoinbaseGateway, args) -> int:
    report = gateway.verify_access()
    nonzero = [b for b in report.balances if b.available or b.hold]
    print(f"API key        : {report.key}")
    print(f"Permissions    : view={report.can_view} trade={report.can_trade} transfer={report.can_transfer}")
    print(f"Portfolio type : {report.portfolio_type}")
    print(f"Accounts       : {len(report.balances)} ({len(nonzero)} with a balance)")
    print(f"USD available  : {report.usd_available}")
    for warning in report.warnings:
        print(f"WARNING        : {warning}")
    universe = gateway.fetch_universe()
    print(f"USD universe   : {len(universe.pairs)} pairs from {universe.source}")
    print("RESULT         : account access verified")
    return 0


def _cmd_universe(gateway: CoinbaseGateway, args) -> int:
    universe = gateway.fetch_universe()
    if args.json:
        print(
            json.dumps(
                {
                    "source": universe.source,
                    "authoritative": universe.authoritative,
                    "total_products": universe.total_products,
                    "rejected": dict(universe.rejected),
                    "product_ids": universe.product_ids,
                },
                indent=2,
            )
        )
        return 0
    if not universe.authoritative:
        print(
            "NOTICE: no credentials, so this is Coinbase's public catalogue, not the list your "
            "account may trade. It is never used for live trading."
        )
    print(f"{len(universe.pairs)} of {universe.total_products} products qualify ({universe.source})")
    for reason, count in sorted(universe.rejected.items()):
        print(f"  rejected {reason}: {count}")
    for pair in universe.pairs:
        print(f"  {pair.product_id:<14} 24h USD volume {pair.quote_volume_24h}")
    return 0


def _cmd_headlines(gateway: CoinbaseGateway, args) -> int:
    from cof_bot.news.feeds import NewsCollector
    from cof_bot.sentiment.analyzer import HeadlineSentiment
    from cof_bot.sentiment.entities import AssetMatcher

    stream = load_stream_settings()
    universe = gateway.fetch_universe()
    result = NewsCollector(max_age_s=stream.news_max_age_s).poll(force=True)
    analyzer, matcher = HeadlineSentiment(), AssetMatcher(universe.pairs)
    for name, err in sorted(result.errors.items()):
        print(f"FEED ERROR {name}: {err}", file=sys.stderr)
    for h in result.new:
        score = analyzer.score(h.title)
        products = matcher.match(h.title)
        if args.matched_only and not products:
            continue
        print(f"{score.compound:+.3f} {score.label:<8} {','.join(products) or '-':<18} [{h.source}] {h.title}")
    return 0


def _cmd_stream(gateway: CoinbaseGateway, args) -> int:
    from cof_bot.runtime import StreamRunner

    universe = gateway.fetch_universe()
    if not universe.authoritative:
        print("NOTICE: streaming the public product catalogue (no credentials). Signals only.", file=sys.stderr)
    runner = StreamRunner(universe, load_stream_settings())
    try:
        runner.run(duration_s=args.duration)
    except KeyboardInterrupt:
        runner.stop_event.set()
    return 0


def _live_preflight(gateway: CoinbaseGateway, settings, universe):
    """Every check live trading needs. Raises on the first failure."""
    from decimal import Decimal

    from cof_bot.errors import ConfigError, CredentialError

    report = gateway.verify_access()  # refuses a key with transfer permission
    if not report.can_trade:
        raise CredentialError("Live trading needs an API key with the TRADE permission.")
    if not universe.authoritative:
        raise ConfigError("Live trading needs the account's own product list (authenticated).")
    fee = gateway.taker_fee_rate()
    if fee is None:
        raise ConfigError("Could not read the account's taker fee rate; live trading will not guess it.")
    return report.usd_available, Decimal(fee), report.balances


def _reconcile(engine, balances, now) -> None:
    """Match stored live positions to what the account actually holds."""
    held = {b.currency: b.available + b.hold for b in balances}
    for pid in list(engine.positions):
        pos = engine.positions[pid]
        actual = held.get(pos.base_currency)
        if actual is None or actual <= 0:
            del engine.positions[pid]
            engine.emit({"type": "reconcile", "at": now, "product_id": pid, "action": "dropped",
                         "message": "stored position not found in the account"})
        elif actual < pos.base_size:
            engine.emit({"type": "reconcile", "at": now, "product_id": pid, "action": "resized",
                         "stored": str(pos.base_size), "account": str(actual)})
            pos.base_size = actual
    engine.persist()


def _cmd_trade(gateway: CoinbaseGateway, args) -> int:
    import time
    from pathlib import Path

    from cof_bot.config import load_trade_settings
    from cof_bot.errors import ConfigError
    from cof_bot.risk.guard import RiskGuard
    from cof_bot.risk.limits import load_risk_limits
    from cof_bot.runtime import StreamRunner
    from cof_bot.trading.engine import TradingEngine
    from cof_bot.trading.executor import LiveExecutor, ShadowExecutor
    from cof_bot.trading.store import StateStore

    settings = gateway.settings
    live = settings.trading_mode == "live"
    if live != args.live:
        raise ConfigError(
            "Live trading needs BOTH COF_TRADING_MODE=live in .env AND the --live flag. "
            "Without both, run without --live and with COF_TRADING_MODE=shadow."
        )
    limits = load_risk_limits()
    trade = load_trade_settings()
    universe = gateway.fetch_universe()
    if live:
        cash, fee, balances = _live_preflight(gateway, settings, universe)
    else:
        cash, fee, balances = trade.shadow_start_usd, trade.shadow_fee_rate, None
        if not universe.authoritative:
            print("NOTICE: shadow trading the public product catalogue (no credentials).", file=sys.stderr)
    mode = "live" if live else "shadow"
    store = StateStore(trade.state_file(mode))
    guard = RiskGuard(limits, Path(trade.kill_switch_file))

    def factory(runner: StreamRunner):
        executor = LiveExecutor(gateway, fee) if live else ShadowExecutor(runner.state, fee)
        engine = TradingEngine(limits=limits, guard=guard, executor=executor, pairs=universe.pairs,
                               state=runner.state, cash_usd=cash, store=store, emit=runner.emit)
        now = time.time()
        runner.emit({"type": "risk_limits", "at": now, "mode": mode, "fingerprint": limits.fingerprint(),
                     "limits": limits.to_dict(), "fee_rate": str(fee), "cash_usd": str(cash)})
        engine.restore(store.load(), now)
        if live:
            _reconcile(engine, balances, now)
        return engine

    runner = StreamRunner(universe, load_stream_settings(), engine_factory=factory)
    try:
        runner.run(duration_s=args.duration)
    except KeyboardInterrupt:
        runner.stop_event.set()
    finally:
        runner.engine.persist()
        runner.emit({"type": "stopped", "at": time.time(), "trading": runner.engine.summary()})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cof_bot")
    parser.add_argument("--env-file", default=None, help="path to a .env file (default: ./.env)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="verify API credentials and account access")
    universe = sub.add_parser("universe", help="list compliant USD spot pairs")
    universe.add_argument("--json", action="store_true")
    headlines = sub.add_parser("headlines", help="fetch, score and attribute current news headlines")
    headlines.add_argument("--matched-only", action="store_true", help="only headlines tied to a pair")
    stream = sub.add_parser("stream", help="live market data + news; print correlation signals (no orders)")
    stream.add_argument("--duration", type=float, default=None, help="seconds to run (default: until Ctrl+C)")
    trade = sub.add_parser("trade", help="signals plus the trading engine; shadow mode unless --live")
    trade.add_argument("--duration", type=float, default=None, help="seconds to run (default: until Ctrl+C)")
    trade.add_argument("--live", action="store_true",
                       help="place real orders; also requires COF_TRADING_MODE=live")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = load_settings(dotenv_path=args.env_file)
        gateway = CoinbaseGateway(settings)
        handler = {
            "verify": _cmd_verify,
            "universe": _cmd_universe,
            "headlines": _cmd_headlines,
            "stream": _cmd_stream,
            "trade": _cmd_trade,
        }[args.command]
        return handler(gateway, args)
    except CofBotError as exc:
        print(f"ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
