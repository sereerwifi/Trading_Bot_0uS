# XAUUSD MT5 Trading Bot — Project Context

This file is read automatically by Claude Code whenever it starts in this
folder. It exists so a debugging session can start immediately without
re-explaining the bot's architecture.

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

## Hard rules (apply even when debugging)

- Never print, log, or transmit the Telegram `bot_token` / `chat_id` from
  `strategy_config.json` beyond this VPS.
- Never place a real trade, modify lot sizing, or change risk parameters
  without the user explicitly confirming first — even if a "fix" seems
  obviously correct.
- Prefer additive, config-flag-driven fixes (matching the existing pattern:
  `DAILY_FILTER_ENABLED`, `LOGIC_GROUP_SELECTION`,
  `LOGIC_GROUPS_APPLY_DAILY_FILTER`) over silently deleting old behavior, so
  the user can switch back without another code change.
- This project also exists as a local working copy at
  `RoBotTrading man 0 US` (no `V9` suffix) — changes made here on the VPS
  copy (`...USV9`) should eventually be mirrored back if the user wants them
  kept in sync; flag this rather than assuming it's already handled.
