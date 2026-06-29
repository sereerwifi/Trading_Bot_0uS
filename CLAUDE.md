# XAUUSD MT5 Trading Bot — Project Context

This file is read automatically by Claude Code whenever it starts in this
folder. It exists so a debugging session can start immediately without
re-explaining the bot's architecture.

## SINGLE SOURCE OF TRUTH (merged 2026-06-28)

This folder (`RoBotTrading man 0 V10`) is now the **only** working copy for
this project. It was created by merging two previously-separate folders:
`RoBotTrading man 0 USV9` (the primary copy, downloaded from the VPS,
git-tracked against `origin/main`, with all live runtime state) and
`RoBotTrading man 0 US` (a secondary copy that had fallen behind — confirmed
via diff to contain zero unique files; every overlapping file in it was a
strict subset of the USV9 version, missing strategies #27-31,
`symbol_normalize.py`, `bot_monitor.py`, `backtest_sim.py`,
`verify_data_sources.py`, and more). USV9's content was copied here in full
(including `.git` history, `backups/`, and live JSON state files);
`__pycache__`, `.DS_Store`, and one leftover `testwrite.tmp` were dropped as
non-source artifacts.

**Going forward**: make all changes here, in this single folder. The old
`RoBotTrading man 0 US` and `RoBotTrading man 0 USV9` folders are now
redundant — treat them as archived/historical, not as sync targets. The
sync target for the live VPS bot is now this folder (see the VPS-sync hard
rule below, which replaces the old "mirror back to US" rule).

## What this is

An MT5 (MetaTrader 5) algorithmic trading bot for XAUUSD (gold), written in
Python. It scans market data, scores multiple strategies/signals, decides
whether to enter a trade, and reports status via a generated HTML dashboard
and Telegram notifications.

## Key files

- `xauusd_mt5_strategy.py` — the bot itself: market data, indicators,
  strategy scoring, entry logic, order placement, logging, config loading.
  Runs as a long-lived process (loop), reconnecting to MT5 as needed.
- `strategy_config_ui.py` — Tkinter UI for editing `strategy_config.json`
  (the bot reloads config from disk without restarting — see
  `load_ui_config()` / `maybe_reload_config()` in the strategy file).
- `strategy_config.json` — live config: thresholds, which entry mode is
  active, Telegram bot token/chat ID. **Contains secrets — never print,
  log, or paste its full contents anywhere outside this VPS.**
- `generate_dashboard.py` — reads the JSON state files below and renders
  `dashboard.html`, a single-page live status view (scores, bias, bot
  uptime, recent trades).
- `strategy_scores.json` — per-scan snapshot written by the bot every loop
  tick: confluence scores, macro bias, and (when `ENTRY_MODE=logic_groups`)
  a `logic_groups` array with each group's bias/candidate/score.
- `bot_state.json` — written once per process launch (`record_bot_start()`):
  `{"started_at": ..., "pid": ...}`. Used by the dashboard to show uptime.
- `xauusd_mt5_strategy.log` — the bot's running log. First place to check
  when something looks wrong.

## Entry modes (config: `ENTRY_MODE`)

- `confluence13` — the original/legacy multi-strategy confluence scan
  (`run_confluence_scan()`), gated by a single global D1 "Daily Filter"
  (`DAILY_FILTER_ENABLED`, `get_daily_bias()`).
- `logic_groups` — newer engine (`run_logic_groups_scan()`) that splits
  strategies into two groups, each with its own priority-cascade trend
  filter:
  - **Day Trade** group
  - **Scalping Trade** group
  Each group computes its own bias (`get_group_bias()`) and, if not
  neutral, picks the best-ranked strategy in its pool
  (`pick_priority_strategy()`, using a League/ranking state). Which
  group(s) are active is controlled by `LOGIC_GROUP_SELECTION` ("both" /
  a single group).

