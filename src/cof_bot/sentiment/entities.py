"""Attribute headlines to products in the trading universe.

Precision matters more than recall here: a false match can put a trade on the
wrong coin. Rules, in order:

* ``$SYM`` or ``(SYM)`` always matches, for any symbol length.
* A bare upper case symbol matches when it is 3 or more characters and not a
  known collision (for example ATH is Aethir's symbol and also "all-time
  high"; AI is Gensyn's symbol).
* A coin name matches case insensitively as a whole phrase. A name that is
  also an ordinary word (Flow, Safe, Sonic, Story, ...) matches only next to
  a crypto qualifier ("Flow token", "Sonic price") or when the headline also
  carries the coin's symbol.

Symbols come from the product id, Coinbase's display symbol and a small alias
table for legacy Coinbase ids (CGLD is Celo, COSMOSDYDX is dYdX).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from cof_bot.exchange.universe import UsdPair

SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "CGLD": ("CELO",),
    "COSMOSDYDX": ("DYDX",),
    "JUPITER": ("JUP",),
    "CORECHAIN": ("CORE",),
    "BOBBOB": ("BOB",),
    "FUN1": ("FUN",),
    "ZETACHAIN": ("ZETA",),
}

# Upper case tokens common in headlines that are not about the coin.
SYMBOL_COLLISIONS = frozenset(
    {"AI", "ATH", "ETF", "SEC", "CEO", "CFO", "USD", "US", "UK", "EU", "IPO", "API", "DAO", "NFT",
     "DEX", "CEX", "TVL", "FBI", "DOJ", "CPI", "FED", "GDP", "IRS", "ONE", "NEW", "CFTC", "OCC",
     "FOMC", "AML", "KYC", "RWA", "L2", "L1", "ME", "UP", "IO", "IP", "OP", "RE", "LA", "SD", "CP",
     # STRK is also Strategy's preferred stock ticker; Starknet still matches by name or $STRK.
     "STRK"}
)

# Names that are ordinary English words or common proper nouns.
AMBIGUOUS_NAMES = frozenset(
    {"access", "amp", "aster", "awe", "beam", "blast", "blur", "cap", "core", "dash", "degen",
     "drift", "edge", "definitive", "elsa", "flow", "fluid", "flock", "grass", "grove", "gravity",
     "helium", "index", "jupiter", "kite", "magic", "mantle", "morpho", "newton", "nexus", "orca",
     "plasma", "plume", "prime", "quant", "recall", "render", "request", "re", "safe", "sei",
     "sign", "sky", "sonic", "spark", "story", "tensor", "toshi", "intuition", "turbo", "walrus",
     "zora", "aurora", "aztec", "stacks", "compound", "balancer", "orchid", "civic", "enzyme",
     "golem", "lighter", "limitless", "marlin", "noice", "rainbow", "sentient", "succinct",
     "threshold", "treehouse", "wormhole", "cookie", "fight", "check", "checkmate", "home",
     "honey", "trust", "super", "useless", "math", "troll", "bonk", "grass", "linea", "katana",
     "kaito", "echelon", "monad", "sapien", "pharos", "origin", "harvest", "reserve", "venice",
     "caldera", "euler", "espresso", "lombard", "fluent", "aligned", "boundless", "towns",
     "meteora", "marinade", "morpho", "tellor", "sport", "yield", "cluster", "allora", "altlayer",
     "billions", "perpetual"}
)

# Never used alone as a shortened name: these belong to excluded Tier 1 assets
# ("Ethereum Name Service" must not match every Ethereum headline).
CORE_NAME_BLOCKLIST = frozenset({"bitcoin", "ethereum", "ether", "usd", "wrapped", "the"})

QUALIFIERS = r"(?:token|tokens|coin|coins|protocol|network|price|chain|dao|labs|foundation|ecosystem|crypto)"

_NAME_SUFFIX = re.compile(
    r"\s+(?:\(.*?\)|protocol|network|token|finance|dao token|dao|coin|chain|governance token|"
    r"network token|ecosystem token|cloud|evm)$",
    re.IGNORECASE,
)


def _core_names(base_name: str) -> set[str]:
    names = set()
    name = base_name.strip()
    if not name:
        return names
    names.add(name.lower())
    stripped = name
    for _ in range(3):
        new = _NAME_SUFFIX.sub("", stripped).strip()
        if new == stripped:
            break
        stripped = new
    if stripped and len(stripped) >= 3 and stripped.lower() not in CORE_NAME_BLOCKLIST:
        names.add(stripped.lower())
    return names


@dataclass
class _Asset:
    product_id: str
    symbols: set[str]
    names: set[str]
    patterns: list[re.Pattern] = field(default_factory=list)


class AssetMatcher:
    def __init__(self, pairs: Iterable[UsdPair]):
        self.assets: list[_Asset] = []
        for pair in pairs:
            symbols = {pair.base_currency.upper(), pair.display_symbol.upper()}
            symbols.update(SYMBOL_ALIASES.get(pair.base_currency.upper(), ()))
            symbols.discard("")
            asset = _Asset(pair.product_id, symbols, _core_names(pair.base_name))
            asset.patterns = self._build(asset)
            display = pair.display_symbol
            if display != display.upper() and len(display) >= 3:
                # Mixed case display symbols (cbETH, JitoSOL) are distinctive as written.
                asset.patterns.append(re.compile(rf"(?<![\w$-]){re.escape(display)}(?![\w-])"))
            self.assets.append(asset)

    @staticmethod
    def _build(asset: _Asset) -> list[re.Pattern]:
        pats: list[re.Pattern] = []
        for sym in asset.symbols:
            s = re.escape(sym)
            pats.append(re.compile(rf"(?<![\w$])\${s}\b", re.IGNORECASE))
            pats.append(re.compile(rf"\(\s*\$?{s}\s*\)"))
            if len(sym) >= 3 and sym not in SYMBOL_COLLISIONS and not sym[0].isdigit():
                pats.append(re.compile(rf"(?<![\w$-]){s}(?![\w-])"))
        for name in asset.names:
            n = re.escape(name).replace(r"\ ", r"\s+")
            if name in AMBIGUOUS_NAMES:
                pats.append(re.compile(rf"(?<![\w-]){n}\s+{QUALIFIERS}\b", re.IGNORECASE))
            else:
                pats.append(re.compile(rf"(?<![\w-]){n}(?![\w-])", re.IGNORECASE))
        return pats

    def match(self, text: str) -> list[str]:
        if not text:
            return []
        hits = []
        for asset in self.assets:
            if any(p.search(text) for p in asset.patterns):
                hits.append(asset.product_id)
        return hits
