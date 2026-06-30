# Changelog — XAUUSD MT5 Trading Bot

All notable changes to this project are documented here, newest first.

---

## [53e2e40] 2026-06-30 — Full Code Audit: 37 Findings Fixed (14 files)

Complete audit of all 22 Python files. Every finding addressed in a single
commit. See `AUDIT_REPORT_2026-06-30.md` for the full annotated report.

### Critical bug fixes
- **harmonic_patterns.py** — Direction assignment was inverted (X=HIGH was
  labeled "bullish" — every signal fired backwards since strategy #33 was
  added). Fixed to Carney convention: X=LOW→bullish, X=HIGH→bearish.
- **harmonic_patterns.py** — PRZ `d_from_cd` formula projected in the wrong
  direction from C, causing PRZ convergence score to always be near zero.
  Fixed: `C + cd_mid * (C - B)` → `C + cd_mid * (B - C)`.
- **xauusd_mt5_strategy.py** — `timedelta` was never imported; every manual
  position close triggered a `NameError` silently swallowed by the outer
  `except`, dropping those trades from P&L history.
- **xauusd_mt5_strategy.py** — Legacy `confluence13` short fib zone condition
  had `fibs["50.0"]` and `fibs["61.8"]` swapped, making the zone always
  `False` for down-legs (no `fib_confluence` shorts ever entered).
- **xauusd_mt5_strategy.py** — `save_scores_snapshot` / `log_all_strategy_scores_debug`
  were called before the league ranking sort in `run_logic_groups_scan`, so
  `direction_taken` logged the unranked winner rather than the actual chosen
  strategy when both groups fired simultaneously. Moved to after the sort.
- **xauusd_mt5_strategy.py** — `mt5.account_info()` returns `None` on
  transient disconnect; `run_confluence_scan`, `run_logic_groups_scan`, and
  `run_once` all passed `None` directly to `check_drawdown_breaker` /
  `info.balance`, causing an `AttributeError` crash. None guards added to all
  three call sites.
- **xauusd_mt5_strategy.py** — `send_order` called `mt5.symbol_info_tick()`
  twice (once for ask, once for bid) with no `None` check; a transient tick
  failure would raise `AttributeError`. Now a single call with a None guard.
- **xauusd_mt5_strategy.py** — `check_daily_status_notify` called
  `account_info()` but never checked for `None`, crashing daily heartbeat
  messages on disconnect.
- **fib_confluence.py** — `level_label()` showed `-61.8` instead of `-161.8`
  for negative extensions (formula used `abs(ratio) * 100` instead of
  `(abs(ratio) + 1.0) * 100`).
- **fib_confluence.py** — `save_price_bars` included the forming (unclosed)
  bar in the saved set, so the most recent bar was always partially-written
  data. Fixed: `tail(n+1).iloc[:-1]`.
- **validate_engulfing.py** — `ci_overlap` used `ci2[0] < ci1[0]` instead of
  `ci2[0] < ci1[1]`, making the overlap check always `False` when `ci2[0] > ci1[0]`.

### Security fixes
- **dashboard_server.py** — Auth comparison used `==` (timing oracle). Changed
  to `hmac.compare_digest()` for both username and password fields.
- **dashboard_server.py** — Missing `dashboard.html` returned HTTP 200 with an
  error page, masking the problem. Now returns 503 + `Retry-After: 30`.
- **backup_restore.py** — `restore_backup` extracted zip members without
  checking whether their resolved paths escaped the target directory (zip-slip).
  Added `os.path.realpath` check — any member that would write outside
  `target_dir` is silently skipped.
- **backup_restore.py** — `zf.write()` in `create_backup` had no `OSError`
  guard; a file disappearing mid-zip (EA write race) aborted the entire backup.
  Each file is now tried individually; failures are skipped.

### Reliability fixes
- **backup_restore.py** — `BACKUP_FILES` was missing the three history databases
  added in previous commits (`fib_confluence_history.db`,
  `harmonic_patterns_history.db`, `strategy_scores_history.db`) and
  `shadow_positions.json`. All four now included.
- **bot_monitor.py** — Fallback `is_bot_running()` checked for `python.exe` in
  the process list, which always returned `True` because `bot_monitor` itself
  is a Python process. Changed to check for `xauusd_mt5_strategy` in the
  command line via `wmic`.
- **bot_monitor.py** — `restart_bot()` launched only the EA trading loop
  (`xauusd_mt5_strategy.py`), leaving the dashboard watcher, web server,
  Cloudflare tunnel, and backup watcher dead. Now calls
  `launch_bot.py --autostart` to restart the full stack.