**Important gotcha already fixed once**: `run_logic_groups_scan()` used to
also apply the old global D1 Daily Filter on top of each group's own bias
cascade, double-gating trades (a trade needed both filters to agree and be
non-neutral simultaneously) — this caused the bot to go quiet even when a
group's own signal was clean and decisive. Fixed via
`LOGIC_GROUPS_APPLY_DAILY_FILTER` (default `False`) — the D1 filter is now
OFF by default for `logic_groups` mode, configurable via a UI checkbox. If
the bot ever seems to be vetoing clearly-directional signals again, check
this flag first, and check the dashboard's "Logic Groups — Live Status"
panel (shows each group's bias + candidate even when nothing fires).

## How to debug a "bot isn't trading" / "bot is acting wrong" report

1. Read the tail of `xauusd_mt5_strategy.log` for the relevant time window —
   look for "veto", "neutral", "Daily filter", "Daily filter veto", error
   tracebacks, or MT5 connection errors.
2. Read `strategy_scores.json` for the latest snapshot — check
   `logic_groups` (if present) for each group's `bias` / `candidate` /
   `candidate_score`, and compare against what the log says happened.
3. Check `bot_state.json` — if `started_at` is old relative to "now" but the
   log has stopped updating, the process likely crashed or hung (dashboard
   surfaces this as EA Process = STOPPED/UNKNOWN even though uptime still
   shows the last known start time).
4. Check `strategy_config.json` for the active `ENTRY_MODE`,
   `LOGIC_GROUP_SELECTION`, `LOGIC_GROUPS_APPLY_DAILY_FILTER`, and
   `DAILY_FILTER_ENABLED` — most "why didn't it trade" questions trace back
   to one of these flags plus the bias at the time.
5. Regenerate the dashboard (`python generate_dashboard.py`) if you want a
   fresh `dashboard.html` reflecting the very latest JSON state.

## Strategies (32 total)

24 price/order-flow/macro strategies, a 25th: **Myfxbook Retail
Sentiment** (`score_myfxbook_sentiment` in `strategies.py`, fetched by
`fetch_myfxbook_sentiment()` in `macro_data.py`). Reads `data["macro"]
["myfxbook_sentiment"]` — scores 0/0 gracefully until enabled with valid
Myfxbook credentials in `strategy_config.json` under `"myfxbook"` (or the UI's
"Myfxbook Sentiment" tab). Contrarian by default (`MYFXBOOK_CONTRARIAN`) —
fades the crowd rather than following it. **Weight (0.8) kept below
`macro_bias` (1.2)** — do not raise without user approval.

A 26th: **Climax Reversal at S/R** (`score_climax_reversal_sr` in
`strategies.py`, key `climax_reversal_sr`, weight `1.0`). User-requested
pattern: a strong/extreme directional move (net move over the last 8 H1
bars >= 2.5x ATR14) that arrives at a fresh price extreme or a known swing
S/R level, then prints a rejection candle (pin bar or engulfing) — votes
the instant that reversal bar closes. Needs only H1 OHLC + atr14 (already
present every scan), no new data wiring required.

A 27th and 28th: **MTR Range Regime** (`score_mtr_range_regime`, key
`mtr_range_regime`, weight `0.9`) and **MTR Trend Regime**
(`score_mtr_trend_regime`, key `mtr_trend_regime`, weight `0.8`) — MTR-style
quantitative regime detectors using Efficiency Ratio, Wilder's ADX, a
variance-ratio test, and Donchian position (`_efficiency_ratio()`,
`_wilder_adx()`, `_variance_ratio()` helpers in `strategies.py`). Range
Regime votes Long AND Short symmetrically when the market is ranging; Trend
Regime votes directionally when a trend is confirmed — the two are
complements. Both are already registered in `strategies.py` and wired into
`strategy_config_ui.py`'s `DEFAULT_CONFIG`/`STRATEGY13_LABELS`; if
`strategy_config.json` on a given copy predates them, the UI's `_deep_merge`
backfills the missing keys on next load/save.

And a 29th: **HTF Zone + M/W Reversal** (`score_zone_mw_reversal` in
`strategies.py`, key `zone_mw_reversal`, weight `1.1`) — derived directly
from the user's uploaded gold swing-trading course material ("GOLD
Fundamentals", "Gold Live Trade and Analysis", "...Profits"). Finds a
multi-touch H4 zone (>= 2 swing highs/lows within `zone_tol_atr` x ATR(H4)
of each other — H4 stands in for the course's Weekly/Daily key-level step,
since this bot has no D1/W1 data wired in), then looks on M15 for a
double-top/double-bottom ("M/W") pattern whose second peak/trough sits at
that zone, voting the instant the latest closed M15 bar breaks the
pattern's neckline by `neckline_break_atr` x ATR(M15). Needs only H4 + M15
OHLC + atr14, both already present every scan — no new data wiring beyond
the registry entry + weight/label defaults.

A 30th and 31st: **Smart Money Sweep — Morning / Night**
(`score_smart_money_sweep` in `strategies.py`, keys
`smart_money_sweep_morning` and `smart_money_sweep_night`, weight `1.0`
each) — user-requested "เจ้ามือ liquidity sweep" detector for super-scalping,
combining 3 independent signals scored higher the more of them fire
together: (1) an M1 stop-hunt sweep of the recently-built range high/low
with a fast reclaim within a few candles — the M1-speed cousin of
`score_liquidity_sweep`, which only runs on H1; (2) a sudden shift in the
live DOM bid/ask imbalance across the last few scan ticks (new
`_DOM_IMBALANCE_HISTORY` module state, since `score_order_flow_dom` only
ever looks at one snapshot in isolation) — this signal never votes alone,
only adds conviction on top of (1) or (3); (3) an abnormal single-candle
M1 spike (>= 2.5x ATR) with a one-sided wick that closes back, the
fast-timeframe cousin of `score_climax_reversal_sr` minus its 8-bar
lead-in requirement. Registered twice with different session windows:
morning ~07:00-10:00 and night ~02:00-04:00.

**Timezone gotcha specific to these two strategies**: their session
windows are plain **Thai/Bangkok local hours**, NOT broker UTC+3 like the
`scalp_london_sweep`/`scalp_ny_orb` defaults. This VPS's
`build_market_data()` sets `data["now"] = datetime.now()`, and this
machine's clock is documented elsewhere in this file (and in
`xauusd_mt5_strategy.py`'s module docstring, "Trading-hours filter"
section) as being set to **Thailand time (UTC+7)** — so `data["now"]` is
already Bangkok wall-clock time, not broker time. The existing
`scalp_*` strategies' docstrings describe their defaults as broker-time
windows but compare them directly against this same Thai-time
`data["now"]` with no conversion — this looks like a **pre-existing
inconsistency** between those docstrings and actual runtime behavior. It
was flagged, not fixed, when `score_smart_money_sweep` was added (fixing
it would change when the older scalp strategies fire, which wasn't asked
for). If "why didn't my scalp strategy fire at the time I expected"
comes up, check this first.

