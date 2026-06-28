# Prompt for Claude Code (run ON THE VPS) — sync the new 26th strategy

Paste this into Claude Code on the VPS, in the live bot folder (whichever
one is currently authoritative there — the old `RoBotTrading man 0 USV9`
folder, or the new `SR Trading Bot 2026V1` folder if you already ran the
consolidation move). This adds the 26th confluence strategy, **"Climax
Reversal at S/R"**, which was just built and verified on the local copy.
It is purely additive — no existing strategy, weight, or behavior changes.

## What this strategy does

Catches a sharp reversal right after an extreme, exhausted directional
move slams into a support/resistance zone: a strong multi-bar push in one
direction, arriving at a fresh price extreme or a known swing level, then
a sharp rejection candle (pin bar / engulfing) snapping price back the
other way. It only votes once BOTH gates are true on the latest closed H1
bar:

1. **Exhaustion** — net move over the last 8 H1 bars is at least 2.5x
   ATR(14) in one direction.
2. **At a level** — the bar's low/high is either a fresh 80-bar extreme
   (a brand-new high/low) OR within 0.4x ATR of an existing swing
   high/low.

Then it looks for a rejection candle (pin bar or engulfing) on that same
bar and votes the instant it closes — no separate breakout/pending-order
logic, matching the user's explicit choice for immediate entry on the
reversal bar's close. Needs only H1 OHLC + `atr14`, which every scan
already has — no new data source, no new EA wiring beyond a registry
entry + weight/label defaults.

## Step 1 — `strategies.py`

Add this new function. Paste it immediately before the
`# ----------------------------- registry + aggregation ---------------------------`
comment (i.e. right after `score_myfxbook_sentiment`, before
`STRATEGY_REGISTRY = {`):

```python
# ----------------------------- 26. Climax Reversal at S/R -----------------------
def score_climax_reversal_sr(data, move_lookback=8, atr_mult_extreme=2.5,
                              sr_lookback=80, proximity_atr=0.4):
    """26th strategy — catches a sharp reversal right after an extreme,
    exhausted directional move slams into a support/resistance zone: a
    strong multi-bar push in one direction, arriving at a fresh price
    extreme or a prior swing level, then a sharp rejection candle (pin
    bar / engulfing) snapping price back the other way. This is the
    pattern the user pointed at on the H1 GOLD chart: a hard multi-bar
    sell-off into a fresh low, then an immediate strong bounce.

    Two gates must BOTH be true before this strategy votes at all —
    this is what separates it from score_price_action (no exhaustion
    check) and score_sr_breakout_retest (no candle-shape check):

      1. EXHAUSTION: the net move over the last `move_lookback` H1 bars
         must be at least `atr_mult_extreme` x ATR(14) in one direction.
         This is the "extreme/strong move" half of the pattern, not just
         normal chop.
      2. AT A LEVEL: the latest bar's low/high is either a fresh
         `sr_lookback`-bar extreme (a brand-new high/low — a classic
         climax point with no prior level needed yet) OR within
         `proximity_atr` x ATR of an existing swing high/low from
         _swing_points() (a level that already proved important before).

    Only once both gates pass does this function look for a rejection
    candle on the LAST closed H1 bar. The vote fires on that bar's
    close — per the user's explicit choice, there is no separate
    pending/breakout-stop order logic here; this strategy's score IS
    the entry signal as soon as the reversal bar closes ("เข้าทันทีตอน
    แท่งกลับตัวปิด"), same as every other confluence strategy here."""
    df = data["h1"].tail(max(sr_lookback, move_lookback) + 10).reset_index(drop=True)
    if len(df) < move_lookback + 15:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    last, prev = df.iloc[-1], df.iloc[-2]

    # --- Gate 1: exhaustion -- net move over move_lookback bars vs ATR
    ref_close = df["close"].iloc[-1 - move_lookback]
    net_move = last["close"] - ref_close
    extreme_strength = abs(net_move) / atr_now  # in units of ATR
    if extreme_strength < atr_mult_extreme:
        return {"long": 0.0, "short": 0.0,
                "note": f"no exhausted move yet ({extreme_strength:.1f}x ATR < {atr_mult_extreme:.1f}x required)"}
    move_was_down = net_move < 0  # exhausted DOWN-move -> look for a BULLISH reversal
    move_was_up = net_move > 0    # exhausted UP-move   -> look for a BEARISH reversal

    # --- Gate 2: at a level -- fresh N-bar extreme OR near a known swing point
    recent = df.tail(sr_lookback)
    fresh_low = last["low"] <= recent["low"].min() + 1e-9
    fresh_high = last["high"] >= recent["high"].max() - 1e-9
    highs, lows = _swing_points(df, lookback=sr_lookback, order=3)
    near_swing_low = any(abs(last["low"] - l[1]) <= atr_now * proximity_atr for l in lows[-5:])
    near_swing_high = any(abs(last["high"] - h[1]) <= atr_now * proximity_atr for h in highs[-5:])
    at_support = fresh_low or near_swing_low
    at_resistance = fresh_high or near_swing_high

    rng = max(last["high"] - last["low"], 1e-6)
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    prev_body_low, prev_body_high = min(prev["open"], prev["close"]), max(prev["open"], prev["close"])
    cur_bullish, cur_bearish = last["close"] > last["open"], last["close"] < last["open"]
    prev_bullish, prev_bearish = prev["close"] > prev["open"], prev["close"] < prev["open"]

    long_score = short_score = 0.0
    note = f"exhausted move ({extreme_strength:.1f}x ATR) but no rejection candle yet at the level"

    if move_was_down and at_support:
        is_pin = lower_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = cur_bullish and prev_bearish and last["open"] <= prev_body_low and last["close"] >= prev_body_high
        if is_pin or is_engulf:
            level_tag = "fresh climax low" if fresh_low else "key support level"
            shape = "bullish pin bar" if is_pin else "bullish engulfing"
            quality = (lower_wick / rng) if is_pin else (body / rng)
            long_score = _clip(55 + extreme_strength * 6 + quality * 35)
            note = f"{shape} after {extreme_strength:.1f}x-ATR exhausted sell-off at {level_tag}"

    if move_was_up and at_resistance:
        is_pin = upper_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = cur_bearish and prev_bullish and last["open"] >= prev_body_high and last["close"] <= prev_body_low
        if is_pin or is_engulf:
            level_tag = "fresh climax high" if fresh_high else "key resistance level"
            shape = "bearish pin bar" if is_pin else "bearish engulfing"
            quality = (upper_wick / rng) if is_pin else (body / rng)
            short_score = _clip(55 + extreme_strength * 6 + quality * 35)
            note = f"{shape} after {extreme_strength:.1f}x-ATR exhausted rally at {level_tag}"

    return {"long": long_score, "short": short_score, "note": note}
```

