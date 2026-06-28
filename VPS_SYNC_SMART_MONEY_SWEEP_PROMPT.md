# Prompt for Claude Code (run ON THE VPS) — sync strategies #30/31

Paste this into Claude Code on the VPS, in the live bot folder (see
`CLAUDE.md` for which copy is currently authoritative there). This adds two
new confluence strategies, **"Smart Money Sweep — Morning"** and
**"Smart Money Sweep — Night"**, just built and verified on the local copy.
Purely additive — no existing strategy, weight, or behavior changes.

## Where this came from

The user asked (in Thai) for a way to detect "เจ้ามือ" (market maker / smart
money) liquidity-sweep signals — buy/sell stop clearing — to drive precise
scalping entries/exits, structured around a "morning" (Thai Asia-session)
and "night" (Thai US-close) scalping session. After a spec was reviewed and
approved, this combines 3 signals into one detector, registered twice with
different session windows:

1. **Stop-hunt sweep + fast reclaim (M1)** — the M1-speed version of
   `score_liquidity_sweep` (which only runs on H1).
2. **DOM imbalance shifting fast** — new `_DOM_IMBALANCE_HISTORY` module
   state tracks the live bid/ask imbalance across the last few scan ticks
   (deque of up to 6 `(timestamp, imbalance)` tuples), so the strategy can
   tell "imbalance just got a lot worse" apart from "this symbol is always a
   bit lopsided" — `score_order_flow_dom` only ever looks at one snapshot.
   This signal never votes alone; it only adds conviction on top of signal 1
   or 3.
3. **Abnormal spike + wick rejection (M1)** — a single M1 candle far bigger
   than typical ATR-relative noise, with a one-sided wick that closes back —
   the fast-timeframe cousin of `score_climax_reversal_sr`, minus its 8-bar
   "exhausted move" lead-in.

## IMPORTANT discovery — timezone mismatch with existing scalp_* strategies

While grounding this in the codebase, found that `build_market_data()` sets
`data["now"] = datetime.now()`, and this machine's clock is documented (in
`xauusd_mt5_strategy.py`'s own module docstring, "Trading-hours filter"
section) as being set to **Thailand local time (UTC+7)**. But
`score_scalp_london_sweep`/`score_scalp_ny_orb`'s docstrings describe their
`session_start`/`session_end` defaults as **broker time (UTC+3)** — and
those defaults are compared directly against this same Thai-time
`data["now"]` with no conversion anywhere in the code. That looks like a
**pre-existing inconsistency**: either those two strategies have been firing
4 hours off from their documented intent the whole time, or the VPS's actual
clock setup differs from what its own docstrings say. **This was flagged,
not fixed** — fixing it would change when 2 existing, presumably-tuned
strategies fire, which wasn't asked for. If "why didn't my scalp strategy
fire when expected" comes up, check this first; don't assume it's already
been corrected just because this prompt mentions it.

To sidestep this ambiguity entirely, `score_smart_money_sweep`'s own
`session_start`/`session_end` are plain **Thai/Bangkok wall-clock hours**,
used as-is against `data["now"]` — no UTC+3 conversion anywhere in this new
function. Morning = 07:00–10:00, Night = 02:00–04:00 (both selected
directly from the user's own stated Bangkok-time ranges, sidestepping the
midnight-wrap problem a literal "22:00–00:00" window would have caused).

## What this strategy does

Needs `data["m1"]` (already wired via `build_market_data()`) and optionally
`data["dom"]` (scores 0/0 gracefully for signal 2 if the broker doesn't
expose Level2 — signals 1 and 3 still work without it).

Gated on the session window first — outside it, returns 0/0 immediately.
Inside it: builds a recent range from M1 bars (excluding the last
`reclaim_bars=3`), checks for a wick that pierced that range and reclaimed
it (signal 1), checks the latest bar for an abnormal spike+wick rejection
(signal 3), and checks whether the live DOM imbalance has shifted by more
than `dom_delta_threshold` across the last few scan ticks (signal 2 — only
ever a bonus on top of 1 or 3, never votes by itself). Score: 1 confirming
signal → ~35, 2 → ~65, all 3 → ~90+, scaled further by how extreme each
signal is. Direction is always opposite the sweep/spike.

