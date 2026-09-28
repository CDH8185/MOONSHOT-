# cof_bot: sentiment momentum bot for Coinbase Advanced

A sentiment driven momentum trading bot for small, volatile assets, limited to
USD spot pairs your Coinbase account can trade. It runs in **shadow mode** by
default: no real orders.

Status: **Phase 3 of 4** complete: live market data, news sentiment and signals (Phase 2), plus entry logic, orders, position sizing and circuit breakers (Phase 3). Shadow mode is the default; live orders need two separate switches.

## Setup (Windows PowerShell)

```powershell
cd MOONSHOT-
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
notepad .env
```

On macOS or Linux: `python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && cp .env.example .env`.

### API key

Create a key in the Coinbase Developer Platform:

* signature algorithm **ECDSA** (Coinbase's App API docs require ES256);
* permissions **View** and **Trade** only. **Never Transfer.** The bot refuses
  to start with a key that has the transfer permission.

Put it in `.env`, either as `COINBASE_API_KEY` plus `COINBASE_API_SECRET`, or
as `COINBASE_API_KEY_FILE` pointing to the downloaded JSON. `.env`, `*.pem` and
key JSON files are git ignored. Nothing secret is written to logs.

## Commands

```
python -m cof_bot verify           # credentials, permissions, balances, universe size
python -m cof_bot universe         # compliant USD pairs with rejection counts
python -m cof_bot universe --json  # machine readable
python -m cof_bot headlines        # current headlines, scored and tied to pairs
python -m cof_bot stream           # live signals as JSON lines (Ctrl+C to stop)
python -m cof_bot stream --duration 600
python -m cof_bot trade            # signals + trading engine, shadow mode (no orders)
python -m cof_bot trade --live     # REAL orders; also needs COF_TRADING_MODE=live
python -m pytest                   # test suite
```

Exit code 0 is success, 2 is a configuration, credential or exchange error.

## Compliance: how the asset universe is built

The authenticated List Products call answers for the account that owns the key,
so for a United States account it returns what that account may trade. That
list is marked **authoritative**. Without credentials the bot falls back to the
public catalogue, marks it **non authoritative**, and it may never feed live
trading.

A product qualifies only if it is SPOT, quoted in USD, online, and not
disabled, trading disabled, view only, cancel only, limit only, post only or in
auction (the strategy must be able to exit at market immediately). Tier 1 bases
(`COF_EXCLUDED_BASES`, default BTC, ETH) and stablecoins are excluded. A 24 hour
USD volume floor (`COF_MIN_QUOTE_VOLUME_24H`, default 100,000) screens out
pairs too thin to exit; an optional ceiling narrows toward smaller assets.

## Error handling

Every REST call passes through `exchange/retry.py`:

* client side throttle (`COF_REST_MAX_RPS`, default 2 per second, 7,200 per hour,
  under Coinbase's documented 10,000 per hour per key);
* exponential backoff with full jitter on 429, 500, 502, 503, 504, timeouts and
  dropped connections, honouring `Retry-After`, capped at `COF_BACKOFF_CAP_S`;
* no retry on other 4xx; a 401 or 403 becomes a `CredentialError`.

## Phase 2: how a signal is produced

1. **Market data** (`market/ws_feed.py`). One WebSocket connection carries
   `heartbeats`, `ticker` and `market_trades` for the whole universe, plus
   `level2` order books for a hot list of at most 25 pairs.
   * **Certificate checking.** The SDK's WebSocket client (1.8.4) connects
     with `ssl.SSLContext()`, which does not check certificates. The bot
     overrides only that step to use a verifying context.
   * **No credentials on the feed.** These channels are public, so no login
     token (JWT) is ever sent.
   * **Reconnects.** The bot handles reconnection itself, where the SDK gives
     up after 5 tries: it reconnects with no limit, with capped backoff, when
     the SDK reports a failure or when no message arrives for 15 seconds.
   * **Dropped messages.** A gap in `sequence_num` means messages were lost,
     so every order book is rebuilt from a fresh snapshot.
   * **Order book limit.** Coinbase refused more than 30 order books on one
     connection without keys ("too many L2 streams requested in a single
     session", measured 2026-09-27), hence the hot list.
2. **Volume** (`market/volume.py`). Each trade's USD value is added to
   one-minute buckets, with duplicate trades dropped. A surge compares the last
   5 minutes with the prior 60. Coinbase reports the **maker's** side on each
   trade, so a trade marked `SELL` is an aggressive buy.
3. **News** (`news/feeds.py`). The RSS and Atom feeds of 11 outlets, each
   reached from the build environment on 2026-09-27. Article pages are never
   fetched. The collector:
   * identifies itself as a bot and honours each site's robots.txt;
   * asks the server to send a feed only if it has changed since last time;
   * caps each feed at 5 MB and refuses XML that declares a DOCTYPE or ENTITY;
   * backs off from a feed that keeps failing.
4. **Sentiment** (`sentiment/analyzer.py`). VADER 3.3.2 plus a crypto word
   list. Labels use VADER's published thresholds (±0.05).
5. **Matching headlines to coins** (`sentiment/entities.py`). It favours
   avoiding false matches over catching every mention:
   * A symbol written as `$SYM` or `(SYM)` always matches.
   * A bare symbol matches only if it is 3 or more letters and not a known
     collision (for example ATH is also "all-time high").
   * A coin name that is also an ordinary word (Flow, Safe, Sonic) matches
     only next to a crypto word such as "token" or "price".
6. **Signal** (`signals/correlator.py`). A signal needs both of the following.
   * A sentiment spike:
     * at least 1 mention in the last hour, and at least 2 times the coin's
       own mention rate over the prior 24 hours;
     * a mean score of at least 0.3, and no headline at -0.3 or lower;
     * the newest positive headline no more than 30 minutes old.
   * An upward volume surge:
     * at least 2 times the baseline, and at least $5,000 in the 5 minute window;
     * at least 55% aggressive buys, and a rising price.

   A 15 minute cooldown applies per pair. Every threshold is set in `.env`.

Output from `stream` is one JSON object per line, with `type` set to
`headline`, `signal` or `status`. Phase 4 writes these to log files.

## Phase 3: how a trade is made and managed

`trade` runs everything `stream` does and hands each signal to the trading
engine (`trading/engine.py`). The order of authority, highest first:

1. **Circuit breakers** (`risk/guard.py`). A kill switch file (`data/KILL`
   by default) or a loss of `COF_MAX_DAILY_DRAWDOWN` (default 3%) from the
   day's starting equity sells every position and blocks entries: until the
   file is removed, or until the next trading day. The trading day starts at
   midnight Pacific (`COF_DAY_TIMEZONE`).
2. **Entry halts.** 3 losing trades in a row, 3 failed orders, 10 trades in
   a day, or 3 open positions stop new entries. Open positions keep their
   stops.
3. **Market quality.** No entry without a synced order book, a price under
   10 seconds old, and a spread of at most 150 basis points.
4. **Fractional sizing** (`risk/sizing.py`). The position is the smallest of:
   * the risk budget: 0.5% of equity, divided by the loss per dollar if the
     stop is hit (stop, slippage allowance and both fees);
   * 10% of equity;
   * cash, less the fee;
   * 25% of the ask depth within 1% of the mid;
   * what is left of the day's drawdown allowance.

   Sizing can only shrink or refuse a trade.
5. **The signal.** Signals are taken strongest first.

**Entry** is a limit order that fills immediately or cancels (IOC), priced
at the best ask plus 75 basis points, so a thin book can never fill it at an
unbounded price. **Exit** is a market sell.

**Infinity trailing** (`trading/position.py`). The stop starts 6% under
entry. When the best bid has risen 4% above entry, a trailing stop switches
on 5% under the highest bid seen and follows every new high, with no
take-profit cap. The stop only moves up.

**Immutable limits** (`risk/limits.py`). The limits are read once at start
and held in a frozen object that no code writes to. Each value is checked
against hard bounds, so a typo cannot risk more than 2% per trade or 10% per
day. Their SHA-256 fingerprint is printed at start and in every status line.

**Shadow and live** (`trading/executor.py`). Shadow fills are simulated
against the live order book at a configurable fee (`COF_SHADOW_FEE_RATE`,
default 1.2%, an assumption to set to your tier). Live trading needs all of
the following:
* `COF_TRADING_MODE=live` and the `--live` flag;
* a key with the trade permission and without the transfer permission;
* the account's own product list;
* the account's real taker fee, read from Coinbase.

The gateway refuses to place an order in any other mode. Order retries reuse
the same `client_order_id`, which Coinbase documents as returning the
existing order instead of creating a second one (read 2026-09-27).

**State** (`data/state_shadow.json`, `data/state_live.json`). Open positions,
shadow cash and the day's counters are saved atomically after every change
and restored on restart. In live mode, stored positions are checked against
the account's balances at start.

`trade` output adds JSON lines of type `risk_limits`, `entry`, `exit`,
`skip` (with the reason), `breaker`, `new_day`, `error`, `restored`,
`reconcile` and `stopped`.

### Known limits

* **Stops are enforced by the bot, not by Coinbase.** While the bot is not
  running, open positions have no stop.
* **A stop is a trigger, not a price.** A price that gaps through the stop
  sells at the lower bid, so a single loss can exceed the 0.5% risk budget.
  A test shows this.
* **Live cash** comes from Coinbase at start and is then tracked from the
  bot's own fills. Deposits or trades made while the bot runs are not seen
  until a restart.
* A volume surge needs 65 minutes of trade history, so no signal can fire in
  the first hour after start.
* A word list cannot fix every headline. Example: "Batch upgrade slips to
  Oct. 9 after validator support resets" still scores +0.23. That is below
  the 0.3 signal bar, but it is not negative.
* A coin name that is also an ordinary word, used without a qualifier, is
  missed on purpose. Example: "Mantle says tokenized assets jumped" does not
  match Mantle.

## Layout

```
src/cof_bot/config.py            environment settings, validation, secret masking
src/cof_bot/errors.py            typed exceptions
src/cof_bot/exchange/retry.py    throttle and retry
src/cof_bot/exchange/client.py   SDK gateway: verify_access, fetch_universe
src/cof_bot/exchange/universe.py USD pair filter
src/cof_bot/cli.py               verify, universe, headlines, stream, trade
src/cof_bot/market/              ws_feed.py, order_book.py, volume.py
src/cof_bot/news/feeds.py        RSS and Atom collection
src/cof_bot/sentiment/           analyzer.py (VADER + lexicon), entities.py
src/cof_bot/signals/correlator.py sentiment and volume signal
src/cof_bot/runtime.py           stream runner (and trade runner with an engine)
src/cof_bot/risk/                limits.py (immutable), sizing.py, guard.py (breakers)
src/cof_bot/trading/             engine.py, position.py (infinity trailing), executor.py, store.py
tests/fixtures/                  recorded live WebSocket messages
tests/                           offline tests with a fake SDK client
```
