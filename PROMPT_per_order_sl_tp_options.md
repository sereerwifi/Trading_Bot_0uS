# Prompt for Claude Code — optional SL/TP toggle + fixed-USD TP per order

Run this in the live bot folder on the VPS (confirm with `git log -1`
that you're on the actual checked-out copy before starting — a Mac-side
mirror of this folder can be several commits behind). User-requested
feature, not a bug fix: add a UI choice for whether to use SL and/or TP
when opening an order, plus a way to set a fixed USD take-profit target
per order instead of the existing ATR/R:R-based TP.

## Ground rule — verify before applying, same as previous prompts

1. Read the actual current code first — line numbers below are from the
   2026-06-26 read of `xauusd_mt5_strategy.py` / `strategy_config_ui.py`
   and may have shifted.
2. This touches order-placement parameters (SL/TP) — treat it with the
   same care as any risk-parameter change: additive, config-flag-driven,
   default behavior UNCHANGED until the user explicitly flips a toggle in
   the UI and saves.
3. `python -m py_compile` every file touched.
4. Run the synthetic tests described below before calling it done.
5. Do NOT restart the live bot. Apply, verify, report back.
6. Never print/log/paste Telegram or Myfxbook credentials.
7. **Flag prominently in your report (don't just silently implement)**:
   disabling SL on a live MT5 order is a real-money, uncapped-risk
   change — broker margin call / full account drawdown is possible on a
   single trade with no SL. Build it because it was requested, but the UI
   must make this danger impossible to miss (see Step 3).

## Current behavior (confirmed by reading the code)

`send_order()` (~line 2452) always sends both `sl` and `tp` on every
order:

```python
request = {
    ...
    "sl": signal["sl"],
    "tp": signal["tp2"],
    ...
}
```

`signal["sl"]` and `signal["tp2"]` are computed earlier per-strategy
(e.g. ATR-based SL, R:R-multiple TP — see `passes_risk_reward()` ~line
1261, `CONFLUENCE_SL_ATR_MULT` / `CONFLUENCE_TP_RR` ~line 652-653). There
is currently no config path to omit SL/TP or to override TP with a flat
USD target.

## Step 1 — new config section `order_options`

Add to `strategy_config.json` (and `strategy_config_ui.py`'s
`DEFAULT_CONFIG`):

```json
"order_options": {
    "use_sl": true,
    "use_tp": true,
    "tp_mode": "strategy",
    "tp_fixed_usd": 50.0
}
```

- `use_sl` (bool, default `true`): if `false`, the order is sent with
  `sl=0.0` (no stop loss attached at the broker level).
- `use_tp` (bool, default `true`): if `false`, the order is sent with
  `tp=0.0` (no take profit attached).
- `tp_mode` (`"strategy"` | `"fixed_usd"`, default `"strategy"`):
  `"strategy"` = unchanged existing behavior (`signal["tp2"]`,
  ATR/R:R-derived). `"fixed_usd"` = use `tp_fixed_usd` instead (only
  relevant if `use_tp` is `true`).
- `tp_fixed_usd` (float, default `50.0`): target profit in account
  currency for ONE order at its actual lot size, only used when
  `tp_mode == "fixed_usd"`.

Defaults must reproduce CURRENT behavior exactly
(`use_sl=true, use_tp=true, tp_mode="strategy"`) so nothing changes for
the user until they explicitly open the UI and change something.

## Step 2 — compute the actual sl/tp to send

Add a small helper near `calc_lot_size()` (~line 1276), same style:

```python
def calc_tp_price_from_usd(entry, direction, lot, target_usd,
                            value_per_point=VALUE_PER_POINT_PER_LOT):
    """Price level that yields `target_usd` profit for `lot` lots,
    given the account's $-per-point-per-lot conversion. Returns None if
    lot or value_per_point is 0 (caller should fall back / skip)."""
    if lot <= 0 or value_per_point <= 0:
        return None
    distance = target_usd / (lot * value_per_point)
    return entry + distance if direction == "long" else entry - distance
```

Then, at each of the three places that build the order-bound `signal`
dict right before calling `send_order()` — confluence13's path, Day
Trade's path, and Scalping Trade's path inside `run_logic_groups_scan()`
(search for the existing `lot = calc_lot_size(...)` calls, ~lines 1997,
2271, 2684 — `send_order()` is called shortly after each) — apply
`order_options` right before the `send_order(signal, lot)` call, NOT
inside `send_order()` itself (keep `send_order()` generic/dumb, it should
just send whatever `signal["sl"]`/`signal["tp2"]` already say — this
matches the existing pattern where signal-shaping happens before
`send_order`, not inside it):

```python
sl_to_use = signal["sl"] if ORDER_OPTIONS["use_sl"] else 0.0
if ORDER_OPTIONS["use_tp"]:
    if ORDER_OPTIONS["tp_mode"] == "fixed_usd":
        fixed_tp = calc_tp_price_from_usd(
            signal["entry"], signal["direction"], lot,
            ORDER_OPTIONS["tp_fixed_usd"])
        tp_to_use = fixed_tp if fixed_tp is not None else signal["tp2"]
    else:
        tp_to_use = signal["tp2"]
else:
    tp_to_use = 0.0
signal["sl"] = sl_to_use
signal["tp2"] = tp_to_use
```

Read the actual current variable names/flow at each of the three call
sites before copy-pasting this — they may differ slightly between
confluence13 and the two logic-groups branches.

**Important interaction to check**: `passes_risk_reward()` (~line 1261)
runs BEFORE this — if `use_sl=False`, there's no real SL to compute a
meaningful R:R against. Decide and document: either (a) skip the R:R
gate entirely when `use_sl=False` (since "risk" is undefined without an
SL), or (b) still compute R:R against the strategy's underlying
`signal["sl"]` for gating purposes even though the actual order won't
carry that SL. Pick (b) — it's safer (keeps the existing setup-quality
filter intact) and only changes what gets SENT to the broker, not which
setups are accepted. State which you implemented in your report.

