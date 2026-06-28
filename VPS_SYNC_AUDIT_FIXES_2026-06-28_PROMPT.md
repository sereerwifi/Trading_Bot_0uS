# VPS Sync Prompt — audit fixes 2026-06-28 (B1-B5, I3, C1-C6)

Paste this into Claude Code running **on the VPS inside `RoBotTrading man 0 USV9`**
(or whatever folder the live bot is currently running from). Read `CLAUDE.md` first.
These are the fixes from `AUDIT_REPORT_2026-06-28.md` — already applied to the local
V10 copy; this prompt brings the VPS copy in sync.

Do NOT restart the live bot without the user's explicit go-ahead. Apply all edits,
run verification, report back.

---

## What was fixed (in V10) and needs applying here

### Fix 1+3 (B1+B3) — entry pools in `xauusd_mt5_strategy.py`

Around lines 304–317, replace `DAY_TRADE_STRATEGIES` and `SCALP_STRATEGIES`:

**Old DAY_TRADE_STRATEGIES:**
```python
DAY_TRADE_STRATEGIES = [
    "bb_breakout", "macd_cross", "opening_range_breakout", "price_action",
    "vwap_rejection", "rsi_divergence", "atr_donchian_breakout", "fair_value_gap",
    "fibonacci", "multi_tf_align", "news_fade", "scalp_ema_pullback",
    "ema_cross", "liquidity_sweep", "bos_choch", "order_block",
]
```

**New DAY_TRADE_STRATEGIES** (adds climax_reversal_sr, zone_mw_reversal, mtr_trend_regime,
mtr_range_regime; removes scalp_ema_pullback which belongs in SCALP only):
```python
DAY_TRADE_STRATEGIES = [
    "bb_breakout", "macd_cross", "opening_range_breakout", "price_action",
    "vwap_rejection", "rsi_divergence", "atr_donchian_breakout", "fair_value_gap",
    "fibonacci", "multi_tf_align", "news_fade",
    "ema_cross", "liquidity_sweep", "bos_choch", "order_block",
    "climax_reversal_sr", "zone_mw_reversal", "mtr_trend_regime", "mtr_range_regime",
]
```

**Old SCALP_STRATEGIES:**
```python
SCALP_STRATEGIES = [
    "scalp_ema_pullback", "london_breakout", "scalp_combo_sweep",
    "scalp_ny_orb", "scalp_london_sweep",
]
```

**New SCALP_STRATEGIES** (adds smart_money_sweep_morning/night):
```python
SCALP_STRATEGIES = [
    "scalp_ema_pullback", "london_breakout", "scalp_combo_sweep",
    "scalp_ny_orb", "scalp_london_sweep",
    "smart_money_sweep_morning", "smart_money_sweep_night",
]
```

### Fix 2 (B2) — missing weights in `_RECOMMENDED_STRATEGY_WEIGHTS`

In `_RECOMMENDED_STRATEGY_WEIGHTS` (~line 354), add after the `smart_money_sweep_night` entry:
```python
    "mtr_range_regime": 0.9,  # 27th -- MTR quantitative range-regime detector
    "mtr_trend_regime": 0.8,  # 28th -- MTR quantitative trend-regime detector
```

### Fix 4 (B4) — misleading log line and spurious fib_confluence warning

1. Find the three-line block (around lines 673–676):
```python
    if "fib_confluence" not in ENABLED_STRATEGIES:
        logger.warning("Note: fib_confluence disabled in config — check_entry_signal() "
              "only implements that logic today, so no entries will fire until "
              "the other selected strategies are coded in.")
```
Delete those 4 lines entirely.

2. Find (around line 752):
```python
    logger.info(f"Loaded config from {path}. Enabled strategies: {sorted(ENABLED_STRATEGIES)}")
```
Change `ENABLED_STRATEGIES` to `CONFLUENCE_ENABLED_STRATEGIES`.

### Fix 6 / I3 — `run_logic_groups_scan` ignores per-strategy enabled flag

In `run_logic_groups_scan()`, find:
```python
    result = strategies.score_all(
        data,
        enabled_keys=set(strategies.STRATEGY_REGISTRY.keys()),
        weights=adjusted_weights,
        bench_check=bench_check,
    )
```
Change `enabled_keys=set(strategies.STRATEGY_REGISTRY.keys())` to
`enabled_keys=CONFLUENCE_ENABLED_STRATEGIES`.