- **bot_monitor.py** — Alert cooldown used `cooldown_ok OR msg_changed`,
  meaning any message change bypassed the 5-minute cooldown and sent a burst
  of alerts during status flapping. Changed to `cooldown_ok AND (msg_changed
  OR first_alert)`.
- **telegram_alert.py** — `format_order_alert` and `format_signal_alert` used
  `signal.get('entry'):.2f` directly; a `None` value raised `TypeError`.
  Now guarded with `or 0.0` fallback.
- **fib_confluence.py** / **harmonic_patterns.py** — Added WAL journal mode
  (`PRAGMA journal_mode=WAL`) to `_db_connect()` in both modules to reduce
  writer-reader lock contention during concurrent bot scans.
- **fib_confluence.py** — `get_price_bars()` and `get_confluence_history()`
  leaked the SQLite connection on exception. Fixed with `conn = None` +
  `finally: if conn: conn.close()` pattern.
- **harmonic_patterns.py** — `get_pattern_history()` had the same connection
  leak. Same fix applied.

### Config / UI fixes
- **strategy_config_ui.py** — `macro_bias` default weight was `0.6`; the
  correct intended value per the project spec is `1.2` (highest-weighted
  strategy). Fixed in `DEFAULT_CONFIG`.
- **strategy_config_ui.py** — `debug_log_all_strategy_scores` and
  `debug_log_all_strategy_scores_every_n` were missing from `DEFAULT_CONFIG`'s
  `"logging"` section; new installs or fresh configs never enabled the full
  score history logging added in the post-mortem fix.
- **strategy_config_ui.py** — When `strategy_config.json` failed to parse
  (corrupt file, manual edit error), the UI silently fell back to factory
  defaults with no indication. Now shows a `messagebox.showwarning` dialog so
  the user knows their saved settings are not active.
- **strategy_config_ui.py** — `_TypedStringVar.get()` returned the raw string
  when numeric conversion failed, allowing an invalid value to propagate into
  the config silently. Now raises `ValueError` with a clear message.

### Data / analysis fixes
- **macro_data.py** — `_cached()` failure path wrote a new cache entry with
  `ts=now` but lost the `last_good_ts` of the previous good record. Once the
  failure record itself expired (next TTL cycle), `stale_secs` could never be
  computed and stale alerts permanently stopped firing during extended outages.
  Fixed by carrying `last_good_ts` forward in every failure record.
- **macro_data.py** — Error strings embedded in Telegram HTML messages were not
  HTML-escaped; angle brackets in `urllib` error messages corrupted the message
  layout. Now escaped before embedding.

### Module-level side-effect fixes
- **auto_minimize.py** — All execution was at module level; importing the
  module triggered a 60-second `time.sleep` and then minimized every window.
  Entire execution body wrapped in `if __name__ == "__main__":`.
- **diagnose_macro_data.py** — Same issue: connectivity check ran on import.
  Wrapped in `if __name__ == "__main__":`.
- **backtest_sim.py** — MT5 `initialize()`, `ea.load_ui_config()`, and the
  entire simulation walk all ran at module level. Fully guarded with
  `if __name__ == "__main__":`. Also added `data["fib_confluence"]` and
  `data["harmonic"]` to `build_data()` so strategies #32 (Fib Confluence)
  and #33 (Harmonic Patterns) score correctly in backtest rather than
  silently scoring 0.

### Other
- **xauusd_mt5_strategy.py** — Removed redundant `import pandas as pd` inside
  `_mtr_is_danger()` (module-level import already present).
- **stress_test_engulfing.py** — Removed dead import of `three_white_soldiers`
  (function was removed from `analyze_candlestick_patterns.py` in a prior
  refactor but the import was left behind, causing an `ImportError`).

---

## [02ef72b] 2026-06-30 — Post-Mortem Fixes: 3 Runtime Bugs from Reversal Analysis

Analysis of the live VPS bot's 09:05–10:30 reversal
(`ANALYSIS_REVERSAL_2026-06-30_0905-1030.md`) surfaced three real issues.

- **fib_confluence.py** — `save_price_bars()` existed and was documented as
  called from `get_fib_confluence_safe()`, but the call was never wired in.
  The `price_bars` table was always empty. Fixed: call added in its own
  `try/except` so a save failure can't break scoring.
- **xauusd_mt5_strategy.py** — `logic_groups` mode only logged each group's
  winning strategy per scan, not all 33 scores. Added
  `log_all_strategy_scores_debug()` which appends every strategy's full score
  dict to `strategy_scores_history.db` every scan via `get_strategy_scores_history()`.
  Gated by `DEBUG_LOG_ALL_STRATEGY_SCORES` (default `True`) and throttled by
  `DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N` (default `1`), both configurable
  via `strategy_config.json`'s `"logging"` section.