Then add the registry entry — find:

```python
    "myfxbook_sentiment": ("Myfxbook Retail Sentiment", score_myfxbook_sentiment),
}
```

and change it to:

```python
    "myfxbook_sentiment": ("Myfxbook Retail Sentiment", score_myfxbook_sentiment),
    # ---- 26th: user-requested pattern -- a strong/extreme directional move
    # that slams into a fresh extreme or known S/R level and snaps back with
    # a rejection candle. Only needs H1 OHLC + atr14, already present in
    # every scan, so it needs no new data wiring beyond this registry entry.
    "climax_reversal_sr": ("Climax Reversal at S/R ★", score_climax_reversal_sr),
}
```

## Step 2 — `xauusd_mt5_strategy.py`

Find `_RECOMMENDED_STRATEGY_WEIGHTS = {` and its closing `}` (currently
ends with `"scalp_combo_sweep": 1.4, ...`). Add two entries — one new
(`climax_reversal_sr`) and one fixing a pre-existing gap where
`myfxbook_sentiment` was never added to this particular fallback table
(it already works correctly via the UI config, this just makes the EA's
own built-in default consistent with the UI's):

```python
    "scalp_combo_sweep": 1.4,  # the user's "most recommended" 4-layer combo setup
    "myfxbook_sentiment": 0.8,  # must stay below macro_bias's 1.2 (Big Data) per user's rule
    "climax_reversal_sr": 1.0,  # 26th -- extreme/exhausted move + S/R + rejection candle
}
```

(No other change needed here — `STRATEGY_WEIGHTS` and
`CONFLUENCE_ENABLED_STRATEGIES` are both derived automatically from
`strategies.STRATEGY_REGISTRY`, so the new strategy is picked up
automatically once Step 1 is done.)

## Step 3 — `strategy_config_ui.py`

Find the end of `DEFAULT_CONFIG["confluence"]["strategies"]` — it
currently ends with:

```python
            "myfxbook_sentiment": {"enabled": True, "weight": 0.8},
        },
    },
```

Change to:

```python
            "myfxbook_sentiment": {"enabled": True, "weight": 0.8},
            # 26th: user-requested pattern -- extreme/exhausted directional
            # move that slams into a fresh extreme or known S/R level and
            # snaps back with a rejection candle (pin bar/engulfing).
            # Weight 1.0 -- same tier as price_action, a solid dependable
            # classic, not an institutional baseline like macro_bias.
            "climax_reversal_sr": {"enabled": True, "weight": 1.0},
        },
    },
```

Find `STRATEGY13_LABELS = {` and its line:

```python
    "myfxbook_sentiment": "25. Myfxbook Retail Sentiment (Community Outlook)",
}
```

Change to:

```python
    "myfxbook_sentiment": "25. Myfxbook Retail Sentiment (Community Outlook)",
    "climax_reversal_sr": "26. Climax Reversal at Support/Resistance ★",
}
```

## Step 4 — `generate_dashboard.py`

Two small text-only changes (the table itself is generated dynamically
from `strategy_scores.json`, so it will automatically show all 26 rows
once the EA produces them — these are just the header/comment text):

- `    # ---- confluence multi-strategy (25) scores ----` → change `(25)`
  to `(26)`
- `  <h2>Multi-Strategy Confluence (25) — Live Scores</h2>` → change `(25)`
  to `(26)`

## Step 5 — `CLAUDE.md`

Find the `## Strategies (25 total)` section and replace it with:

```markdown
## Strategies (26 total)

24 price/order-flow/macro strategies, a 25th: **Myfxbook Retail
Sentiment** (`score_myfxbook_sentiment` in `strategies.py`, fetched by
`fetch_myfxbook_sentiment()` in `macro_data.py`). Reads `data["macro"]
["myfxbook_sentiment"]` — scores 0/0 gracefully until enabled with valid
Myfxbook credentials in `strategy_config.json` under `"myfxbook"` (or the UI's
"Myfxbook Sentiment" tab). Contrarian by default (`MYFXBOOK_CONTRARIAN`/
`"contrarian"` flag) — fades the crowd rather than following it; flip to
trend-following via the same flag.

And a 26th: **Climax Reversal at S/R** (`score_climax_reversal_sr` in
`strategies.py`, key `climax_reversal_sr`, weight `1.0`). User-requested
pattern: a strong/extreme directional move (net move over the last 8 H1
bars >= 2.5x ATR14) that arrives at a fresh price extreme or a known swing
S/R level, then prints a rejection candle (pin bar or engulfing) — votes
the instant that reversal bar closes, no separate breakout/pending-order
logic. Needs only H1 OHLC + atr14 (already present every scan), so it
required no new data wiring, only a registry entry + UI/weight defaults.
```

## Verification before calling this done

1. Syntax-check all 4 edited files (`python -m py_compile
   strategies.py strategy_config_ui.py xauusd_mt5_strategy.py
   generate_dashboard.py`).
2. Run a quick synthetic logic test directly against `score_climax_reversal_sr`
   (no MT5 needed — it only touches a pandas DataFrame): build a 60-bar quiet
   synthetic H1 series, append an 8-bar hard sell-off (~12 pts/bar), then a
   bullish hammer/engulfing bar closing back up. Confirm it returns a
   `long` score near 100 with a note mentioning "exhausted sell-off at fresh
   climax low" (or similar). Then confirm: (a) the same setup WITHOUT the
   final reversal bar returns 0/0 with a "no rejection candle yet" note, and
   (b) a gentle-drift series with no extreme move returns 0/0 with a "no
   exhausted move yet" note. This was already verified on the local copy —
   just confirm the VPS copy behaves identically after the edit.
3. Confirm `strategy_config.json` on the VPS still loads correctly after a
   restart (the new key should backfill via `_deep_merge` the same way
   `myfxbook` did) — check the UI's "24 กลยุทธ์ (Confluence)" tab shows
   "26. Climax Reversal at Support/Resistance ★" as a new checkbox/weight row.
4. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, any other
   strategy's weight, or any risk/lot parameter as part of this sync — this
   is purely additive, matching the local copy exactly.
5. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running — apply the edits, syntax-check, and report back; let
   the user decide when to restart so the new strategy actually takes effect
   in the live scan.
