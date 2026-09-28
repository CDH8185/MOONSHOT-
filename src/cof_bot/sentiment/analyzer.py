"""Headline polarity with VADER plus a crypto market lexicon.

VADER (vaderSentiment 3.3.2, MIT) is a lexicon and rule based scorer built for
short social text. Its ``compound`` score lies in [-1, 1]; its README gives the
standard labels: positive >= 0.05, negative <= -0.05, neutral between.

Stock VADER misreads market headlines. Measured 2026-09-27 before this
overlay: "SEC sues Ripple over unregistered securities" scored +0.296, and
"Token plunges after rug pull", "Bitcoin rallies to new all-time high",
"Dogecoin pumps on Musk tweet" and "Bullish breakout for ONDO" all scored 0.
The overlay adds market vocabulary on VADER's -4 to +4 valence scale and
joins multi word terms into single tokens before scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

# Multi word terms joined into one token before scoring.
PHRASES: list[tuple[re.Pattern, str]] = [
    (re.compile(p, re.IGNORECASE), repl)
    for p, repl in [
        (r"\ball[\s-]time[\s-]highs?\b", " athigh "),
        (r"\brecord[\s-]highs?\b", " athigh "),
        (r"\brug[\s-]?pull(ed|s)?\b", " rugpull "),
        (r"\bshort[\s-]squeeze\b", " shortsqueeze "),
        (r"\bsell[\s-]?offs?\b", " selloff "),
        (r"\bclass[\s-]action\b", " classaction "),
        (r"\bto\s+list\b", " listing "),
        (r"\blists\s+on\b", " listing "),
        (r"\bde[\s-]?list(s|ed|ing)?\b", " delisting "),
        (r"\bde[\s-]?peg(s|ged|ging)?\b", " depeg "),
    ]
]


def _forms(valence: float, *words: str) -> dict[str, float]:
    return {w: valence for w in words}


CRYPTO_LEXICON: dict[str, float] = {
    # joined phrases
    "athigh": 2.6, "rugpull": -3.5, "shortsqueeze": 1.5, "selloff": -2.2,
    "classaction": -2.5, "listing": 1.8, "delisting": -3.0, "depeg": -2.8,
    # upward moves and bullish vocabulary
    **_forms(2.2, "surge", "surges", "surged", "surging"),
    **_forms(2.5, "soar", "soars", "soared", "soaring", "skyrocket", "skyrockets", "skyrocketed"),
    **_forms(2.2, "rally", "rallies", "rallied", "rallying"),
    **_forms(1.8, "jump", "jumps", "jumped", "jumping"),
    **_forms(1.5, "climb", "climbs", "climbed", "climbing", "rebound", "rebounds", "rebounded"),
    **_forms(1.2, "spike", "spikes", "spiked"),
    **_forms(1.5, "pump", "pumps", "pumped", "pumping"),
    **_forms(2.0, "moon", "mooning", "moons"),
    **_forms(2.5, "bullish"),
    **_forms(1.8, "breakout", "breakouts"),
    **_forms(1.5, "outperform", "outperforms", "outperformed", "adoption", "inflow", "inflows"),
    **_forms(1.5, "partnership", "partnerships"),
    **_forms(1.2, "integration", "integrates", "integrated", "mainnet", "upgrade", "upgrades", "accumulation"),
    **_forms(2.0, "approval", "approves", "approved"),
    **_forms(1.0, "launch", "launches", "launched", "airdrop"),
    # downward moves and bearish vocabulary
    **_forms(-2.8, "plunge", "plunges", "plunged", "plunging"),
    **_forms(-3.0, "crash", "crashes", "crashed", "crashing"),
    **_forms(-2.5, "tumble", "tumbles", "tumbled", "bearish", "capitulation"),
    **_forms(-2.3, "slump", "slumps", "slumped"),
    **_forms(-2.0, "sink", "sinks", "sank", "outage", "halt", "halts", "halted"),
    **_forms(-1.5, "drop", "drops", "dropped", "fall", "falls", "fell", "outflow", "outflows"),
    **_forms(-1.0, "dip", "dips", "dipped"),
    **_forms(-2.2, "dump", "dumps", "dumped", "dumping", "crackdown", "subpoena"),
    **_forms(-3.0, "hack", "hacks", "hacked", "hacker", "hackers", "exploit", "exploits", "exploited"),
    **_forms(-2.5, "drain", "drains", "drained", "breach", "phishing", "lawsuit", "sue", "sues", "sued"),
    **_forms(-3.0, "stolen", "scam", "fraud", "indicted", "indictment", "insolvent", "insolvency"),
    **_forms(-3.2, "ponzi", "bankrupt", "bankruptcy"),
    **_forms(-2.5, "ban", "bans", "banned"),
    **_forms(-2.0, "liquidation", "liquidations", "liquidated", "vulnerability"),
    **_forms(-1.5, "probe", "investigation", "downgrade", "downgraded", "unregistered"),
    **_forms(-1.8, "fud"),
    **_forms(-1.2, "delay", "delays", "delayed", "postpone", "postponed"),
    **_forms(-1.8, "slip", "slips", "slipped"),
}


@dataclass(frozen=True)
class SentimentScore:
    compound: float
    positive: float
    negative: float
    neutral: float

    @property
    def label(self) -> str:
        if self.compound >= POSITIVE_THRESHOLD:
            return "positive"
        if self.compound <= NEGATIVE_THRESHOLD:
            return "negative"
        return "neutral"


class HeadlineSentiment:
    def __init__(self, extra_lexicon: dict[str, float] | None = None):
        self._vader = SentimentIntensityAnalyzer()
        self._vader.lexicon.update(CRYPTO_LEXICON)
        if extra_lexicon:
            self._vader.lexicon.update({k.lower(): float(v) for k, v in extra_lexicon.items()})

    @staticmethod
    def normalize(text: str) -> str:
        for pattern, repl in PHRASES:
            text = pattern.sub(repl, text)
        return re.sub(r"\s+", " ", text).strip()

    def score(self, text: str) -> SentimentScore:
        s = self._vader.polarity_scores(self.normalize(text or ""))
        return SentimentScore(s["compound"], s["pos"], s["neg"], s["neu"])
