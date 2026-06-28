# XAUUSD MT5 Bot — Audit Report (2026-06-28)

## Critical Operational Note

The live bot is still running from `RoBotTrading man 0 USV9`, not V10. The log confirms:
```
Loaded config from C:\Users\Administrator\Desktop\RoBotTrading man 0 USV9\strategy_config.json
```
**Every code change made in V10 has zero effect until the bot is switched over or synced.** The bot also appears currently stopped (last log entry 13:41 today, 37-hour gap before that from 2026-06-27 04:01).

---

## BROKEN — needs a fix

**B1 — Strategies 25–31 absent from `logic_groups` entry pools** (`xauusd_mt5_strategy.py`, lines 304–317)

`DAY_TRADE_STRATEGIES` (16 entries) and `SCALP_STRATEGIES` (5 entries) both omit: `myfxbook_sentiment`, `climax_reversal_sr`, `mtr_range_regime`, `mtr_trend_regime`, `zone_mw_reversal`, `smart_money_sweep_morning`, `smart_money_sweep_night`. In `logic_groups` mode, `pick_priority_strategy()` only picks from its pool — these 7 strategies are **scored every scan but can never trigger a trade**. This is the highest-priority finding.

Fix: add `climax_reversal_sr` and `zone_mw_reversal` to `DAY_TRADE_STRATEGIES`; add `smart_money_sweep_morning` / `_night` to `SCALP_STRATEGIES`. Decide whether `myfxbook_sentiment`, `mtr_range_regime`, `mtr_trend_regime` are entry triggers or informational-only.

---

**B2 — `mtr_range_regime` and `mtr_trend_regime` missing from `_RECOMMENDED_STRATEGY_WEIGHTS`** (`xauusd_mt5_strategy.py`, line ~354)

All other 29 strategies have explicit weights. These two fall back to 1.0 on the backfill path; intended weights per `DEFAULT_CONFIG` are 0.9 and 0.8 respectively.

Fix: add `"mtr_range_regime": 0.9, "mtr_trend_regime": 0.8` to `_RECOMMENDED_STRATEGY_WEIGHTS`.

---

**B3 — `scalp_ema_pullback` appears in both `DAY_TRADE_STRATEGIES` and `SCALP_STRATEGIES`** (`xauusd_mt5_strategy.py`, lines 307 and 314)

A scalp-specific M1 strategy can fire as a "day trade" candidate with day-trade SL/TP parameters — wrong position sizing for that strategy.

Fix: remove `scalp_ema_pullback` from `DAY_TRADE_STRATEGIES`.

---

**B4 — "Enabled strategies" log line shows a dead legacy variable** (`xauusd_mt5_strategy.py`, line ~752)

`ENABLED_STRATEGIES` logs 10 old defunct key names (`fib_confluence`, `mtf_alignment`, etc.) not in `STRATEGY_REGISTRY`. The actual scoring set is `CONFLUENCE_ENABLED_STRATEGIES` (all 31 keys). A spurious warning fires if `fib_confluence` is absent, claiming "no entries will fire" — which is false.

Fix: change the log line to use `CONFLUENCE_ENABLED_STRATEGIES`; gate or remove the `fib_confluence` warning.

---

**B5 — V10's `strategy_config.json` missing 5 strategies**

Config has 26 entries under `confluence.strategies`; missing `mtr_range_regime`, `mtr_trend_regime`, `zone_mw_reversal`, `smart_money_sweep_morning/night`. Backfill via `load_ui_config` adds them at weight 1.0 (not their intended weights).

Fix: open `strategy_config_ui.py`'s UI and save — regenerates the config with all 31 entries and correct weights.

---

## INCONSISTENT — works but contradicts docs or cross-file wiring

**I1 — `macro_bias` weight: `_RECOMMENDED_STRATEGY_WEIGHTS` says 1.2, `DEFAULT_CONFIG` says 0.6**
Live effective weight is always 0.6 (loaded from JSON). The 1.2 value is only active for milliseconds at import.

**I2 — Session strategies compare UTC+3 docstring windows against UTC+7 Thai-time clock**
`score_london_breakout`, `score_opening_range_breakout`, `score_scalp_london_sweep` describe windows in "broker UTC+3" but `data["now"] = datetime.now()` is Thai time (UTC+7). These strategies fire 4 hours late relative to the documented windows. `score_smart_money_sweep` and `score_scalp_ny_orb` are correct (explicitly Bangkok/Thai time). This is the known mismatch flagged in CLAUDE.md — confirmed still present, still the only one beyond `scalp_ny_orb`.

**I3 — `run_logic_groups_scan` ignores per-strategy `enabled` flag**
Calls `score_all(enabled_keys=set(STRATEGY_REGISTRY.keys()))` — bypasses the `enabled: False` setting in the UI. Users who disable a strategy in the UI will still have it scored and influencing bias in `logic_groups` mode.

Fix: pass `enabled_keys=CONFLUENCE_ENABLED_STRATEGIES` instead.

**I4 — Live scores snapshot shows `apply_daily_filter: true` (the double-gating bug)**
The running USV9 bot briefly had this re-enabled today (logs show Day Trade signals vetoed 13:37–13:40). V10 config has `False`; USV9 has since been corrected. Operational note only — documented.

**I5 — Dashboard "candidate" field doesn't show which bias-priority strategy set the direction**
When `macro_bias` or `order_flow_dom` set the group bias, the candidate shown is the entry strategy, not the bias-setter. No logic bug; a transparency gap.

---

## COSMETIC / DOC-ONLY

| ID | File | Issue |
|----|------|-------|
| C1 | `xauusd_mt5_strategy.py` ~1975 | `run_confluence_scan()` docstring says "24 strategies", should be 31 |
| C2 | `xauusd_mt5_strategy.py` ~357 | Comment says "now 24 confluence strategies", should be 31 |
| C3 | `xauusd_mt5_strategy.py` ~253 | ENTRY_MODE comment says "confluence13 (default)" and "13 strategies" |
| C4 | `strategy_config_ui.py` ~186 | DEFAULT_CONFIG comment says "all 24 scores" |
| C5 | `strategy_config_ui.py` ~297 | `STRATEGY13_LABELS` comment running tally still partial, total now 31 |
| C6 | `strategies.py` module docstring | Says "24 price/order-flow/macro strategies" |

---

## Priority Summary

| # | Finding | Impact |
|---|---------|--------|
| 1 | **B1** — 7 strategies never trigger trades in `logic_groups` mode | Silent — all newly added strategies are dead as entry signals |
| 2 | **B4** — log line shows wrong variable; spurious warning | Misleading ops log every config reload |
| 3 | **B3** — `scalp_ema_pullback` in both pools | Wrong SL/TP parameters when day-trade group picks it |
| 4 | **B2** — `mtr_*` weights wrong on backfill path | 1.0 instead of 0.9/0.8 |
| 5 | **B5** — V10 config missing 5 strategies | Fix by saving from UI |
| 6 | **I3** — `enabled: false` ignored in `logic_groups` | UI disable has no effect in active mode |
| 7 | **I1** — `macro_bias` weight 1.2 vs 0.6 | User sees 0.6 live, code says 1.2 recommended |
| 8 | **I2** — UTC+3 session windows vs UTC+7 clock | london/opening-range strategies fire 4h late |
| 9 | **Operational** — bot still on USV9 | V10 edits are inert until sync/switchover |

---

*Nothing has been changed. Awaiting go-ahead on which findings to fix and in what order.*
