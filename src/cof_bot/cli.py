"""Command line entry point.

    python -m cof_bot verify     check credentials, permissions and balances
    python -m cof_bot universe   list the compliant USD spot pairs
    python -m cof_bot headlines  score and attribute current news headlines
    python -m cof_bot stream     live signals from sentiment and volume (no orders)
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
        }[args.command]
        return handler(gateway, args)
    except CofBotError as exc:
        print(f"ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
