from decimal import Decimal

import pytest

from cof_bot.exchange.universe import UsdPair
from cof_bot.sentiment.analyzer import HeadlineSentiment, SentimentScore
from cof_bot.sentiment.entities import AssetMatcher

A = HeadlineSentiment()


@pytest.mark.parametrize(
    "headline, label",
    [
        # The first six were misread by stock VADER (measured 2026-09-27).
        ("SEC sues Ripple over unregistered securities", "negative"),
        ("Token plunges after rug pull", "negative"),
        ("Bitcoin rallies to new all-time high", "positive"),
        ("Dogecoin pumps on Musk tweet", "positive"),
        ("Bullish breakout for ONDO", "positive"),
        ("Coinbase to list PEPE", "positive"),
        ("Solana surges 20% as ETF approval nears", "positive"),
        ("Exchange hacked, $40M drained from hot wallet", "negative"),
        ("Protocol exploit leads to delisting", "negative"),
        ("Stablecoin depegs amid sell-off", "negative"),
        ("Bitcoin price holds steady", "neutral"),
        ("Analysts are not bullish on the token", "negative"),
    ],
)
def test_headline_labels(headline, label):
    assert A.score(headline).label == label


def test_known_limit_delay_headline_stays_below_signal_bar():
    # VADER reads "upgrade" and "support" as positive; the overlay cannot make
    # this delay headline negative, but it keeps it under the 0.3 signal bar.
    s = A.score("XRP Ledger's Batch upgrade slips to Oct. 9 after validator support resets")
    assert s.compound < 0.3


def test_thresholds_follow_vader_readme():
    assert SentimentScore(0.05, 0, 0, 1).label == "positive"
    assert SentimentScore(0.0499, 0, 0, 1).label == "neutral"
    assert SentimentScore(-0.05, 0, 0, 1).label == "negative"


def test_extra_lexicon_and_empty():
    a = HeadlineSentiment({"Wagmi": 3.0})
    assert a.score("wagmi").compound > 0.5
    assert a.score("").compound == 0.0


def pair(pid, name, display=None):
    base = pid.split("-")[0]
    d = Decimal("1")
    return UsdPair(pid, base, name, display or base, d, d, d, d, None, None)


PAIRS = [
    pair("ONDO-USD", "Ondo"),
    pair("ENS-USD", "Ethereum Name Service"),
    pair("STRK-USD", "Starknet Token"),
    pair("BILL-USD", "Billions Network"),
    pair("PERP-USD", "Perpetual Protocol"),
    pair("FLOW-USD", "Flow"),
    pair("S-USD", "Sonic"),
    pair("ATH-USD", "Aethir"),
    pair("CGLD-USD", "Celo"),
    pair("ME-USD", "Magic Eden"),
    pair("GRT-USD", "The Graph"),
    pair("ZRO-USD", "LayerZero"),
    pair("CBETH-USD", "Coinbase Wrapped Staked ETH", "cbETH"),
]
M = AssetMatcher(PAIRS)


@pytest.mark.parametrize(
    "headline, expected",
    [
        ("Ondo Finance Unveils BlackRock-Backed Portfolios", ["ONDO-USD"]),
        ("Bullish breakout for ONDO", ["ONDO-USD"]),
        ("KelpDAO Sues LayerZero Over $292M Hack", ["ZRO-USD"]),
        ("The Graph adds new indexers", ["GRT-USD"]),
        ("Magic Eden warns of exploit", ["ME-USD"]),
        ("Starknet Token jumps", ["STRK-USD"]),
        ("CELO rallies after upgrade", ["CGLD-USD"]),
        ("Sonic (S) surges 12%", ["S-USD"]),
        ("$S holders cheer", ["S-USD"]),
        ("Flow token breaks out", ["FLOW-USD"]),
        ("Aethir (ATH) expands GPU network", ["ATH-USD"]),
        ("cbETH discount narrows", ["CBETH-USD"]),
        # false positives found against live headlines, 2026-09-27
        ("Ethereum Price Analysis: ETH Eyes $3K", []),
        ("Strategy proposes dividends for STRC, STRD, STRF and STRK preferred stocks", []),
        ("Kraken parent is betting billions on infrastructure", []),
        ("OG.com seeks CFTC approval for single-stock perpetual futures", []),
        # ordinary words and acronyms
        ("Money continues to flow into ETFs", []),
        ("Bitcoin hits new ATH", []),
        ("Sonic boom in the market", []),
        ("Is it safe to buy now?", []),
        ("", []),
    ],
)
def test_matcher(headline, expected):
    assert M.match(headline) == expected