## Step 1 — `strategies.py`

Add `from collections import deque` to the imports at the top (next to
`import pandas as pd`), if not already present.

Add this new code block immediately before the
`# ----------------------------- registry + aggregation ---------------------------`
comment (i.e. right after `score_zone_mw_reversal`, before
`STRATEGY_REGISTRY = {`):

```python
# ----------------------------- 30/31. Smart Money Liquidity Sweep --------------
# Module-level: tracks the last few DOM (bid/ask volume) snapshots so we can
# measure HOW FAST the order-book imbalance is shifting, not just whether it's
# lopsided right now. score_order_flow_dom only ever looks at one snapshot in
# isolation, which can't tell "a sudden imbalance just appeared" apart from
# "this symbol is always a bit bid-heavy" -- this fixes that gap. Shared by
# both smart_money_sweep registry entries (morning/night), since they read the
# same single live DOM feed each scan; the dedup guard below stops a single
# scan tick from being counted twice just because two registry entries call
# into this module in the same tick.
_DOM_IMBALANCE_HISTORY = deque(maxlen=6)


def _update_dom_imbalance_history(data):
    """Appends (timestamp, imbalance) once per scan tick, only if DOM data is
    actually present. No-ops silently if the broker/symbol doesn't expose
    Level2 -- the caller treats a too-short history as "no DOM signal" rather
    than an error."""
    dom = data.get("dom")
    now = data.get("now")
    if not dom or now is None:
        return
    bid_vol = dom.get("bid_volume", 0.0)
    ask_vol = dom.get("ask_volume", 0.0)
    total = bid_vol + ask_vol
    if total <= 0:
        return
    imbalance = (bid_vol - ask_vol) / total
    ts = now_ts(now)
    if _DOM_IMBALANCE_HISTORY and _DOM_IMBALANCE_HISTORY[-1][0] == ts:
        return  # same scan tick already recorded (e.g. by the twin morning/night entry)
    _DOM_IMBALANCE_HISTORY.append((ts, imbalance))


def _dom_imbalance_delta():
    """Returns (delta, latest_imbalance) across the tracked window -- how much
    the bid/ask imbalance has shifted from the oldest to the newest snapshot
    currently held. (0.0, 0.0) if there isn't enough history yet (e.g. right
    after the bot starts, or the broker doesn't expose DOM at all)."""
    if len(_DOM_IMBALANCE_HISTORY) < 3:
        return 0.0, 0.0
    oldest_imb = _DOM_IMBALANCE_HISTORY[0][1]
    latest_imb = _DOM_IMBALANCE_HISTORY[-1][1]
    return latest_imb - oldest_imb, latest_imb


def score_smart_money_sweep(data, session_start=(7, 0), session_end=(10, 0),
                             range_lookback_bars=120, sweep_atr_mult=0.3,
                             reclaim_bars=3, dom_delta_threshold=0.25,
                             spike_atr_mult=2.5, wick_ratio=0.6,
                             session_label="session"):
    """30th/31st strategy -- user-requested "smart money / market maker
    liquidity sweep" detector for super-scalping, combining 3 independent
    signals that each point at the same underlying event (a deliberate
    clearing of resting buy/sell stops just before a fast directional move),
    scored higher the more of them fire together rather than any one alone:

      1. STOP-HUNT SWEEP + FAST RECLAIM (M1): a wick pierces the high/low of
         the recently-built range by >= sweep_atr_mult x ATR(M1), then price
         closes back inside that range within `reclaim_bars` candles. This is
         the M1-speed version of score_liquidity_sweep (which only runs on H1
         -- far too slow to use for scalping entries).
      2. DOM IMBALANCE SHIFTING FAST: the live bid/ask volume imbalance (see
         score_order_flow_dom) has moved by at least `dom_delta_threshold`
         across the last few scan ticks -- i.e. not just lopsided, but
         actively getting more lopsided right now. Needs data["dom"]; if the
         broker/symbol doesn't expose Level2, this signal simply never fires
         (the other two still can). By itself this signal NEVER votes --
         DOM-only with no price-action confirmation is too noisy -- it only
         adds conviction on top of signal 1 or 3.
      3. ABNORMAL SPIKE + WICK REJECTION (M1): the latest M1 bar's range is
         >= spike_atr_mult x ATR(M1) (a single candle far bigger than normal
         M1 noise) with a one-sided wick covering >= wick_ratio of that bar,
         closing back the other way. The fast-timeframe cousin of
         score_climax_reversal_sr's rejection-candle check, minus its 8-bar
         "exhausted move" lead-in -- scalping needs to react to the spike
         candle itself, the instant it closes.

    Scoring: 1 confirming signal -> ~35, 2 -> ~65, all 3 -> ~90+, scaled up
    further by how extreme the pierce/spike/DOM-shift is. Direction is always
    OPPOSITE the sweep/spike (stops cleared above -> short bias, stops
    cleared below -> long bias), matching score_liquidity_sweep's convention.

    SESSION GATING -- IMPORTANT TIMEZONE NOTE: unlike the existing scalp_*
    strategies (whose docstrings describe their session defaults in broker
    time, UTC+3), this VPS's build_market_data() sets data["now"] = Python's
    datetime.now(), and this machine's clock is documented elsewhere in this
    file (xauusd_mt5_strategy.py's module docstring, "Trading-hours filter"
    section) as being set to Thailand local time (UTC+7) -- not broker time.
    So `session_start`/`session_end` here are plain Thai/Bangkok wall-clock
    hours, used as-is with no UTC+3 conversion. Defaults: morning session
    07:00-10:00 (Bangkok Asia-session liquidity, before London desks are
    active), night session 22:00-00:00 is NOT used here because it crosses
    midnight awkwardly for this simple same-day compare -- the night entry
    below instead uses 02:00-04:00 directly (US-close liquidity), matching
    what the user actually asked for. Register this function twice in
    STRATEGY_REGISTRY (see below) with different windows/labels -- do NOT
    reuse the scalp_*'s "broker time" defaults here, they are not the same
    clock as data["now"] on this VPS. This mismatch between the scalp_*
    docstrings and the actual data["now"] clock looks like a pre-existing
    inconsistency in this codebase -- flagged here, not silently fixed
    elsewhere, since changing the scalp_* strategies' behavior wasn't asked
    for and could shift when they fire."""
    df = data.get("m1")
    now = data.get("now")
    if df is None or len(df) < 30 or now is None:
        return {"long": 0.0, "short": 0.0,
                "note": "M1 data not available — add 'm1' to build_market_data()"}

    _update_dom_imbalance_history(data)

    sess_start = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{session_start[0]:02d}:{session_start[1]:02d}").time())
    sess_end = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{session_end[0]:02d}:{session_end[1]:02d}").time())
    if not (sess_start <= now_ts(now) <= sess_end):
        return {"long": 0.0, "short": 0.0,
                "note": f"outside {session_label} smart-money sweep window"}

    window = df.tail(max(range_lookback_bars, reclaim_bars + 10)).reset_index(drop=True)
    if len(window) < reclaim_bars + 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient M1 data for this session window"}

    atr_now = window["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (window["high"] - window["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    pre_window = window.iloc[:-reclaim_bars]
    range_high, range_low = pre_window["high"].max(), pre_window["low"].min()
    recent_bars = window.tail(reclaim_bars)
    last = recent_bars.iloc[-1]

    # --- Signal 1: stop-hunt sweep + fast reclaim ---
    swept_high = recent_bars["high"].max() > range_high
    pierce_high = (recent_bars["high"].max() - range_high) / atr_now if swept_high else 0.0
    sweep_high_signal = swept_high and last["close"] < range_high and pierce_high >= sweep_atr_mult

    swept_low = recent_bars["low"].min() < range_low
    pierce_low = (range_low - recent_bars["low"].min()) / atr_now if swept_low else 0.0
    sweep_low_signal = swept_low and last["close"] > range_low and pierce_low >= sweep_atr_mult

    # --- Signal 3: abnormal spike + wick rejection on the latest bar ---
    bar_rng = max(last["high"] - last["low"], 1e-6)
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    is_spike = bar_rng >= atr_now * spike_atr_mult
    spike_bear_reject = is_spike and (upper_wick / bar_rng) >= wick_ratio and last["close"] < last["open"]
    spike_bull_reject = is_spike and (lower_wick / bar_rng) >= wick_ratio and last["close"] > last["open"]

    # --- Signal 2: DOM imbalance shifting fast (only ever a bonus, never votes alone) ---
    dom_delta, _dom_latest = _dom_imbalance_delta()
    dom_bear_signal = dom_delta <= -dom_delta_threshold
    dom_bull_signal = dom_delta >= dom_delta_threshold

    def _combo_score(n_signals, quality):
        base = {1: 35.0, 2: 65.0, 3: 90.0}.get(n_signals, 0.0)
        return _clip(base + quality)

    short_price_signal = sweep_high_signal or spike_bear_reject
    long_price_signal = sweep_low_signal or spike_bull_reject

    short_score = long_score = 0.0
    note = f"no smart-money sweep signal in {session_label} window"

    if short_price_signal:
        dom_confirms_short = dom_bear_signal
        n = int(sweep_high_signal) + int(spike_bear_reject) + int(dom_confirms_short)
        quality = 0.0
        parts = []
        if sweep_high_signal:
            quality += min(pierce_high / sweep_atr_mult, 2.0) * 5
            parts.append(f"sweep above {range_high:.2f} ({pierce_high:.2f}xATR) reclaimed")
        if spike_bear_reject:
            quality += min(bar_rng / (atr_now * spike_atr_mult), 2.0) * 5
            parts.append("abnormal up-wick spike rejected")
        if dom_confirms_short:
            quality += min(abs(dom_delta) / dom_delta_threshold, 2.0) * 5
            parts.append(f"DOM imbalance swinging ask-heavy ({dom_delta * 100:+.0f}%)")
        short_score = _combo_score(n, quality)
        note = f"SHORT smart-money sweep ({session_label}): " + " + ".join(parts)

    if long_price_signal:
        dom_confirms_long = dom_bull_signal
        n = int(sweep_low_signal) + int(spike_bull_reject) + int(dom_confirms_long)
        quality = 0.0
        parts = []
        if sweep_low_signal:
            quality += min(pierce_low / sweep_atr_mult, 2.0) * 5
            parts.append(f"sweep below {range_low:.2f} ({pierce_low:.2f}xATR) reclaimed")
        if spike_bull_reject:
            quality += min(bar_rng / (atr_now * spike_atr_mult), 2.0) * 5
            parts.append("abnormal down-wick spike rejected")
        if dom_confirms_long:
            quality += min(abs(dom_delta) / dom_delta_threshold, 2.0) * 5
            parts.append(f"DOM imbalance swinging bid-heavy ({dom_delta * 100:+.0f}%)")
        s = _combo_score(n, quality)
        if s > long_score:
            long_score = s
            note = f"LONG smart-money sweep ({session_label}): " + " + ".join(parts)

    return {"long": long_score, "short": short_score, "note": note}
```