- **harmonic_patterns.py** — `_RATIO_TOL` widened from `0.07` to `0.09` after
  zero XABCD matches across 135 consecutive live scans in the analyzed window.
  PRZ-convergence and fib-confluence cross-check filters still prevent weak
  matches from generating entries.

---

## [4204f5f] 2026-06-29 — launch_bot.py: add strategy_config_ui.py to kill_stale targets

Added `strategy_config_ui.py` to the `TARGETS` list swept by
`kill_stale_processes` on launch/stop, preventing orphaned UI processes from
accumulating across restarts.

---

## [0bf9022] 2026-06-29 — Add --autostart mode for UI-sequenced bot startup

`launch_bot.py --autostart` bypasses the normal interactive prompt so the bot
can be restarted programmatically (e.g. by `bot_monitor.py`) through the full
launch sequence — dashboard watcher, web server, Cloudflare tunnel, backup
watcher — not just the EA trading loop.

---

## [d905b03] 2026-06-29 — Strategy #33: Harmonic Patterns (XABCD)

New standalone module `harmonic_patterns.py`. Detects classic XABCD harmonic
patterns (Gartley, Bat, Butterfly, Crab, Deep Crab, Cypher) on H1 using an
ATR-based zigzag swing detector. Scores each pattern by PRZ convergence (two
independent D projections) and alignment with Fibonacci Confluence zones
(strategy #32). Only votes once price reaches the PRZ and a rejection candle
(pin bar or engulfing) closes there. Appends every scan's best match to
`harmonic_patterns_history.db`. Weight: `1.3`.

---

## [32aa276] 2026-06-29 — Strategy #32: Fibonacci Confluence S/R (Major+Minor Swing)

New standalone module `fib_confluence.py`. Finds the most recent major swing
on H4 and minor swing on H1, computes full Fibonacci tables for each (0–300%
retracement and extensions), and identifies zones where a major- and minor-
swing level land within 0.35×ATR of each other. Zones get bonus confirmation
points for co-location with an EMA/SMA, prior horizontal S/R, or a trendline.
Nearest confirmed support → Long; nearest resistance → Short. Audit trail
written to `fib_confluence_history.db`. Weight: `1.2`.

---

## [da2f461] 2026-06-29 — Dashboard: 60-second auto-refresh + countdown timer

`dashboard.html` now auto-refreshes every 60 seconds with a visible countdown
so it stays current without manual browser refresh.

---

## [2ef1991] 2026-06-29 — Fix dashboard win-rate display; fix macro docstring

- Win-rate column no longer shows misleading percentages when there are zero
  completed trades (showed `0.0%` instead of `n/a`).
- `_macro_bull_score` docstring corrected to reflect actual weighting table.

---

## [58d66ea] 2026-06-29 — Dashboard: Performance Statistics panel

New panel in `dashboard.html` showing per-strategy win count, loss count, and
win rate drawn from `strategy_scores_history.db`.

---

## [1ce56e6] 2026-06-29 — Macro: rebalance Gold Decision Matrix weights

COMEX registered inventory weight raised from 5% to 10%; Fed expectation
(2Y proxy) weight lowered from 20% to 15%. Rationale: COMEX was systematically
under-represented relative to its predictive value for gold spot price.

---

## [3ce9c62] 2026-06-29 — Fix MTR Range Regime entry-at-extremes bug; UI-only start

- `score_mtr_range_regime` was voting Long even when price was already at
  the top of the range (entry at the Donchian high, worst possible
  risk/reward for a mean-reversion long). Fixed to block entries within
  one ATR of the range extreme.
- Enforced that the bot can only be started through the UI sequence
  (not by running `xauusd_mt5_strategy.py` directly) to ensure the full
  launch stack always initialises together.

---

## [9b6e400] 2026-06-28 — Strategies #27–31; audit fixes B1–B5/I3; session docs

### New strategies
- **#27 MTR Range Regime** (`mtr_range_regime`, weight `0.9`) — quantitative
  range detector using Efficiency Ratio, Wilder ADX, variance-ratio test, and
  Donchian position. Votes Long and Short symmetrically when the market is
  ranging.
- **#28 MTR Trend Regime** (`mtr_trend_regime`, weight `0.8`) — directional
  complement to #27; votes in the trend direction when a trend is confirmed.
- **#29 HTF Zone + M/W Reversal** (`zone_mw_reversal`, weight `1.1`) — finds
  multi-touch H4 zones (≥2 swing highs/lows within `zone_tol_atr` × ATR),
  then looks on M15 for a double-top/double-bottom whose second peak sits at
  that zone; votes when the neckline breaks.
- **#30 Smart Money Sweep — Morning** (`smart_money_sweep_morning`, weight `1.0`,
  session 07:00–10:00 Thai time) — M1 liquidity sweep + DOM imbalance + M1
  spike detector.
