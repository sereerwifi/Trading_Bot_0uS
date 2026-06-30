# Prompt for Claude Code — fix the 2026-06-28 audit findings

Paste this into Claude Code running inside `RoBotTrading man 0 V10`. Read
`CLAUDE.md` first. This fixes the issues found in `AUDIT_REPORT_2026-06-28.md`
(already in this folder — read it in full before starting). Apply fixes in
the order below. Do not touch anything not listed here. Do not restart the
live bot — it currently runs from a separate VPS copy (`...USV9`); these are
local V10 edits only and need a sync prompt afterward to reach the VPS (see
the last section).

## Why B1 matters most

All 7 of the most recently added strategies — `myfxbook_sentiment`,
`climax_reversal_sr`, `mtr_range_regime`, `mtr_trend_regime`,
`zone_mw_reversal`, `smart_money_sweep_morning`, `smart_money_sweep_night` —
are scored every scan but **cannot trigger a trade** in `logic_groups` mode
because `pick_priority_strategy()` only picks from `DAY_TRADE_STRATEGIES` /
`SCALP_STRATEGIES`, and none of the 7 are in either list. Fix this first.

## Fix 1 (B1) — add all 7 newest strategies to the entry pools

In `xauusd_mt5_strategy.py`, around line 304-317:

```python
DAY_TRADE_STRATEGIES = [
    "bb_breakout", "macd_cross", "opening_range_breakout", "price_action",
    "vwap_rejection", "rsi_divergence", "atr_donchian_breakout", "fair_value_gap",
    "fibonacci", "multi_tf_align", "news_fade", "scalp_ema_pullback",
    "ema_cross", "liquidity_sweep", "bos_choch", "order_block",
    "climax_reversal_sr", "zone_mw_reversal", "mtr_trend_regime", "mtr_range_regime",
]
```

```python
SCALP_STRATEGIES = [
    "scalp_ema_pullback", "london_breakout", "scalp_combo_sweep",
    "scalp_ny_orb", "scalp_london_sweep",
    "smart_money_sweep_morning", "smart_money_sweep_night",
]
```

`climax_reversal_sr`, `zone_mw_reversal`, `mtr_trend_regime`, and
`mtr_range_regime` all run on H1/H4/M15 — the Day Trade group's cadence.
`smart_money_sweep_morning`/`_night` run on M1 and are session-gated — the
Scalp group's cadence. This was the user's explicit instruction: every
strategy added so far should be a live entry candidate, not just an
informational score.

**`myfxbook_sentiment` is the one exception — leave it out of both pools.**
It's a contrarian sentiment overlay with no price-action trigger of its own
(it scores off retail positioning data, not a chart pattern or breakout), so
it isn't the kind of thing that should "win" a scan and set SL/TP — it's
designed to add/subtract conviction alongside other strategies, the same
role `order_flow_dom`/`macro_bias` already play as bias-setters. If the user
wants it added to an entry pool anyway, that's a one-line addition to either
list, but flag it back to the user rather than assuming.

## Fix 2 (B2) — add missing weights

In `_RECOMMENDED_STRATEGY_WEIGHTS` (~line 354), add:

```python
"mtr_range_regime": 0.9, "mtr_trend_regime": 0.8,
```