Then add the registry entries — find the end of `STRATEGY_REGISTRY` and add,
immediately before the closing `}`:

```python
    # ---- 30th/31st: user-requested "smart money / market maker liquidity
    # sweep" detector for super-scalping -- combines an M1 stop-hunt sweep +
    # fast reclaim, a sudden DOM bid/ask imbalance shift, and an abnormal
    # spike+wick rejection candle. Registered twice with different session
    # windows in Thai/Bangkok local time (this VPS's data["now"] clock --
    # see score_smart_money_sweep's docstring for why these are NOT broker
    # UTC+3 times like the scalp_* strategies' defaults). Needs "m1" + "dom"
    # in the data dict (both already present via build_market_data()).
    "smart_money_sweep_morning": (
        "Smart Money Sweep — Morning (Asia 07-10) ★",
        lambda data: score_smart_money_sweep(
            data, session_start=(7, 0), session_end=(10, 0),
            session_label="morning/Asia"),
    ),
    "smart_money_sweep_night": (
        "Smart Money Sweep — Night (US-close 02-04) ★",
        lambda data: score_smart_money_sweep(
            data, session_start=(2, 0), session_end=(4, 0),
            session_label="night/US-close"),
    ),
}
```

**Important:** confirm what the VPS's `STRATEGY_REGISTRY` actually ends with
first — don't assume it matches the local copy's exact ordering, just add
these 2 entries right before the final `}`.