- **#31 Smart Money Sweep — Night** (`smart_money_sweep_night`, weight `1.0`,
  session 02:00–04:00 Thai time) — same logic, different session window.

### Audit fixes (B1–B5, I3)
- B1: `run_logic_groups_scan` re-applied the global D1 Daily Filter on top of
  each group's own bias cascade, double-gating entries. Fixed via
  `LOGIC_GROUPS_APPLY_DAILY_FILTER` (default `False`).
- B2–B5: Various strategy weight defaults corrected in `DEFAULT_CONFIG`.
- I3: Trading-hours session documentation added to `xauusd_mt5_strategy.py`
  module docstring.

---

## [4dccbb0] 2026-06-28 — Update hardcoded paths from USV9 to V10

`RoBotTrading man 0 USV9` merged into `RoBotTrading man 0 V10` as the single
source of truth. All hardcoded folder references updated.

---

## [ab4760c] 2026-06-25 — Real market-open detection + price sanity check

Bot now checks MT5's `symbol_info().session_*` fields to detect whether the
market is actually open before scanning. Added price sanity check (rejects
ticks that are > 5× ATR from the last known price as a feed-error guard).

---

## [29d5bd5] 2026-06-25 — Add symbol_normalize.py

New utility module that canonicalises gold symbol names across broker
conventions (`XAUUSD`, `XAUUSDm`, `GOLD`, `XAUUSD.pro`, etc.) and provides
a single `display_label()` function used by Telegram alerts and the dashboard.

---

## [9b824e0] 2026-06-25 — Strategy #26: Climax Reversal at S/R

Detects a strong/extreme directional move (net move over last 8 H1 bars ≥
2.5×ATR14) arriving at a fresh price extreme or known swing level, then
prints a rejection candle (pin bar or engulfing). Votes the instant the
reversal bar closes. Uses only H1 OHLC + atr14. Weight: `1.0`.

---

## [724ac85] 2026-06-25 — Fix false STOPPED status in dashboard

Bot-running detection was log-file-only; a log rotation or slow write could
trigger a false STOPPED alert. Changed to check process PID (from
`bot_state.json`) and `strategy_scores.json` freshness together.

---

## [f66fdec] 2026-06-25 — Fix Thai console encoding (reconfigure)

Previous fix double-wrapped `stdout`, causing a `TextIOWrapper` error on
Python 3.11+. Changed to `sys.stdout.reconfigure(encoding="utf-8")`.

---

## [d30c2e1] 2026-06-25 — Fix NameError: missing `import sys`

`sys` was used in the UTF-8 console handler added by the Thai encoding commit
but not imported. Caused an immediate `NameError` on startup.

---

## [76504c0] 2026-06-25 — Strategy #25: Myfxbook Retail Sentiment

Contrarian retail-sentiment strategy reading live crowd long/short ratios
from the Myfxbook Community Outlook API. Fades the crowd rather than
following it (`MYFXBOOK_CONTRARIAN` default `True`). Requires valid
Myfxbook credentials in `strategy_config.json`. Scores 0/0 gracefully until
configured. Weight: `0.8` (kept below `macro_bias` 1.2).

---

## [156aa7f] 2026-06-25 — Show bot version in UI title bar (v9.0)

`strategy_config_ui.py` now displays `v9.0` in the window title bar.

---

## [8e2badc] 2026-06-25 — Add run_claude.bat

Convenience launcher: double-clicking `run_claude.bat` opens Claude Code
(the AI coding assistant) in this project directory.

---

## [a95b4ed] 2026-06-25 — Thai language support

- UI fonts changed to ones that render Thai characters correctly.
- Console output re-encoded to UTF-8 on startup.
- All `json.dump` / `logger` calls use `ensure_ascii=False` so Thai text in
  strategy labels and Telegram messages is preserved as readable characters
  rather than `\uXXXX` escapes.

---

## [2700471] 2026-06-24 — Add kill_stale_processes

`launch_bot.py` now sweeps orphaned Python processes matching known bot
script names before launching new ones, preventing duplicate-bot scenarios
after abnormal terminations.