A 32nd: **Fibonacci Confluence S/R (Major+Minor Swing)** (`score_fib_confluence_sr` in
`strategies.py`, key `fib_confluence_sr`, weight `1.2`) — implements the user-supplied
"Fibonacci Level.docx" reference as a computable strategy. Finds the most recent major
swing leg on H4 and minor swing leg on H1, computes the full Fibonacci level table for
each (retracement 0-100%, extensions 127/161.8/200/261.8/300%, negative extensions),
then finds price zones where a major-swing Fibonacci level and a minor-swing level land
within 0.35×ATR of each other (confluence). Each zone gets bonus confirmation points
for co-location with an EMA/SMA, a prior horizontal S/R swing, or a trendline/channel
boundary (per the doc's "do not use Fibonacci alone" rule). Nearest confirmed SUPPORT
zone below price → Long vote; nearest RESISTANCE zone above price → Short vote. Reads
`data["fib_confluence"]` pre-computed by `get_fib_confluence_safe()` in
`xauusd_mt5_strategy.py` (same try/except pattern as `get_macro_snapshot_safe()` for
macro_bias). The standalone module `fib_confluence.py` also appends every computed
swing leg + zone set to `fib_confluence_history.db` (SQLite, audit trail) and persists
OHLC bars to `price_bars` table for backtesting (mirrors `macro_data_history.db`
rationale). Uses only H4 + H1 OHLC + indicators already present every scan — no new
MT5 data wiring required.

## Hard rules (apply even when debugging)

- Never print, log, or transmit the Telegram `bot_token` / `chat_id`, or the
  Myfxbook `email` / `password`, from `strategy_config.json` beyond this VPS.
- Never place a real trade, modify lot sizing, or change risk parameters
  without the user explicitly confirming first — even if a "fix" seems
  obviously correct.
- Prefer additive, config-flag-driven fixes (matching the existing pattern:
  `DAILY_FILTER_ENABLED`, `LOGIC_GROUP_SELECTION`,
  `LOGIC_GROUPS_APPLY_DAILY_FILTER`) over silently deleting old behavior, so
  the user can switch back without another code change.
- This folder is the single local working copy (see "SINGLE SOURCE OF
  TRUTH" note at the top). The live bot itself still runs on a separate
  VPS — changes made here need to be synced TO the VPS (not the reverse)
  before they take effect live. Use the existing `VPS_SYNC_*_PROMPT.md`
  files in this folder as the template for writing a new sync prompt
  whenever a change here needs to reach the VPS; don't assume a local edit
  is already live just because it's been made here.
