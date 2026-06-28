# Prompt for Claude Code (run ON THE VPS) — sync the new 29th strategy

Paste this into Claude Code on the VPS, in the live bot folder (whichever
one is currently authoritative there — see `CLAUDE.md`). This adds the
29th confluence strategy, **"HTF Zone + M/W Reversal"**, which was just
built and verified on the local copy. It is purely additive — no existing
strategy, weight, or behavior changes.

## Where this came from

The user uploaded 3 documents from their gold swing-trading course
("GOLD Fundamentals", "Gold Live Trade and Analysis", "Gold Live Trade and
Analysis Profits") and asked for the strongest method in them to be turned
into a new bot strategy. The single most repeated, most concretely
actionable method across all 3 documents: draw key zones top-down
(Weekly → Daily → H4), validated only by multiple price reactions over
time (not arbitrary points); wait for price to return to a zone; drill into
a lower timeframe to find a double-top/double-bottom ("M/W") reversal
pattern, focusing on the pattern's SECOND touch/leg; enter on the candle
that closes through the pattern's neckline. None of the bot's existing 28
strategies implement this — `score_order_block`/`score_supply_demand` are
the closest in spirit but don't combine an HTF multi-touch zone with a
nested lower-timeframe double-top/bottom pattern.

This bot has no D1/W1 OHLC wired in (`data` only has `h4`/`h1`/`m15`/`dom`/
`macro`/`now`), so **H4 stands in for the course's Weekly/Daily key-level
step**, and **M15 stands in for the course's lower-timeframe pattern step**.

## What this strategy does

Two gates must BOTH be true before it votes:

1. **PROVEN ZONE** — at least `min_touches` (default 2) H4 swing highs (or
   lows) within `zone_tol_atr` (default 0.35) x ATR(H4) of each other. Not
   gated on the current close — by the time the M15 neckline breaks, price
   has already moved away from the zone by design; gate 2 ties the pattern
   back to a specific zone instead.
2. **M/W PATTERN AT THE ZONE** — on M15, the two most recent swing highs
   (for a resistance zone) or swing lows (for a support zone) sit within
   `peak_tol_atr` (default 0.30) x ATR(M15) of each other AND near a
   qualifying H4 zone, AND the latest closed M15 bar has just closed
   through the neckline (the low between the two tops / high between the
   two bottoms) by at least `neckline_break_atr` (default 0.15) x ATR(M15).

Needs only H4 + M15 OHLC + `atr14`, both already present in every scan — no
new data source, no new EA wiring beyond a registry entry + weight/label
defaults.

## Step 1 — `strategies.py`

Add this new function. Paste it immediately before the
`# ----------------------------- registry + aggregation ---------------------------`
comment (i.e. right after `score_mtr_trend_regime`, before
`STRATEGY_REGISTRY = {`):

```python
# ----------------------------- 29. HTF Zone + M/W Reversal --------------------
def score_zone_mw_reversal(data, h4_lookback=80, zone_tol_atr=0.35, min_touches=2,
                            m15_lookback=60, peak_tol_atr=0.30, neckline_break_atr=0.15):
    """29th strategy -- multi-touch H4 zone (the highest timeframe this bot
    has wired in -- standing in for the Weekly/Daily "key level" step from
    the user's uploaded gold swing-trading course material) combined with a
    nested M15 double-top/double-bottom ("M/W") reversal pattern confirmed
    by a neckline break. This is the entry method described and worked
    through repeatedly across all three of the user's uploaded documents
    ("GOLD Fundamentals", "Gold Live Trade and Analysis", "...Profits"):
    draw HTF zones where price has reacted multiple times, wait for price to
    return to the zone, then drill down to a lower timeframe and take the
    SECOND touch/leg of a double-top/double-bottom at that zone, entering on
    the candle that breaks the pattern's neckline.

    Two gates must BOTH be true before this strategy votes:

      1. PROVEN ZONE: at least `min_touches` H4 swing highs (or lows) within
         `zone_tol_atr` x ATR(H4) of each other -- not gated on the current
         close, since by the time the M15 neckline breaks price has already
         moved away from the zone by design; gate 2 below is what ties the
         pattern back to a specific zone. This is the "level the market has
         reacted to many times" step from the course, using H4 (the highest
         timeframe already wired into this bot) in place of the course's
         Weekly/Daily charts -- no new data source required.
      2. M/W PATTERN AT THE ZONE: on M15, the two most recent swing highs
         (when the zone is acting as resistance) or swing lows (when it's
         acting as support) sit within `peak_tol_atr` x ATR(M15) of each
         other near that zone, AND the latest closed M15 bar has just
         closed through the neckline (the low between the two tops, or the
         high between the two bottoms) by at least `neckline_break_atr` x
         ATR(M15) -- the "official entry" moment the course repeatedly
         points to as the second leg's confirmation.

    Needs only H4 + M15 OHLC + atr14, both already present in every scan --
    no new data wiring beyond a registry entry + weight/label defaults,
    matching the pattern used for the 26th-28th strategies."""
    h4 = data.get("h4")
    m15 = data.get("m15")
    if h4 is None or m15 is None or len(h4) < 30 or len(m15) < m15_lookback + 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H4/M15 data"}

    atr_h4 = h4["atr14"].iloc[-1]
    atr_h4 = atr_h4 if atr_h4 and not pd.isna(atr_h4) else (h4["high"] - h4["low"]).tail(20).mean()
    atr_h4 = max(atr_h4, 1e-6)
    atr_m15 = m15["atr14"].iloc[-1]
    atr_m15 = atr_m15 if atr_m15 and not pd.isna(atr_m15) else (m15["high"] - m15["low"]).tail(20).mean()
    atr_m15 = max(atr_m15, 1e-6)

    # --- Gate 1: find multi-touch H4 zones (don't gate on the current close --
    # by the time the M15 neckline breaks, price has already moved AWAY from
    # the zone by design; gate 2 below checks the pattern's peak/trough sits
    # at the zone instead).
    highs, lows = _swing_points(h4, lookback=h4_lookback, order=3)

    def _zone_clusters(points):
        clusters, seen = [], []
        for _, lvl in points:
            if any(abs(lvl - s) <= atr_h4 * zone_tol_atr for s in seen):
                continue
            touches = sum(1 for _, p in points if abs(p - lvl) <= atr_h4 * zone_tol_atr)
            if touches >= min_touches:
                clusters.append((lvl, touches))
                seen.append(lvl)
        return clusters

    res_clusters = _zone_clusters(highs)
    sup_clusters = _zone_clusters(lows)

    if not res_clusters and not sup_clusters:
        return {"long": 0.0, "short": 0.0, "note": "no multi-touch H4 zone found"}

    # --- Gate 2: M/W pattern + neckline break on M15 ---
    m15_recent = m15.tail(m15_lookback).reset_index(drop=True)
    m_highs, m_lows = _swing_points(m15_recent, lookback=m15_lookback, order=2)
    last_closed = m15_recent.iloc[-1]

    long_score = short_score = 0.0
    note = "no M/W reversal pattern at a proven H4 zone yet on M15"

    if res_clusters and len(m_highs) >= 2:
        p2_idx, p2 = m_highs[-1]
        p1_idx, p1 = m_highs[-2]
        symmetric = abs(p1 - p2) <= atr_m15 * peak_tol_atr
        zone_hits = [c for c in res_clusters if abs(p2 - c[0]) <= atr_h4 * zone_tol_atr * 1.5]
        if symmetric and zone_hits and p2_idx > p1_idx:
            res_level, res_touches = max(zone_hits, key=lambda c: c[1])
            neckline = m15_recent["low"].iloc[p1_idx:p2_idx + 1].min()
            broke = last_closed["close"] < neckline - atr_m15 * neckline_break_atr
            if broke:
                sym_quality = 1.0 - abs(p1 - p2) / max(atr_m15 * peak_tol_atr, 1e-6)
                break_quality = (neckline - last_closed["close"]) / max(atr_m15, 1e-6)
                short_score = _clip(50 + res_touches * 8 + sym_quality * 20 + break_quality * 20)
                note = (f"double top at {res_touches}-touch H4 resistance "
                        f"[{res_level:.2f}] -- M15 neckline broken")

    if sup_clusters and len(m_lows) >= 2:
        p2_idx, p2 = m_lows[-1]
        p1_idx, p1 = m_lows[-2]
        symmetric = abs(p1 - p2) <= atr_m15 * peak_tol_atr
        zone_hits = [c for c in sup_clusters if abs(p2 - c[0]) <= atr_h4 * zone_tol_atr * 1.5]
        if symmetric and zone_hits and p2_idx > p1_idx:
            sup_level, sup_touches = max(zone_hits, key=lambda c: c[1])
            neckline = m15_recent["high"].iloc[p1_idx:p2_idx + 1].max()
            broke = last_closed["close"] > neckline + atr_m15 * neckline_break_atr
            if broke:
                sym_quality = 1.0 - abs(p1 - p2) / max(atr_m15 * peak_tol_atr, 1e-6)
                break_quality = (last_closed["close"] - neckline) / max(atr_m15, 1e-6)
                long_score = _clip(50 + sup_touches * 8 + sym_quality * 20 + break_quality * 20)
                note = (f"double bottom at {sup_touches}-touch H4 support "
                        f"[{sup_level:.2f}] -- M15 neckline broken")

    return {"long": long_score, "short": short_score, "note": note}
```

Then add the registry entry — find the end of `STRATEGY_REGISTRY` (whatever
its current last entries are; on the local copy that's `mtr_range_regime`/
`mtr_trend_regime`) and add, immediately before the closing `}`:

```python
    # ---- 29th: user-uploaded gold swing-trading course method -- a multi-touch
    # H4 zone (proxy for the course's Weekly/Daily key levels) plus a nested
    # M15 double-top/double-bottom reversal confirmed by a neckline break.
    # Needs only H4 + M15 OHLC + atr14, already present in every scan.
    "zone_mw_reversal": ("HTF Zone + M/W Reversal ★", score_zone_mw_reversal),
}
```

**Important:** confirm what the VPS's `STRATEGY_REGISTRY` actually ends
with first (`grep -n "STRATEGY_REGISTRY = {" -A 60 strategies.py | tail
-20`) — don't assume it matches the local copy's exact ordering/comments,
just add this new entry right before the final `}`.

## Step 2 — `xauusd_mt5_strategy.py`

Find `_RECOMMENDED_STRATEGY_WEIGHTS = {` and its closing `}`. Add one new
entry immediately before the closing `}`:

```python
    "zone_mw_reversal": 1.1,  # 29th -- multi-touch H4 zone + M15 double top/bottom + neckline break
}
```

(No other change needed here — `STRATEGY_WEIGHTS` is derived automatically
from `strategies.STRATEGY_REGISTRY`, so the new strategy is picked up
automatically once Step 1 is done.)

## Step 3 — `strategy_config_ui.py`

Find the end of `DEFAULT_CONFIG["confluence"]["strategies"]` and add,
immediately before the closing `},`:

```python
            # 29th: from the user's uploaded gold swing-trading course --
            # multi-touch H4 zone (proxy for Weekly/Daily key levels) plus a
            # nested M15 double-top/double-bottom reversal confirmed by a
            # neckline break. Weight 1.1 -- same tier as supply_demand/fair_value_gap.
            "zone_mw_reversal": {"enabled": True, "weight": 1.1},
```

Find `STRATEGY13_LABELS = {` and add, immediately before its closing `}`:

```python
    "zone_mw_reversal":   "29. HTF Zone + M/W Reversal (Course Method) ★",
```

## Step 4 — `generate_dashboard.py`

Two small text-only changes (the table is generated dynamically from
`strategy_scores.json`, so it will automatically show the new row once the
EA produces it — these are just the header/comment text). Find whatever
number is currently in these two lines and update it to the new total
(confirm the actual current count first — it may not be 28 if other
strategies were added on the VPS since this prompt was written):

- `    # ---- confluence multi-strategy (N) scores ----`
- `  <h2>Multi-Strategy Confluence (N) — Live Scores</h2>`

## Step 5 — `CLAUDE.md`

Update the `## Strategies (N total)` section to reflect the new total and
add a short paragraph for the 29th strategy, following the style already
used for the 26th-28th entries (see the local copy's `CLAUDE.md` for the
exact wording used there).

## Verification before calling this done

1. Syntax-check all 4 edited files (`python -m py_compile strategies.py
   strategy_config_ui.py xauusd_mt5_strategy.py generate_dashboard.py`).
2. Run a synthetic logic test directly against `score_zone_mw_reversal`
   (no MT5 needed — pandas DataFrames only): build an H4 series that
   oscillates up to a resistance level (e.g. ~2450) and back down 3 times
   (>= `min_touches` swing-high touches within tolerance), and an M15
   series that leads in, makes a first peak near that level, pulls back to
   a neckline, makes a second peak near the same level, then breaks down
   through the neckline by more than `neckline_break_atr` x ATR(M15) on the
   final closed bar. Confirm it returns a `short` score with a note
   mentioning "double top at N-touch H4 resistance ... M15 neckline
   broken". Then confirm: (a) the same H4 zone with the M15 pattern present
   but NOT yet broken through the neckline returns 0/0 with a "no M/W
   reversal pattern at a proven H4 zone yet" note, and (b) a smooth-trending
   series with no H4 zone clustering returns 0/0 with a "no multi-touch H4
   zone found" note. This was already verified on the local copy (all 3
   cases passed) — just confirm the VPS copy behaves identically.
3. Confirm `strategy_config.json` on the VPS still loads correctly after a
   restart (the new key should backfill via `_deep_merge` the same way
   `climax_reversal_sr` did) — check the UI's confluence-strategies tab
   shows "29. HTF Zone + M/W Reversal (Course Method) ★" as a new
   checkbox/weight row.
4. While in `strategies.py`, also confirm whether `mtr_range_regime` /
   `mtr_trend_regime` (the 27th/28th strategies) are already fully wired
   into the VPS's `strategy_config.json`/`strategy_config_ui.py`/
   `CLAUDE.md`/dashboard text — on the Mac mirror these were found already
   implemented in `strategies.py`'s registry but not yet reflected in those
   other 4 places before this session's edits. If the VPS has the same gap,
   flag it rather than fixing it silently (it's a separate, pre-existing
   item, not part of this strategy's sync).
5. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, any other
   strategy's weight, or any risk/lot parameter as part of this sync — this
   is purely additive, matching the local copy exactly.
6. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running — apply the edits, syntax-check, and report back; let
   the user decide when to restart so the new strategy actually takes effect
   in the live scan.
