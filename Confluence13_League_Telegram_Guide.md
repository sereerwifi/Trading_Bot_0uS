# XAUUSD EA v2 — 20-Strategy Confluence, League System, Breakeven, Telegram

This supplements `MM_System_Guide.md`. It covers the new entry engine added on
top of the existing Money Management system — nothing in the original MM
guide changed; this is an additional layer.

## 1. What's new

| Feature | File(s) |
|---|---|
| 20-strategy 0–100% scoring engine (13 original + 5 merged from v1 + 1 DOM order-flow + 1 Macro Bias/Big Data) | `strategies.py` |
| League System (auto-bench losing strategies) | `league.py` |
| Telegram order alerts | `telegram_alert.py` |
| Confluence entry logic + Breakeven SL + DOM fetch | `xauusd_mt5_strategy.py` |
| Big Data / macro fundamentals fetcher (DXY, COT, COMEX, yield, calendar) | `macro_data.py` |
| New config tabs | `strategy_config_ui.py` |
| Live scores + League panel + Macro checklist | `generate_dashboard.py` |

## 2. How entry decisions work (`ENTRY_MODE = "confluence13"`)

Every `scan_interval_seconds` (default 30s), the bot scores all 20 strategies
0–100% for **long** and **short** independently:

1. Order Block (ICT) ★
2. Supply & Demand
3. EMA Cross
4. RSI Divergence
5. London Breakout
6. Fibonacci
7. VWAP Rejection
8. News Fade
9. Multi-TF Align
10. BOS/CHoCH
11. Liquidity Sweep ✦
12. Fair Value Gap ✦
13. Opening Range Breakout ✦
14. MACD Signal Cross (merged from v1)
15. Bollinger Band Breakout (merged from v1)
16. S/R Breakout + Retest (merged from v1)
17. Price Action Candlestick (merged from v1)
18. ATR/Donchian Breakout (merged from v1)
19. Order Flow (DOM) — real MT5 Depth-of-Market bid/ask imbalance
20. Macro Bias (Big Data) — real DXY/yield/COT/COMEX/calendar fundamentals

★/✦ mark the more discretionary ICT/SMC-style concepts — these are
structured, backtestable *approximations*, not a ground-truth implementation
of manual chart reading. Expect to retune thresholds against your own data.

### #19 Order Flow (DOM) — what it actually is (and isn't)

This was added on request to approximate "Order Flow / Footprint" and "Order
Block" confirmation tools, using what MT5's Python API can genuinely provide:

- **What it uses**: `mt5.market_book_add/get/release()` — a real, live
  Depth-of-Market (Level2) snapshot of resting bid/ask volume, read fresh
  every scan via `get_dom_snapshot()` in `xauusd_mt5_strategy.py`. The score
  is the bid-vs-ask volume imbalance: heavier bids → long pressure, heavier
  asks → short pressure.
- **What it is NOT**: a historical footprint chart (per-tick bid/ask volume
  history — MT5's Python API has no endpoint for that, those come from paid
  3rd-party indicators reading the broker's raw tick feed differently), and
  it is NOT FXSSI's Order Book (that's broker-aggregated retail-trader
  sentiment data pulled from FXSSI's own service, not something the MT5 API
  exposes at all).