Also check `calc_lot_size()` (~line 1276) and `check_trade_interval` /
basket-close / breakeven logic (~line 2441, `modify_sl()`) — breakeven
and trailing-stop logic both call `modify_sl()` which assumes a real SL
exists on the position. If `use_sl=False`, breakeven/trailing-stop should
be skipped for that position (there's nothing to move) — add a guard so
`manage_trailing_stop()` / breakeven check skips positions whose SL is
`0.0`, rather than erroring or trying to set an SL where the user
explicitly chose none.

## Step 3 — UI controls (`strategy_config_ui.py`)

In `build_risk_tab()` (~line 1248), add a new `LabelFrame` after the
existing "Risk Management" frame and before "Money Management", e.g.:

```python
oframe = ttk.LabelFrame(parent, text="Order Options — SL / TP ต่อออเดอร์")
oframe.pack(fill="x", padx=12, pady=12)
o = "order_options"
self.reg_bool(oframe, o, "use_sl",
              "ใช้ Stop Loss (SL) ในการเปิดออเดอร์ — แนะนำให้เปิดไว้เสมอ", row=0)
ttk.Label(
    oframe,
    text="⚠ คำเตือน: ถ้าปิด SL ออเดอร์จะไม่มีจุดตัดขาดทุนที่โบรกเกอร์เลย "
         "ความเสี่ยงไม่จำกัดต่อออเดอร์ — ใช้ความระมัดระวังสูงสุด",
    foreground="#cc0000", wraplength=560, justify="left",
).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 6))
self.reg_bool(oframe, o, "use_tp",
              "ใช้ Take Profit (TP) ในการเปิดออเดอร์", row=2)
self.reg_combo(oframe, o, "tp_mode", "วิธีกำหนด TP:",
               ["strategy", "fixed_usd"], row=3)
self.reg_entry(oframe, o, "tp_fixed_usd",
               "TP แบบกำหนดเอง (USD ต่อออเดอร์ ใช้เมื่อเลือก fixed_usd):",
               row=4, numeric_type=float)
```

Use the existing `reg_bool` / `reg_combo` / `reg_entry` helper methods
already used elsewhere in this file (confirm their exact signatures by
reading their definitions before using them — don't guess parameter
order). Match the existing Thai-language labeling convention used
throughout this tab (see the Auto Trade warning text right above it,
~line 1258-1264, as a style reference) — keep the SL warning text in red
(`foreground="#cc0000"` or similar, matching the existing orange warning
style's pattern but visually distinct/more severe since this risk is
larger than the existing Auto Trade warning).

## Verification before calling this done

1. `python -m py_compile xauusd_mt5_strategy.py strategy_config_ui.py`.
2. Unit-test `calc_tp_price_from_usd()` directly: known lot/value-per-point
   combo → assert the returned price matches a hand-calculated expected
   distance, for both `"long"` and `"short"` directions. Also test
   `lot=0` and `value_per_point=0` return `None`.
3. Synthetic test of the sl/tp override logic (no MT5 needed — call the
   sl/tp-resolution code directly with a fake `signal` dict and fake
   `ORDER_OPTIONS` values): confirm all 2x2x2 combinations of
   `use_sl`/`use_tp`/`tp_mode` produce the expected `sl`/`tp` values,
   especially confirming the DEFAULT combination
   (`use_sl=True, use_tp=True, tp_mode="strategy"`) reproduces today's
   exact values (`signal["sl"]`, `signal["tp2"]` unchanged) — this is the
   backward-compatibility guarantee and must pass.
4. Confirm `strategy_config.json` loads correctly both with and without
   the new `order_options` key present (backward-compat for existing
   config files that predate this feature — should default exactly as
   specified in Step 1, not error or warn).
5. Confirm breakeven/trailing-stop logic correctly skips positions with
   `sl == 0.0` rather than erroring.
6. Do NOT restart the live bot.

## Report back

State which of (a)/(b) you implemented for the R:R-gate interaction in
Step 2, confirm the default-combination backward-compatibility test
passed, and confirm the UI warning text renders for the SL-disable
option. Also note: this is a real-money risk change once a user actually
flips `use_sl` off and saves — that decision is the user's, not yours;
your job here is only to build the option and make its danger visible,
not to recommend whether to use it.