## Step 2 — `xauusd_mt5_strategy.py`

Find `_RECOMMENDED_STRATEGY_WEIGHTS = {` and its closing `}`. Add immediately
before the closing `}`:

```python
    "smart_money_sweep_morning": 1.0,  # 30th -- M1 sweep+reclaim / DOM delta / spike-wick, Asia 07-10 BKK
    "smart_money_sweep_night": 1.0,  # 31st -- same logic, US-close window 02-04 BKK
}
```

## Step 3 — `strategy_config_ui.py`

Find the end of `DEFAULT_CONFIG["confluence"]["strategies"]` and add,
immediately before the closing `},`:

```python
            # 30th/31st: user-requested smart-money liquidity sweep detector
            # for super-scalping (M1 stop-hunt sweep+reclaim, DOM imbalance
            # delta, abnormal spike+wick rejection). Registered twice with
            # different session windows (Thai/Bangkok local time).
            "smart_money_sweep_morning": {"enabled": True, "weight": 1.0},
            "smart_money_sweep_night": {"enabled": True, "weight": 1.0},
```

Find `STRATEGY13_LABELS = {` and add, immediately before its closing `}`:

```python
    "smart_money_sweep_morning": "30. Smart Money Sweep — Morning (Asia 07-10) ★ 🩳",
    "smart_money_sweep_night":   "31. Smart Money Sweep — Night (US-close 02-04) ★ 🩳",
```