- **Order Block (#1) DOM bonus**: when a price retest of an Order Block zone
  lines up with a same-direction DOM imbalance, `score_order_block()` boosts
  its own score (up to +50%) — a lightweight version of what "Order Block
  Flow Elite" claims to do, using your own broker's real order book instead
  of a 3rd-party plugin.
- **Graceful fallback**: many brokers/symbols don't expose DOM at all. When
  that's the case `get_dom_snapshot()` returns `None` and this strategy
  scores a neutral 0/0 — it never errors and never blocks the other 18.

### #20 Macro Bias (Big Data) — what it actually is (and isn't)

Added on request to bring in the institutional "Big Data" checklist that
funds/Smart Money desks watch before forming a directional bias on Gold —
beyond price action alone. Implemented in `macro_data.py` (fetchers) +
`strategies.score_macro_bias()` (scoring).

**The weighted "Gold Decision Matrix"** (per `USA new trade.docx` — each
factor contributes its weight to a 0-100 Bull Score, not an equal vote):

| Factor | Weight | Bullish Gold | Bearish Gold |
|---|---|---|---|
| DXY (Dollar Index) | 30% | DXY falling | DXY rising |
| US 10Y Treasury Yield | 25% | Yield falling | Yield rising |
| Fed Expectation | 20% | Cut priced in | Hawkish/hold priced in |
| ETF Flow (GLD) | 10% | Inflow | Outflow |
| COT Position (Managed Money) | 10% | Net Long rising | Net Long falling |
| COMEX Registered Inventory | 5% | Registered thinning vs Eligible | Registered building |

`Bull Score = Σ(weight of bullish factors) / Σ(weight of AVAILABLE factors) × 100`
— unavailable factors (most often ETF Flow) are dropped from **both** the
numerator and denominator, so a missing source never silently drags the
score toward bearish. The doc's own probability bucket table is reproduced
in the strategy's `note` field:

| Bull Score | Label |
|---|---|
| 0-25 | Bearish 70-90% |
| 25-45 | Bearish 55-70% |
| 45-55 | Sideway (the doc's own guidance: don't trend-follow here) |
| 55-75 | Bullish 60-75% |
| 75-100 | Bullish 75-90% |

`long` = Bull Score, `short` = 100 − Bull Score, so this slots into the
confluence vote exactly like every other strategy.

**Real data sources used** (all free, no API key required for the basic
tier used here):

| Category | Source | Update cadence |
|---|---|---|
| DXY | Yahoo Finance chart API, falls back to FRED `DTWEXBGS` (Fed broad dollar index) if blocked | ~daily |
| US 10Y Yield | FRED `DGS10` (official Fed data) | daily (business days) |
| Fed Expectation | **Approximation** — FRED `DGS2` (US 2-Year Treasury yield) as a proxy for rate-cut/hike pricing, see caveat below | daily (business days) |
| ETF Flow | SPDR `GLD` / iShares `SLV` holdings CSV — **best-effort only**, see caveat below | daily, when reachable |
| COT Report | CFTC Socrata Open Data API (Disaggregated Futures Only) | weekly (Fridays) |
| COMEX Inventory | CME's own official `Gold_Stocks.xls` / `Silver_Stocks.xls` report | daily (business days) |
| Economic Calendar | ForexFactory's public JSON feed (same one their widget uses) | refreshed a few times/day |

**Honest caveat on Fed Expectation**: the real CME FedWatch tool (rate-cut/
hike odds derived from Fed Funds futures) has no free structured API and
its page is JavaScript-rendered, so it can't be reliably scraped with a
plain HTTP request. `fetch_fed_expectation()` uses the US 2-Year Treasury
yield trend instead — a market-standard, free, durable directional proxy
for near-term Fed policy expectations, but **not** the actual FedWatch
percentage. The dashboard card and code docstrings both label this clearly
as "(2Y proxy)" so you never mistake it for the real tool.

**Honest caveat on ETF Flow**: SPDR's and iShares' own holdings-CSV
endpoints sit behind bot-detection (WAF) that frequently serves an HTML
page instead of the CSV to a plain HTTP request — this was confirmed during
development. The fetch code is still there and may well work fine from your
own residential/office IP even though it didn't from the dev sandbox, but
treat it as a bonus signal, not a dependency: when it's unavailable,
`score_macro_bias()` simply drops it from the weighted matrix (renormalizes
the Bull Score over whatever weight is available) rather than treating "no
data" as bearish.

**Central Bank Buying** (World Gold Council) is intentionally **not**
auto-scraped — WGC has no free structured API and publishes this quarterly,
which doesn't fit a scan-loop-driven checklist. If you want this factored
in, treat it as a manual seasonal adjustment to `min_strategy_score`/weight
rather than a 7th auto-fetched item.

**Open Interest**: rather than scraping CME's (bot-walled) JSON stocks
endpoint a second time, this reuses the `open_interest_all` /
`change_in_open_interest_all` fields the CFTC already publishes alongside
every COT report — same underlying data, one less fragile fetch.

**Update cadence vs. the other 19 strategies**: this data does NOT refresh
every `scan_interval_seconds` like the price-derived strategies — COT is
weekly, the rest are every few hours (see `CACHE_TTL` in `macro_data.py`).
`get_macro_snapshot()` is still called every scan, but it's reading a local
JSON cache file almost every time, not hitting the network — cheap and
safe to leave as-is.

**Soft pre-news gate**: if a High-impact USD event (NFP, CPI, FOMC, PCE,
GDP, etc.) from the ForexFactory calendar is landing within 60 minutes,
both long/short scores from this strategy are damped by 40% — funds
typically stand aside right before these prints regardless of the rest of
the checklist.

**Graceful fallback**: if `macro_data.py` hasn't successfully fetched
anything yet (e.g. right after a fresh install, before the first
background fetch completes), `data["macro"]` is `None` and this strategy
scores a neutral 0/0 — exactly like the DOM strategy does when DOM is
unsupported. It never blocks or errors the other 19 strategies.

### Merge with v1's original 10-strategy list

The original (v1) `entry_mode = "legacy"` system had its own list of 10
strategies, configured in the "เงื่อนไขเข้าออเดอร์เดิม" tab. When the 13-strategy
confluence engine was built, 5 of those 10 concepts already had a near-identical
equivalent among the 13, so they were **not duplicated**:

| v1 strategy | Equivalent already in confluence | Action |
|---|---|---|
| `ema_cross` | EMA Cross | skipped (duplicate) |
| `rsi_divergence` | RSI Divergence | skipped (duplicate) |
| `fib_confluence` | Fibonacci | skipped (duplicate) |
| `mtf_alignment` | Multi-TF Align | skipped (duplicate) |
| `news_momentum` | News Fade | skipped (duplicate) |
| `macd_cross` | — none | **merged in as #14** |
| `bb_breakout` | — none | **merged in as #15** |
| `sr_breakout_retest` | — none | **merged in as #16** |
| `price_action` | — none | **merged in as #17** |
| `atr_donchian_breakout` | — none | **merged in as #18** |

The 5 genuinely new v1 concepts now run as full confluence strategies (votes,
weights, League tracking, dashboard rows) under the names above. The v1
"เงื่อนไขเข้าออเดอร์เดิม" tab still exists, but it only takes effect if you set
`entry_mode = "legacy"` — with the default `entry_mode = "confluence13"`, that
tab's settings are ignored (its own UI note explains this).

An order only fires when there is **confluence** — multiple strategies must
agree, not just one strategy crossing a threshold on its own. The gate is two
parts, both configurable in the UI's "20 กลยุทธ์ (Confluence)" tab:

- **Combined score**: the weighted average score of all non-benched,
  "voting" strategies (a strategy "votes" a side once its score on that side
  reaches 50%) must be ≥ `min_strategy_score` (default 70%).
- **Agreeing count**: at least `min_agreeing_strategies` (default 3) strategies
  must be voting that same side.

If both long and short qualify at once, the higher-scoring side is taken. The
existing Daily Trend Filter, drawdown breaker, daily loss limit, anti-Martingale
breaker, session filter, and max-concurrent-trades checks all still apply on
top of this — confluence13 doesn't bypass any of the original MM gates.

Per-strategy enable/disable and weight are in the same tab; a disabled
strategy is excluded entirely, a benched one (see below) is still scored and
shown but contributes 0 weight.

SL/TP for confluence trades: `SL = ATR(H1) × sl_atr_mult` (default 1.5),
`TP2 = SL distance × tp_rr` (default 2.0 — i.e. R:R 1:2).

Set `entry_mode` back to `"legacy"` in the UI to revert to the original
single-strategy `fib_confluence` logic.

## 3. League System

Each strategy's win/loss record is tracked in `strategy_league.json`. When a
confluence trade closes, **every contributing strategy** (everyone who voted
on the winning side) gets credited with that trade's win/loss — since the
entry itself was a group decision.

A strategy is benched (temporarily zero-weighted) when **either**:

- it loses `max_consecutive_losses` trades in a row (default 3), **or**
- its win-rate over the last `winrate_lookback_trades` trades (default 10)
  drops below `min_winrate_pct` (default 35%)

Both rules and the bench duration (`bench_hours`, default 24h) are adjustable
in the "League System" tab. A benched strategy keeps appearing on the
dashboard with its live score so you can see it's still being evaluated —
it's just excluded from the confluence vote until the bench expires.

## 4. Breakeven Stop Loss

Independent of the existing trailing-stop methods (ATR/EMA/FIXED_POINTS/
PERCENT). Once a position's floating profit reaches `trigger_r` × the
*original* risk distance captured at entry (default 1.0R), the SL is moved to
entry price ± `buffer_points` (default 2 points) — and only ever tightens,
never loosens. Configurable in Risk & Basket Close tab. Trades opened before
this feature existed (no stored risk metadata) are skipped safely.

## 5. Telegram Alerts

Sends a message on every new order and every close, with the contributing
strategies and running account stats.

**Setup (do this yourself — never paste your token or chat ID into a chat
with anyone, including me):**

1. On Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Send your new bot any message, then open in a browser:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   and find the numeric `"chat":{"id": ...}` value — that's your chat ID.
3. Open `strategy_config_ui.py` → "Telegram Alert" tab → paste the token and
   chat ID, tick "เปิดใช้ Telegram Alert", Save Config.

If left blank/disabled, the bot trades exactly the same — Telegram alerts are
purely informational and fail silently (never interrupt trading) if
misconfigured or offline.

## 6. Dashboard additions

Re-run `python generate_dashboard.py` (or `run_dashboard.bat`) — new
sections appear:

- **Multi-Strategy Confluence (20) — Live Scores**: combined long/short score,
  the active gate thresholds, last scan time, and a full per-strategy table
  (score, weight, active/benched, note).
- **Macro Bias (Big Data) — Gold Decision Matrix**: a 6-card weighted
  checklist (DXY 30%, US10Y Yield 25%, Fed Expectation 20%, ETF Flow 10%,
  COT Net Long 10%, COMEX Registered 5%) each marked BULLISH/BEARISH/N/A with
  the underlying real number and its weight, plus a bolded Bull Score +
  probability-bucket line, so you can see at a glance how the weighted
  formula landed — not just the single combined macro_bias score/note row in
  the table above.
- **League System — Win/Loss Bench Status**: trades, win-rate, current
  losing streak, and bench status/reason per strategy.

These read straight from `strategy_scores.json` / `strategy_league.json`, so
they show your latest data even if MT5 isn't connected at the moment you
generate the dashboard (it'll just say so).

## 7. New files this version writes (auto-generated, safe to delete to reset)

- `strategy_scores.json` — latest scan snapshot for the dashboard (now
  includes `"macro"`, the raw `macro_data.get_macro_snapshot()` result)
- `strategy_league.json` — League System state
- `open_entry_meta.json` — pending trade → contributing-strategies map
- `processed_deals.json` — dedupe list so closed trades aren't double-counted
- `macro_data_cache.json` — last-good value + timestamp per Big Data source,
  refreshed at the cadence in `macro_data.py`'s `CACHE_TTL` (so a transient
  network hiccup falls back to the last successful fetch instead of going
  blank). Safe to delete to reset — it's just a cache.
- `macro_data_history.db` — **this is the data-loss protection layer**, a
  permanent SQLite append-only log (table `macro_history`) of every
  successful fetch, separate from the cache above. Where
  `macro_data_cache.json` only ever holds the single latest value (and is
  safe/expected to be deleted), this file is the actual historical record —
  if the bot crashes, the EXE errors out, or the cache file gets corrupted
  or wiped, your macro data history survives in this DB untouched. Query it
  with `macro_data.get_macro_history(category=None, limit=200)` from a
  Python shell any time you want to inspect or export it. **Do not delete
  this file** unless you intend to wipe your history on purpose.

### Optional: backing up history to Google Sheets

`macro_data.export_history_to_google_sheets(sheet_id, creds_json_path)` is an
optional, fully gated second layer of backup — useful if you want your
macro history visible/off-machine (e.g. in case the whole PC has a problem,
not just the bot). It does nothing and returns `False` silently if you
haven't set it up — it can never break the bot.

**Setup (do this yourself — never paste your service-account JSON or its
contents into a chat with anyone, including me, same rule as the Telegram
bot token):**

1. `pip install gspread google-auth` (not installed by default — only
   needed if you want this feature).
2. In Google Cloud Console, create a Service Account, enable the
   "Google Sheets API", and download its JSON key file to your own machine.
3. Create a Google Sheet, then share it with the service account's email
   (found inside the JSON key file, looks like
   `xxx@xxx.iam.gserviceaccount.com`) with Editor access.
4. Copy the Sheet ID from its URL (the long string between `/d/` and
   `/edit`).
5. Call `export_history_to_google_sheets("<SHEET_ID>", "<path to your JSON key file>")`
   whenever you want to push the latest history snapshot — manually, or
   wire it into a scheduled task if you want it automatic. It overwrites
   `sheet1` with the latest `limit` rows each time it's called (simple
   snapshot export, not an incremental append).