(Both are already present in `DEFAULT_CONFIG`/UI at these values — this just
makes the Python-side recommended-weights dict agree, so a fresh
`STRATEGY_WEIGHTS` backfill doesn't silently use 1.0 instead.)

## Fix 3 (B3) — remove the duplicate-pool strategy

In `DAY_TRADE_STRATEGIES` (~line 307), remove `"scalp_ema_pullback"` — it's
an M1 scalp strategy and should only live in `SCALP_STRATEGIES` (~line 314),
where it already is. Leaving it in `DAY_TRADE_STRATEGIES` lets it fire with
day-trade SL/TP sizing, which doesn't match its M1 structure.

## Fix 4 (B4) — fix the misleading log line

Around line 752, the log line uses the legacy `ENABLED_STRATEGIES` variable
(only ever contains `{"fib_confluence"}` plus whatever `legacy_strategies_cfg`
sets, which is dead config) instead of the real `CONFLUENCE_ENABLED_STRATEGIES`
set (all 31 current strategy keys). Change:

```python
logger.info(f"Loaded config from {path}. Enabled strategies: {sorted(ENABLED_STRATEGIES)}")
```

to log `sorted(CONFLUENCE_ENABLED_STRATEGIES)` instead. Also find and
remove/gate whatever downstream check warns "no entries will fire" if
`fib_confluence` is absent from `ENABLED_STRATEGIES` — that key isn't in
`STRATEGY_REGISTRY` at all anymore, so the warning is always-false and
purely noise.

## Fix 5 (B5) — backfill the config file

`strategy_config.json` only has 26 entries under `confluence.strategies`;
missing the 5 newest (`mtr_range_regime`, `mtr_trend_regime`,
`zone_mw_reversal`, `smart_money_sweep_morning`, `smart_money_sweep_night`).
Don't hand-edit the JSON — run `strategy_config_ui.py`, let `_deep_merge`
backfill the missing keys on load, then save. Confirm afterward (by reading
the structure, not printing secrets) that all 31 registry keys now exist
under `confluence.strategies` with the weights from Fix 2 / existing
defaults, not 1.0 placeholders.

## Fix 6 (I3) — `logic_groups` ignoring the UI's enabled/disabled toggle

`run_logic_groups_scan()` calls `score_all(enabled_keys=set(STRATEGY_REGISTRY.keys()))`
— every registered strategy gets scored and can influence group bias
regardless of its `enabled` flag in the UI. Change that call to use
`enabled_keys=CONFLUENCE_ENABLED_STRATEGIES` instead, matching what
`confluence13` mode already respects. This is a behavior change (a
currently-always-on strategy could now turn off if a user unchecks it) —
additive/config-driven and matches existing UI expectations, but flag it
explicitly in your final report since it does change live behavior the
moment someone unchecks a box.

## Do NOT auto-fix — decisions needed from the user first

- **I1** — `macro_bias` weight disagreement (`_RECOMMENDED_STRATEGY_WEIGHTS`
  says 1.2, `DEFAULT_CONFIG` says 0.6, live config always wins at 0.6). Don't
  change either value; just report that this is the status quo and ask
  whether the recommended-weights comment should be corrected to 0.6 to stop
  being misleading, or whether the user actually wants to raise the live
  weight to 1.2 (a real behavior change to risk/scoring — needs explicit
  go-ahead per the project's hard rule).
- **I2** — UTC+3-documented session strategies (`london_breakout`,
  `opening_range_breakout`, `scalp_london_sweep`) running against the
  UTC+7 Thai-time clock, firing ~4h later than documented. This is a
  pre-existing, previously-flagged, intentionally-unfixed mismatch — do not
  change session-time math without the user explicitly asking for it.

## Cosmetic batch (C1-C6) — safe to fix together, no behavior change

Update these doc/comment strings to say "31" instead of "24"/"13":
- `xauusd_mt5_strategy.py` ~1975 (`run_confluence_scan()` docstring)
- `xauusd_mt5_strategy.py` ~357 (comment above `CONFLUENCE_ENABLED_STRATEGIES`)
- `xauusd_mt5_strategy.py` ~253 (ENTRY_MODE comment)
- `strategy_config_ui.py` ~186 (`DEFAULT_CONFIG` comment)
- `strategy_config_ui.py` ~297 (`STRATEGY13_LABELS` comment)
- `strategies.py` module docstring

## Verification before reporting done

1. `python -m py_compile *.py` — clean compile.
2. Re-derive and print (not the JSON, just counts): `len(STRATEGY_REGISTRY)`,
   `len(DAY_TRADE_STRATEGIES)`, `len(SCALP_STRATEGIES)`, confirm all 31 keys
   appear in at least one of {`DAY_TRADE_STRATEGIES`, `SCALP_STRATEGIES`,
   "intentionally excluded — myfxbook_sentiment"} — i.e. nothing is silently
   still orphaned.
3. Confirm `scalp_ema_pullback` now appears in exactly one list.
4. Confirm `strategy_config.json` now has 31 entries under
   `confluence.strategies` (count only, don't print contents).
5. Re-run the read-only audit checks from `AUDIT_PROMPT_FULL_CODEBASE.md`
   Step 1 against the fixed files to confirm B1/B2/B3/B4/B5/I3 are resolved
   and nothing new broke.

## After fixing: this is still V10-only

Per the operational note in the audit report, the live bot runs from
`RoBotTrading man 0 USV9` on the VPS, not this folder — these fixes have
**zero live effect** until synced over. Once these fixes are verified here,
write (or extend) a `VPS_SYNC_*_PROMPT.md` in this same style covering the
6 fixes above, for a future Claude Code session running directly on the VPS.
Do not restart the live bot yourself, on the VPS or otherwise — that's the
user's call.