## Step 4 — `generate_dashboard.py`

Two text-only changes — find whatever number is currently in these two
lines and bump it by 2 (confirm the actual current count first, it may not
be 29 if anything else was added on the VPS since the last sync):

- `    # ---- confluence multi-strategy (N) scores ----`
- `  <h2>Multi-Strategy Confluence (N) — Live Scores</h2>`

## Step 5 — `CLAUDE.md`

Update `## Strategies (N total)` to the new total and add a short paragraph
for strategies #30/31, including the timezone-mismatch flag described above
(see this prompt's "IMPORTANT discovery" section for the wording used on the
local copy's `CLAUDE.md`).

## Verification before calling this done

1. Syntax-check all 4 edited files: `python -m py_compile strategies.py
   strategy_config_ui.py xauusd_mt5_strategy.py generate_dashboard.py`.
2. Run a synthetic logic test directly against `score_smart_money_sweep`
   (no MT5 needed — pandas DataFrames + plain dicts for `dom`/`now` only).
   Confirm all of: (a) outside the session window → 0/0 with an "outside ...
   window" note; (b) inside the window, flat market, no DOM history → 0/0
   with a "no smart-money sweep signal" note; (c) a small wick that pierces
   the established M1 range by >= 0.3xATR and reclaims it within 3 bars,
   with no abnormal spike → long or short ~35-45 with a "sweep ... reclaimed"
   note only; (d) a single M1 bar with range >= 2.5xATR and a one-sided wick
   >= 60% of the bar that closes back, with no range breach → ~35-45 with an
   "abnormal ... spike rejected" note only; (e) feeding 3 DOM snapshots at
   distinct timestamps via `_update_dom_imbalance_history` that shift hard
   toward one side, with NO accompanying price signal → must still return
   0/0 (DOM never votes alone — if it doesn't, something is wrong); (f) all
   3 signals together (sweep + spike + DOM shift) → score should clip at or
   near 100 with a note listing all 3 parts. This was already verified on
   the local copy (all 6 cases passed) — just confirm the VPS copy behaves
   identically.
3. Confirm `strategy_config.json` on the VPS still loads correctly after a
   restart (the 2 new keys should backfill via `_deep_merge`) — check the
   UI's confluence-strategies tab shows both new rows.
4. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, any other
   strategy's weight, or any risk/lot parameter as part of this sync — this
   is purely additive, matching the local copy exactly.
5. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running — apply the edits, syntax-check, and report back; let
   the user decide when to restart so the new strategies actually take
   effect in the live scan.
6. Flag (don't fix) the timezone discovery above if it hasn't already been
   raised with the user — it affects 2 *existing* strategies
   (`scalp_london_sweep`, `scalp_ny_orb`), not the new ones, so resolving it
   is a separate decision for the user to make, not part of this sync.