**Note for the user**: this is a live behavior change — strategies that are toggled OFF
in the UI will now actually be excluded from scoring in `logic_groups` mode (previously
they were scored anyway). This matches how `confluence13` mode already works.

### Fix 5 (B5) — backfill `strategy_config.json`

Run this one-liner to add the 5 missing strategies without hand-editing the JSON and
without printing secrets:

```python
import sys, json
sys.path.insert(0, ".")
from strategy_config_ui import _deep_merge, DEFAULT_CONFIG
path = "strategy_config.json"
with open(path, encoding="utf-8") as f:
    existing = json.load(f)
merged = _deep_merge(DEFAULT_CONFIG, existing)
with open(path, "w", encoding="utf-8") as f:
    json.dump(merged, f, indent=2, ensure_ascii=False)
n = len(merged["confluence"]["strategies"])
print(f"confluence.strategies: {n} entries")
print("Keys:", sorted(merged["confluence"]["strategies"].keys()))
```

Expect: 31 entries, all 31 registry keys present.

### Cosmetic fixes (C1–C6) — no behavior change

In `xauusd_mt5_strategy.py`, update these strings:
- `# --- Multi-Strategy (20) Confluence Engine` → `(31)`
- `run_confluence_scan(): all 13` → `all 31`
- `# nothing is disabled, only down-weighted, so all 24 strategies still vote.` → `all 31`
- `# Which of the (now 24) confluence strategies` → `(now 31)`
- `"""Multi-strategy (24) parallel scan:` → `(31)`

In `strategy_config_ui.py`:
- `still see all 24 scores` → `all 31 scores`
- `NOT price-derived like the other 19 — 20 total.` → `31 total.`

In `strategies.py` module docstring (line 2):
- `Multi-Strategy (24) Confluence Scoring Engine` → `(31)`

---

## Verification after applying

```python
# Run from inside the VPS bot folder:
import sys, json
sys.path.insert(0, ".")
import strategies

DAY_TRADE_STRATEGIES = [
    "bb_breakout", "macd_cross", "opening_range_breakout", "price_action",
    "vwap_rejection", "rsi_divergence", "atr_donchian_breakout", "fair_value_gap",
    "fibonacci", "multi_tf_align", "news_fade",
    "ema_cross", "liquidity_sweep", "bos_choch", "order_block",
    "climax_reversal_sr", "zone_mw_reversal", "mtr_trend_regime", "mtr_range_regime",
]
SCALP_STRATEGIES = [
    "scalp_ema_pullback", "london_breakout", "scalp_combo_sweep",
    "scalp_ny_orb", "scalp_london_sweep",
    "smart_money_sweep_morning", "smart_money_sweep_night",
]
BIAS_ONLY = {"macro_bias", "order_flow_dom", "supply_demand", "sr_breakout_retest"}
SENTIMENT_ONLY = {"myfxbook_sentiment"}
covered = set(DAY_TRADE_STRATEGIES) | set(SCALP_STRATEGIES) | BIAS_ONLY | SENTIMENT_ONLY
orphaned = set(strategies.STRATEGY_REGISTRY.keys()) - covered
dup = set(DAY_TRADE_STRATEGIES) & set(SCALP_STRATEGIES)
cfg = json.load(open("strategy_config.json", encoding="utf-8"))
n_cfg = len(cfg["confluence"]["strategies"])
print("Orphaned:", orphaned or "none")
print("In both pools:", dup or "none")
print(f"config entries: {n_cfg}")
```

Expected: orphaned=none, in-both-pools=none, config entries=31.

Also run `python -m py_compile *.py` — expect clean compile.

---

## Do NOT change

- `macro_bias` weight (`_RECOMMENDED_STRATEGY_WEIGHTS` says 1.2, DEFAULT_CONFIG says 0.6) —
  awaiting user decision (see I1 in audit report).
- Session-time windows for `london_breakout`, `opening_range_breakout`, `scalp_london_sweep`
  (UTC+3 docs vs UTC+7 clock) — pre-existing, intentionally unfixed per CLAUDE.md.
- Do NOT restart the live bot — that is the user's call.
