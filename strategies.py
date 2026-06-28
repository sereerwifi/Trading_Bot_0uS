"""
Multi-Strategy (31) Confluence Scoring Engine for the XAUUSD MT5 EA
=============================================================
Each strategy function below scores the CURRENT market 0-100 for "long" and
0-100 for "short" independently (a market can score on both sides at once —
e.g. a sweep that just reversed will score high on one side and near-zero on
the other; a flat/no-signal market scores low on both sides).

IMPORTANT — read this before tuning:
  These are heuristic, rules-based approximations of each named concept
  (especially the ICT-style ones: Order Block, FVG, Liquidity Sweep,
  BOS/CHoCH). Real ICT/SMC analysis is partly discretionary and there is no
  single "correct" formula — treat these scores as a structured, consistent
  proxy you can backtest and retune, not as ground truth. Adjust the
  thresholds/lookbacks in each function (or the per-strategy weight in
  config) if you find a particular strategy is over/under-scoring on your
  data.

Every function takes a single `data` dict (built by `build_market_data()` in
xauusd_mt5_strategy.py) with keys:
    "d1", "h4", "h1", "m15"   -> pandas DataFrames (oldest->newest rows),
                                  each already has columns: open high low
                                  close time, PLUS ema20/ema50/ema200, rsi14,
                                  atr14 precomputed (see _enrich()).
    "m5", "m1"                 -> same shape, added for the 4 scalping
                                  strategies (#21-24). Strategies that use
                                  these keys degrade to a 0/0 score with an
                                  explanatory note if they're missing, so the
                                  other 20 strategies are unaffected on a
                                  build that hasn't added m1/m5 yet.
    "now"                      -> datetime.now()

Returns: {"long": float 0-100, "short": float 0-100, "note": str}
"""

import numpy as np
import pandas as pd
from collections import deque

# ----------------------------- shared indicator helpers ---------------------
# Self-contained (deliberately not imported from xauusd_mt5_strategy.py) to
# keep this module import-safe on its own (no MetaTrader5 dependency, no
# circular import).

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def macd(series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def _clip(x):
    return float(max(0.0, min(100.0, x)))


def _swing_points(df, lookback=80, order=3):
    """Very small local swing-high/low finder: a bar is a swing high if its
    high is the max within +/-`order` bars, similarly for swing low. Returns
    two lists of (index, price) tuples, oldest->newest, within the lookback
    window."""
    window = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    n = len(window)
    for i in range(order, n - order):
        seg_h = window["high"].iloc[i - order:i + order + 1]
        seg_l = window["low"].iloc[i - order:i + order + 1]
        if window["high"].iloc[i] == seg_h.max():
            highs.append((i, window["high"].iloc[i]))
        if window["low"].iloc[i] == seg_l.min():
            lows.append((i, window["low"].iloc[i]))
    return highs, lows


def enrich(df):
    """Adds the common indicator columns every strategy expects. Call once
    per timeframe per scan (build_market_data() does this)."""
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi14"] = rsi(df["close"], 14)
    df["atr14"] = atr(df, 14)
    macd_line, sig_line, hist = macd(df["close"])
    df["macd"], df["macd_signal"], df["macd_hist"] = macd_line, sig_line, hist
    return df


# ----------------------------- 1. Order Block (ICT) --------------------------
def score_order_block(data):
    """Last opposite-colour candle immediately before the largest recent
    impulse leg = the order block. Score rises the closer current price sits
    inside that candle's body/range (a retest), and decays once price has
    moved well past it."""
    df = data["h4"].tail(60).reset_index(drop=True)
    if len(df) < 20:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H4 data"}

    ranges = (df["high"] - df["low"])
    impulse_idx = ranges.iloc[5:-2].idxmax()  # leave room to look 1 bar before/after
    impulse = df.iloc[impulse_idx]
    bullish_impulse = impulse["close"] > impulse["open"]

    ob_idx = impulse_idx - 1
    if ob_idx < 0:
        return {"long": 0.0, "short": 0.0, "note": "no candle before impulse"}
    ob = df.iloc[ob_idx]
    ob_low, ob_high = min(ob["open"], ob["close"]), max(ob["open"], ob["close"])
    last_price = df["close"].iloc[-1]
    atr_now = data["h4"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (ob_high - ob_low) or 1.0

    bars_since = len(df) - 1 - impulse_idx
    recency_decay = max(0.0, 1.0 - bars_since / 40.0)

    long_score = short_score = 0.0
    note = "no fresh order block in range"
    if bullish_impulse:
        # bearish candle before a bullish impulse -> bullish OB, longs on retest
        if ob_low - atr_now * 0.5 <= last_price <= ob_high + atr_now * 0.25:
            dist = min(abs(last_price - ob_low), abs(last_price - ob_high))
            proximity = max(0.0, 1.0 - dist / max(atr_now, 1e-6))
            long_score = _clip(100 * proximity * (0.5 + 0.5 * recency_decay))
            note = f"price retesting bullish OB [{ob_low:.2f}-{ob_high:.2f}]"
    else:
        if ob_low - atr_now * 0.25 <= last_price <= ob_high + atr_now * 0.5:
            dist = min(abs(last_price - ob_low), abs(last_price - ob_high))
            proximity = max(0.0, 1.0 - dist / max(atr_now, 1e-6))
            short_score = _clip(100 * proximity * (0.5 + 0.5 * recency_decay))
            note = f"price retesting bearish OB [{ob_low:.2f}-{ob_high:.2f}]"

    # ---- DOM confirmation bonus (Order Block Flow Elite style) ----
    # If a real MT5 Depth-of-Market snapshot is available (data["dom"], see
    # score_order_flow_dom() below), use the live bid/ask volume imbalance to
    # confirm the OB retest: heavier resting bids than asks supports a long
    # retest, heavier asks supports a short retest. This only ever boosts a
    # score that's already non-zero — DOM never creates a signal on its own
    # here, it just adds conviction when institutional order flow agrees.
    dom = data.get("dom")
    if dom and (dom.get("bids") or dom.get("asks")):
        bid_vol = dom.get("bid_volume", 0.0)
        ask_vol = dom.get("ask_volume", 0.0)
        total = bid_vol + ask_vol
        if total > 0:
            imbalance = (bid_vol - ask_vol) / total  # +1 = all bids, -1 = all asks
            if long_score > 0 and imbalance > 0.1:
                long_score = _clip(long_score * (1.0 + min(imbalance, 0.5)))
                note += " + DOM bid-heavy confirms"
            if short_score > 0 and imbalance < -0.1:
                short_score = _clip(short_score * (1.0 + min(-imbalance, 0.5)))
                note += " + DOM ask-heavy confirms"

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 2. Supply & Demand -----------------------------
def score_supply_demand(data):
    """Looks for a small-range 'base' (1-3 tight candles) immediately
    followed by a strong directional breakout candle. The base = the zone;
    score rises when price returns to an as-yet-unmitigated zone."""
    df = data["h4"].tail(80).reset_index(drop=True)
    if len(df) < 20:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H4 data"}

    atr_series = data["h4"]["atr14"]
    atr_now = atr_series.iloc[-1] if not pd.isna(atr_series.iloc[-1]) else (df["high"] - df["low"]).mean()
    body = (df["close"] - df["open"]).abs()
    rng = df["high"] - df["low"]

    long_score = short_score = 0.0
    note = "no fresh supply/demand zone in range"
    best_long_score, best_short_score = 0.0, 0.0
    last_price = df["close"].iloc[-1]

    for i in range(3, len(df) - 1):
        base = df.iloc[i - 3:i]
        if not (base_is_tight := (rng.iloc[i - 3:i].mean() < atr_now * 0.6)):
            continue
        breakout = df.iloc[i]
        if rng.iloc[i] < atr_now * 1.2 or body.iloc[i] < rng.iloc[i] * 0.6:
            continue
        zone_low, zone_high = base["low"].min(), base["high"].max()
        bullish_break = breakout["close"] > breakout["open"]
        bars_since = len(df) - 1 - i
        if bars_since > 50:
            continue
        recency = max(0.0, 1.0 - bars_since / 50.0)
        in_zone = zone_low - atr_now * 0.25 <= last_price <= zone_high + atr_now * 0.25
        if not in_zone:
            continue
        dist = min(abs(last_price - zone_low), abs(last_price - zone_high))
        proximity = max(0.0, 1.0 - dist / max(atr_now, 1e-6))
        s = _clip(100 * proximity * (0.4 + 0.6 * recency))
        if bullish_break:
            best_long_score = max(best_long_score, s)
        else:
            best_short_score = max(best_short_score, s)

    if best_long_score or best_short_score:
        note = "price inside a fresh demand/supply base"
    return {"long": best_long_score, "short": best_short_score, "note": note}


# ----------------------------- 3. EMA Cross -----------------------------------
def score_ema_cross(data):
    """EMA20/EMA50 cross on H1, scored by freshness and agreement with the
    H4 trend bias (EMA50 vs EMA200)."""
    h1 = data["h1"]
    h4 = data["h4"]
    if len(h1) < 55 or len(h4) < 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient data"}

    fast, slow = h1["ema20"], h1["ema50"]
    diff = fast - slow
    cross_up_idx = None
    cross_down_idx = None
    for i in range(len(diff) - 1, max(len(diff) - 12, 1), -1):
        if diff.iloc[i] > 0 and diff.iloc[i - 1] <= 0:
            cross_up_idx = i
            break
        if diff.iloc[i] < 0 and diff.iloc[i - 1] >= 0:
            cross_down_idx = i
            break

    h4_long_bias = h4["close"].iloc[-1] > h4["ema50"].iloc[-1] > h4["ema200"].iloc[-1]
    h4_short_bias = h4["close"].iloc[-1] < h4["ema50"].iloc[-1] < h4["ema200"].iloc[-1]

    long_score = short_score = 0.0
    note = "no fresh EMA cross"
    if cross_up_idx is not None:
        bars_since = len(diff) - 1 - cross_up_idx
        freshness = max(0.0, 1.0 - bars_since / 12.0)
        bonus = 1.2 if h4_long_bias else 0.7
        long_score = _clip(100 * freshness * bonus)
        note = f"EMA20 crossed above EMA50 {bars_since} bars ago"
    if cross_down_idx is not None:
        bars_since = len(diff) - 1 - cross_down_idx
        freshness = max(0.0, 1.0 - bars_since / 12.0)
        bonus = 1.2 if h4_short_bias else 0.7
        short_score = _clip(100 * freshness * bonus)
        note = f"EMA20 crossed below EMA50 {bars_since} bars ago"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 4. RSI Divergence -------------------------------
def score_rsi_divergence(data):
    """Regular divergence between price swing highs/lows and RSI on H1 over
    the last ~40 bars. Bullish divergence (price lower-low, RSI higher-low)
    scores long; bearish divergence (price higher-high, RSI lower-high)
    scores short."""
    df = data["h1"].tail(40).reset_index(drop=True)
    if len(df) < 20:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    lows, highs = _swing_points(df, lookback=40, order=2)
    long_score = short_score = 0.0
    note = "no clear divergence"

    if len(lows) >= 2:
        (i1, p1), (i2, p2) = lows[-2], lows[-1]
        r1, r2 = df["rsi14"].iloc[i1], df["rsi14"].iloc[i2]
        if p2 < p1 and r2 > r1 and not pd.isna(r1) and not pd.isna(r2):
            price_drop_pct = (p1 - p2) / p1 * 100 if p1 else 0
            rsi_gain = r2 - r1
            long_score = _clip(40 + price_drop_pct * 8 + rsi_gain * 1.5)
            note = "bullish RSI divergence on H1"

    if len(highs) >= 2:
        (i1, p1), (i2, p2) = highs[-2], highs[-1]
        r1, r2 = df["rsi14"].iloc[i1], df["rsi14"].iloc[i2]
        if p2 > p1 and r2 < r1 and not pd.isna(r1) and not pd.isna(r2):
            price_gain_pct = (p2 - p1) / p1 * 100 if p1 else 0
            rsi_drop = r1 - r2
            s = _clip(40 + price_gain_pct * 8 + rsi_drop * 1.5)
            if s > short_score:
                short_score = s
                note = "bearish RSI divergence on H1"

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 5. London Breakout ------------------------------
def score_london_breakout(data, london_start=(10, 0), london_end=(14, 0)):
    """Builds the first-hour range of the London session from M15 bars,
    then scores a breakout above/below that range during the rest of the
    session.

    Session corrected from Thai/Bangkok time (UTC+7) to broker UTC+3:
      BKK 14:00-17:00 → broker 10:00-14:00 (07:00-11:00 UTC = London BST open)."""
    df = data["m15"]
    now = data["now"]
    if len(df) < 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient M15 data"}

    today = now.date()
    sess_start = pd.Timestamp.combine(today, pd.Timestamp(f"{london_start[0]:02d}:{london_start[1]:02d}").time())
    range_end = sess_start + pd.Timedelta(hours=1)
    sess_end = pd.Timestamp.combine(today, pd.Timestamp(f"{london_end[0]:02d}:{london_end[1]:02d}").time())

    if not (sess_start <= now_ts(now) <= sess_end):
        return {"long": 0.0, "short": 0.0, "note": "outside London session window"}

    opening = df[(df["time"] >= sess_start) & (df["time"] < range_end)]
    if opening.empty:
        return {"long": 0.0, "short": 0.0, "note": "no opening-range bars yet"}

    rng_high, rng_low = opening["high"].max(), opening["low"].min()
    rng_size = max(rng_high - rng_low, 1e-6)
    after = df[df["time"] >= range_end]
    if after.empty:
        return {"long": 0.0, "short": 0.0, "note": "opening range still forming"}

    last = after.iloc[-1]
    body = abs(last["close"] - last["open"])
    long_score = short_score = 0.0
    note = "no breakout yet"
    if last["close"] > rng_high:
        breakout_size = (last["close"] - rng_high) / rng_size
        momentum = body / rng_size
        long_score = _clip(50 + breakout_size * 60 + momentum * 20)
        note = f"London breakout above opening range high {rng_high:.2f}"
    elif last["close"] < rng_low:
        breakout_size = (rng_low - last["close"]) / rng_size
        momentum = body / rng_size
        short_score = _clip(50 + breakout_size * 60 + momentum * 20)
        note = f"London breakdown below opening range low {rng_low:.2f}"
    return {"long": long_score, "short": short_score, "note": note}


def now_ts(now):
    return pd.Timestamp(now)


# ----------------------------- 6. Fibonacci ------------------------------------
def score_fibonacci(data):
    """Same swing+fib-zone logic as the original fib_confluence strategy,
    expressed as a 0-100 score instead of a hard pass/fail: highest in the
    50-61.8% retracement zone with MACD/RSI confirming, decaying outside it."""
    df = data["h1"]
    if len(df) < 60:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    window = df.tail(50)
    swing_low, swing_high = window["low"].min(), window["high"].max()
    lo_idx, hi_idx = window["low"].idxmin(), window["high"].idxmax()
    leg_up = lo_idx < hi_idx
    diff = max(swing_high - swing_low, 1e-6)

    # Retracement zones are always measured from the origin of the move:
    # up-leg: retrace DOWN from swing_high (look for longs in the pullback zone)
    # down-leg: retrace UP from swing_low (look for shorts in the rally zone)
    if leg_up:
        zone_a = swing_high - 0.5 * diff
        zone_b = swing_high - 0.618 * diff
    else:
        zone_a = swing_low + 0.5 * diff
        zone_b = swing_low + 0.618 * diff
    zone_low, zone_high = min(zone_a, zone_b), max(zone_a, zone_b)

    last, prev = df.iloc[-1], df.iloc[-2]
    price = last["close"]

    long_score = short_score = 0.0
    note = "price not in fib 50-61.8% zone"
    if zone_low <= price <= zone_high:
        depth = 1.0 - abs(price - (zone_low + zone_high) / 2) / max((zone_high - zone_low) / 2, 1e-6)
        macd_cross_up = prev["macd_hist"] <= 0 and last["macd_hist"] > 0
        macd_cross_down = prev["macd_hist"] >= 0 and last["macd_hist"] < 0
        rsi_mid = 35 <= last["rsi14"] <= 65
        base = 40 + depth * 30
        if leg_up:
            long_score = _clip(base + (20 if macd_cross_up else 0) + (10 if rsi_mid else 0))
            note = "price retracing into bullish fib zone (50-61.8% of up-leg)"
        else:
            short_score = _clip(base + (20 if macd_cross_down else 0) + (10 if rsi_mid else 0))
            note = "price rallying into bearish fib zone (50-61.8% retrace of down-leg)"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 7. VWAP Rejection --------------------------------
def _session_vwap(df):
    today_mask = df["time"].dt.date == df["time"].iloc[-1].date()
    day = df[today_mask]
    if day.empty or day["close"].sum() == 0:
        day = df.tail(48)  # fallback: ~last 12h of M15 bars
    typical = (day["high"] + day["low"] + day["close"]) / 3.0
    vol = day["tick_volume"] if "tick_volume" in day.columns else pd.Series(np.ones(len(day)), index=day.index)
    vol = vol.replace(0, 1)
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum()
    return (cum_pv / cum_v).iloc[-1]


def score_vwap_rejection(data):
    """Session VWAP on M15: scores a bullish rejection (wick below VWAP,
    close back above) or bearish rejection (wick above VWAP, close back
    below), scaled by wick size relative to ATR."""
    df = data["m15"]
    if len(df) < 20:
        return {"long": 0.0, "short": 0.0, "note": "insufficient M15 data"}

    vwap = _session_vwap(df)
    last = df.iloc[-1]
    atr_now = data["m15"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    long_score = short_score = 0.0
    note = "no VWAP rejection"
    wick_down = last["open"] - last["low"] if last["close"] >= last["open"] else last["close"] - last["low"]
    wick_up = last["high"] - last["close"] if last["close"] >= last["open"] else last["high"] - last["open"]

    # Require the wick to pierce VWAP by a meaningful amount — not just any bar
    # that happens to span the VWAP line.  A 15% ATR minimum penetration filters
    # out noise candles; strict close (not >=/<= VWAP) ensures a genuine rejection.
    penetration_min = atr_now * 0.15

    if last["low"] < vwap - penetration_min and last["close"] > vwap:
        wick_ratio = wick_down / atr_now
        long_score = _clip(40 + wick_ratio * 80)
        note = f"bullish rejection off session VWAP {vwap:.2f}"
    if last["high"] > vwap + penetration_min and last["close"] < vwap:
        wick_ratio = wick_up / atr_now
        s = _clip(40 + wick_ratio * 80)
        if s > short_score:
            short_score = s
            note = f"bearish rejection off session VWAP {vwap:.2f}"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 8. News Fade -------------------------------------
def score_news_fade(data):
    """Heuristic proxy only — this EA has no real economic-calendar/news
    feed wired in. Approximates a 'news spike' as an abnormally large-range
    candle (>2x ATR) on M15, then fades it once the next candle shows a
    reversal back toward the pre-spike level. Consider wiring a real
    calendar API (e.g. ForexFactory/Investing.com feed) for a more accurate
    version of this strategy."""
    df = data["m15"]
    if len(df) < 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient M15 data"}

    # Use ATR from the bar BEFORE the spike so the spike's own outsized range
    # doesn't inflate the 14-period rolling ATR and make the threshold self-defeating.
    atr_now = data["m15"]["atr14"].iloc[-3]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    spike = df.iloc[-2]
    reversal = df.iloc[-1]
    spike_range = spike["high"] - spike["low"]
    long_score = short_score = 0.0
    note = "no news-style spike detected"
    if spike_range >= 2.0 * atr_now:
        spike_bullish = spike["close"] > spike["open"]
        reversal_body = reversal["close"] - reversal["open"]
        magnitude = min(spike_range / atr_now / 4.0, 1.0)
        if spike_bullish and reversal_body < 0:
            short_score = _clip(40 + magnitude * 60)
            note = "fading an oversized bullish spike candle"
        elif not spike_bullish and reversal_body > 0:
            long_score = _clip(40 + magnitude * 60)
            note = "fading an oversized bearish spike candle"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 9. Multi-TF Align --------------------------------
def score_multi_tf_align(data):
    """% of timeframes (D1, H4, H1, M15) whose EMA50 vs EMA200 stack agrees
    on a direction. 4/4 aligned = 100, 0/4 = 0; mixed timeframes scale
    linearly in between."""
    tfs = ["d1", "h4", "h1", "m15"]
    bull_votes = bear_votes = 0
    total = 0
    for tf in tfs:
        df = data.get(tf)
        if df is None or len(df) < 5 or pd.isna(df["ema200"].iloc[-1]):
            continue
        total += 1
        last = df.iloc[-1]
        if last["close"] > last["ema50"] > last["ema200"]:
            bull_votes += 1
        elif last["close"] < last["ema50"] < last["ema200"]:
            bear_votes += 1
    if total == 0:
        return {"long": 0.0, "short": 0.0, "note": "insufficient data"}
    long_score = _clip(100 * bull_votes / total)
    short_score = _clip(100 * bear_votes / total)
    return {"long": long_score, "short": short_score,
            "note": f"{bull_votes}/{total} TFs bullish, {bear_votes}/{total} bearish"}


# ----------------------------- 10. BOS / CHoCH ----------------------------------
def score_bos_choch(data):
    """Tracks the last two swing highs/lows on H4. BOS = price closes beyond
    the most recent swing in the SAME direction as the prior leg
    (continuation). CHoCH = price closes beyond a swing in the OPPOSITE
    direction of the prior leg (early reversal signal). Both score in the
    direction implied; CHoCH is weighted slightly higher since it's the
    higher-value reversal signal when it fires cleanly."""
    df = data["h4"].tail(100).reset_index(drop=True)
    if len(df) < 30:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H4 data"}

    highs, lows = _swing_points(df, lookback=100, order=3)
    if len(highs) < 2 or len(lows) < 2:
        return {"long": 0.0, "short": 0.0, "note": "not enough swing points yet"}

    last_close = df["close"].iloc[-1]
    last_swing_high = highs[-1][1]
    last_swing_low = lows[-1][1]
    prior_leg_up = lows[-1][0] < highs[-1][0]  # most recent swing low formed before most recent swing high

    long_score = short_score = 0.0
    note = "no structure break yet"
    bars_since_high = len(df) - 1 - highs[-1][0]
    bars_since_low = len(df) - 1 - lows[-1][0]

    if last_close > last_swing_high and bars_since_high <= 10:
        freshness = max(0.0, 1.0 - bars_since_high / 10.0)
        if prior_leg_up:
            long_score = _clip(60 + freshness * 40)
            note = "BOS: continuation break above last swing high"
        else:
            long_score = _clip(50 + freshness * 50)
            note = "CHoCH: reversal break above last swing high"
    if last_close < last_swing_low and bars_since_low <= 10:
        freshness = max(0.0, 1.0 - bars_since_low / 10.0)
        if not prior_leg_up:
            s = _clip(60 + freshness * 40)
            note2 = "BOS: continuation break below last swing low"
        else:
            s = _clip(50 + freshness * 50)
            note2 = "CHoCH: reversal break below last swing low"
        if s > short_score:
            short_score = s
            note = note2

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 11. Liquidity Sweep ------------------------------
def score_liquidity_sweep(data):
    """Detects a wick that pierces a recent swing high/low (a 'stop hunt')
    and then closes back inside range within the same or next candle. The
    score is on the side OPPOSITE the sweep (sweep highs -> bearish bias;
    sweep lows -> bullish bias), scaled by how far the wick pierced and how
    quickly price reclaimed the level."""
    df = data["h1"].tail(60).reset_index(drop=True)
    if len(df) < 20:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    highs, lows = _swing_points(df, lookback=60, order=3)
    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    last = df.iloc[-1]
    long_score = short_score = 0.0
    note = "no liquidity sweep detected"

    recent_highs = [h for h in highs if h[0] < len(df) - 1]
    recent_lows = [l for l in lows if l[0] < len(df) - 1]

    # Check every recent swing high individually — not just the extreme —
    # so sweeps of intermediate levels (which also have stops above them) fire.
    for h_idx, h_level in recent_highs[-5:]:
        if last["high"] > h_level and last["close"] < h_level:
            pierce = (last["high"] - h_level) / atr_now
            s = _clip(40 + pierce * 100)
            if s > short_score:
                short_score = s
                note = f"swept liquidity above {h_level:.2f} then closed back below"

    for l_idx, l_level in recent_lows[-5:]:
        if last["low"] < l_level and last["close"] > l_level:
            pierce = (l_level - last["low"]) / atr_now
            s = _clip(40 + pierce * 100)
            if s > long_score:
                long_score = s
                note = f"swept liquidity below {l_level:.2f} then closed back above"

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 12. Fair Value Gap (FVG) -------------------------
def score_fair_value_gap(data):
    """3-candle imbalance: bullish FVG = candle1.high < candle3.low (gap left
    behind by a strong impulse), bearish FVG = candle1.low > candle3.high.
    Scores highest when price is currently retracing back INTO an unfilled
    gap, in the same direction as the H4 bias."""
    df = data["h1"].tail(60).reset_index(drop=True)
    if len(df) < 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    h4 = data["h4"]
    h4_long_bias = h4["close"].iloc[-1] > h4["ema50"].iloc[-1] > h4["ema200"].iloc[-1]
    h4_short_bias = h4["close"].iloc[-1] < h4["ema50"].iloc[-1] < h4["ema200"].iloc[-1]

    last_price = df["close"].iloc[-1]
    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    best_long, best_short = 0.0, 0.0
    note = "no unfilled FVG near price"

    for i in range(2, len(df)):
        c1, c3 = df.iloc[i - 2], df.iloc[i]
        bars_since = len(df) - 1 - i
        if bars_since > 30:
            continue
        recency = max(0.0, 1.0 - bars_since / 30.0)
        if c1["high"] < c3["low"]:  # bullish FVG
            gap_low, gap_high = c1["high"], c3["low"]
            if gap_low - atr_now * 0.2 <= last_price <= gap_high + atr_now * 0.2:
                gap_size = max(gap_high - gap_low, 1e-6)
                dist = min(abs(last_price - gap_low), abs(last_price - gap_high))
                proximity = max(0.0, 1.0 - dist / max(gap_size, atr_now))
                bonus = 1.2 if h4_long_bias else 0.8
                s = _clip(100 * proximity * (0.4 + 0.6 * recency) * bonus)
                if s > best_long:
                    best_long = s
                    note = f"price retracing into bullish FVG [{gap_low:.2f}-{gap_high:.2f}]"
        if c1["low"] > c3["high"]:  # bearish FVG
            gap_low, gap_high = c3["high"], c1["low"]
            if gap_low - atr_now * 0.2 <= last_price <= gap_high + atr_now * 0.2:
                gap_size = max(gap_high - gap_low, 1e-6)
                dist = min(abs(last_price - gap_low), abs(last_price - gap_high))
                proximity = max(0.0, 1.0 - dist / max(gap_size, atr_now))
                bonus = 1.2 if h4_short_bias else 0.8
                s = _clip(100 * proximity * (0.4 + 0.6 * recency) * bonus)
                if s > best_short:
                    best_short = s
                    note = f"price retracing into bearish FVG [{gap_low:.2f}-{gap_high:.2f}]"

    return {"long": best_long, "short": best_short, "note": note}


# ----------------------------- 13. Opening Range Breakout -----------------------
def score_opening_range_breakout(data, session_start=(15, 0)):
    """Same mechanic as London Breakout but anchored to the NY/Comex gold
    session open (15:00 broker/UTC+3 = 12:00 UTC = 08:00 ET) — first 30
    minutes define the range, a directional break afterward scores the
    breakout direction.

    session_start default corrected to (15, 0) broker time (UTC+3):
      old (19, 0) = 19:00 UTC+3 = 16:00 UTC = NY afternoon (wrong session).
      new (15, 0) = 15:00 UTC+3 = 12:00 UTC = Comex/NY gold open (correct).

    Requires the breakout candle body to cover at least 30% of the opening
    range (momentum confirmation) — reduces false-breakout signals that fired
    on tiny M15 pokes beyond the range boundary."""
    df = data["m15"]
    now = data["now"]
    if len(df) < 6:
        return {"long": 0.0, "short": 0.0, "note": "insufficient M15 data"}

    atr_now = data["m15"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    today = now.date()
    sess_start = pd.Timestamp.combine(today, pd.Timestamp(f"{session_start[0]:02d}:{session_start[1]:02d}").time())
    range_end = sess_start + pd.Timedelta(minutes=30)

    opening = df[(df["time"] >= sess_start) & (df["time"] < range_end)]
    after = df[df["time"] >= range_end]
    if opening.empty or after.empty:
        return {"long": 0.0, "short": 0.0, "note": "opening range not available yet"}

    rng_high, rng_low = opening["high"].max(), opening["low"].min()
    rng_size = max(rng_high - rng_low, 1e-6)
    last = after.iloc[-1]
    body = abs(last["close"] - last["open"])

    long_score = short_score = 0.0
    note = "no opening-range breakout yet"
    bars_since_open = len(after) - 1
    if bars_since_open > 8:  # only score the first 2 hours after the open
        return {"long": 0.0, "short": 0.0, "note": "too far past opening range window"}

    # Require a momentum candle: body must cover at least 30% of the range
    # so tiny wicks beyond the boundary don't trigger a score.
    body_min = rng_size * 0.30

    if last["close"] > rng_high and body >= body_min:
        breakout_size = (last["close"] - rng_high) / rng_size
        momentum = body / rng_size
        long_score = _clip(50 + breakout_size * 50 + momentum * 20)
        note = f"ORB breakout above {rng_high:.2f}"
    elif last["close"] < rng_low and body >= body_min:
        breakout_size = (rng_low - last["close"]) / rng_size
        momentum = body / rng_size
        short_score = _clip(50 + breakout_size * 50 + momentum * 20)
        note = f"ORB breakdown below {rng_low:.2f}"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 14. MACD Cross (merged from v1) ------------------
def score_macd_cross(data):
    """MACD line/signal cross on H1, scored by freshness and agreement with
    the H4 trend bias (EMA50 vs EMA200) — same idea as score_ema_cross() but
    using the MACD trigger instead. This is the v1 'macd_cross' strategy,
    folded into the confluence engine so it isn't a separate/unused config."""
    h1, h4 = data["h1"], data["h4"]
    if len(h1) < 35 or len(h4) < 10:
        return {"long": 0.0, "short": 0.0, "note": "insufficient data"}

    hist = h1["macd_hist"]
    cross_up_idx = cross_down_idx = None
    for i in range(len(hist) - 1, max(len(hist) - 12, 1), -1):
        if pd.isna(hist.iloc[i]) or pd.isna(hist.iloc[i - 1]):
            continue
        if hist.iloc[i] > 0 and hist.iloc[i - 1] <= 0:
            cross_up_idx = i
            break
        if hist.iloc[i] < 0 and hist.iloc[i - 1] >= 0:
            cross_down_idx = i
            break

    h4_long_bias = h4["close"].iloc[-1] > h4["ema50"].iloc[-1] > h4["ema200"].iloc[-1]
    h4_short_bias = h4["close"].iloc[-1] < h4["ema50"].iloc[-1] < h4["ema200"].iloc[-1]

    long_score = short_score = 0.0
    note = "no fresh MACD cross"
    if cross_up_idx is not None:
        bars_since = len(hist) - 1 - cross_up_idx
        freshness = max(0.0, 1.0 - bars_since / 12.0)
        bonus = 1.2 if h4_long_bias else 0.7
        long_score = _clip(100 * freshness * bonus)
        note = f"MACD crossed above signal {bars_since} bars ago"
    if cross_down_idx is not None:
        bars_since = len(hist) - 1 - cross_down_idx
        freshness = max(0.0, 1.0 - bars_since / 12.0)
        bonus = 1.2 if h4_short_bias else 0.7
        short_score = _clip(100 * freshness * bonus)
        note = f"MACD crossed below signal {bars_since} bars ago"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 15. Bollinger Band Breakout (merged from v1) -----
def score_bb_breakout(data, period=20, std_mult=2.0, squeeze_pct=2.0):
    """Bollinger Bands on H1: looks for a squeeze (bandwidth below
    squeeze_pct% of price) followed by a candle closing outside a band —
    scores the breakout direction, higher when the prior squeeze was tight."""
    df = data["h1"]
    if len(df) < period + 5:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    close = df["close"]
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    upper, lower = mid + std_mult * sd, mid - std_mult * sd
    bandwidth_pct = ((upper - lower) / mid.replace(0, np.nan)) * 100

    last = df.iloc[-1]
    last_upper, last_lower = upper.iloc[-1], lower.iloc[-1]
    if pd.isna(last_upper) or pd.isna(last_lower):
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    prior_squeeze = bandwidth_pct.iloc[-6:-1].min()
    was_squeezed = not pd.isna(prior_squeeze) and prior_squeeze <= squeeze_pct
    squeeze_bonus = 1.3 if was_squeezed else 0.8

    long_score = short_score = 0.0
    note = "no BB breakout"
    if last["close"] > last_upper:
        breakout_pct = (last["close"] - last_upper) / max(last_upper - mid.iloc[-1], 1e-6)
        long_score = _clip((50 + breakout_pct * 50) * squeeze_bonus)
        note = "closed above upper Bollinger Band" + (" after squeeze" if was_squeezed else "")
    elif last["close"] < last_lower:
        breakout_pct = (last_lower - last["close"]) / max(mid.iloc[-1] - last_lower, 1e-6)
        short_score = _clip((50 + breakout_pct * 50) * squeeze_bonus)
        note = "closed below lower Bollinger Band" + (" after squeeze" if was_squeezed else "")
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 16. S/R Breakout + Retest (merged from v1) -------
def score_sr_breakout_retest(data, lookback=50, retest_tol_atr=0.3):
    """Finds the nearest swing-high resistance / swing-low support on H1
    over `lookback` bars, scores a clean breakout, and scores even higher
    once price comes back to retest that broken level (the classic
    breakout-then-retest entry)."""
    df = data["h1"].tail(lookback + 5).reset_index(drop=True)
    if len(df) < 20:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    highs, lows = _swing_points(df, lookback=lookback, order=3)
    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    last = df.iloc[-1]
    long_score = short_score = 0.0
    note = "no S/R breakout or retest"

    if highs:
        resistance = max(h[1] for h in highs[-3:])
        if last["close"] > resistance:
            dist = (last["close"] - resistance) / atr_now
            long_score = _clip(50 + dist * 60)
            note = f"broke resistance {resistance:.2f}"
        elif abs(last["close"] - resistance) <= atr_now * retest_tol_atr and last["close"] <= resistance:
            # retesting a previously broken level from below-ish, but only meaningful if it was broken earlier
            prior_break = (df["close"].iloc[:-1] > resistance).any()
            if prior_break:
                proximity = 1.0 - abs(last["close"] - resistance) / max(atr_now * retest_tol_atr, 1e-6)
                long_score = _clip(60 + proximity * 40)
                note = f"retesting broken resistance {resistance:.2f} as new support"

    if lows:
        support = min(l[1] for l in lows[-3:])
        if last["close"] < support:
            dist = (support - last["close"]) / atr_now
            s = _clip(50 + dist * 60)
            if s > short_score:
                short_score = s
                note = f"broke support {support:.2f}"
        elif abs(last["close"] - support) <= atr_now * retest_tol_atr and last["close"] >= support:
            prior_break = (df["close"].iloc[:-1] < support).any()
            if prior_break:
                proximity = 1.0 - abs(last["close"] - support) / max(atr_now * retest_tol_atr, 1e-6)
                s = _clip(60 + proximity * 40)
                if s > short_score:
                    short_score = s
                    note = f"retesting broken support {support:.2f} as new resistance"

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 17. Price Action (merged from v1) ----------------
def score_price_action(data, proximity_atr=0.5):
    """Pin bar / engulfing candlestick patterns on H1, scored higher when
    they occur near a recent swing high/low (a 'key level') rather than in
    open space."""
    df = data["h1"].tail(60).reset_index(drop=True)
    if len(df) < 15:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    highs, lows = _swing_points(df, lookback=60, order=3)
    last, prev = df.iloc[-1], df.iloc[-2]
    rng = max(last["high"] - last["low"], 1e-6)
    body = abs(last["close"] - last["open"])

    near_swing_high = any(abs(last["close"] - h[1]) <= atr_now * proximity_atr for h in highs[-3:])
    near_swing_low = any(abs(last["close"] - l[1]) <= atr_now * proximity_atr for l in lows[-3:])
    level_bonus = 1.3 if (near_swing_high or near_swing_low) else 0.8

    long_score = short_score = 0.0
    note = "no price-action signal"

    # bullish pin bar: long lower wick, small body near the top of the range
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    if lower_wick >= rng * 0.55 and body <= rng * 0.35 and near_swing_low:
        long_score = _clip((55 + lower_wick / rng * 40) * level_bonus)
        note = "bullish pin bar at key support level"
    if upper_wick >= rng * 0.55 and body <= rng * 0.35 and near_swing_high:
        s = _clip((55 + upper_wick / rng * 40) * level_bonus)
        if s > short_score:
            short_score = s
            note = "bearish pin bar at key resistance level"

    # engulfing: current body fully engulfs the previous body, opposite colour
    prev_body_low, prev_body_high = min(prev["open"], prev["close"]), max(prev["open"], prev["close"])
    cur_bullish = last["close"] > last["open"]
    prev_bearish = prev["close"] < prev["open"]
    if cur_bullish and prev_bearish and last["open"] <= prev_body_low and last["close"] >= prev_body_high:
        s = _clip((50 + body / rng * 40) * level_bonus)
        if s > long_score:
            long_score = s
            note = "bullish engulfing" + (" at key support level" if near_swing_low else "")
    cur_bearish = last["close"] < last["open"]
    prev_bullish = prev["close"] > prev["open"]
    if cur_bearish and prev_bullish and last["open"] >= prev_body_high and last["close"] <= prev_body_low:
        s = _clip((50 + body / rng * 40) * level_bonus)
        if s > short_score:
            short_score = s
            note = "bearish engulfing" + (" at key resistance level" if near_swing_high else "")

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 18. ATR/Donchian Breakout (merged from v1) -------
def score_atr_donchian_breakout(data, donchian_period=20, atr_mult=1.0):
    """Donchian channel breakout (highest high / lowest low over N bars on
    H1), confirmed by the breakout candle's range being at least atr_mult x
    ATR (filters out weak/noise breakouts)."""
    df = data["h1"]
    if len(df) < donchian_period + 5:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    donchian_high = df["high"].rolling(donchian_period).max().shift(1)
    donchian_low = df["low"].rolling(donchian_period).min().shift(1)
    last = df.iloc[-1]
    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    ch_high, ch_low = donchian_high.iloc[-1], donchian_low.iloc[-1]
    if pd.isna(ch_high) or pd.isna(ch_low):
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    candle_range = last["high"] - last["low"]
    confirmed = candle_range >= atr_now * atr_mult

    long_score = short_score = 0.0
    note = "no Donchian breakout"
    if last["close"] > ch_high:
        breakout_size = (last["close"] - ch_high) / atr_now
        base = 45 + breakout_size * 55
        long_score = _clip(base if confirmed else base * 0.6)
        note = f"broke {donchian_period}-bar Donchian high {ch_high:.2f}" + ("" if confirmed else " (weak/unconfirmed range)")
    elif last["close"] < ch_low:
        breakout_size = (ch_low - last["close"]) / atr_now
        base = 45 + breakout_size * 55
        short_score = _clip(base if confirmed else base * 0.6)
        note = f"broke {donchian_period}-bar Donchian low {ch_low:.2f}" + ("" if confirmed else " (weak/unconfirmed range)")
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 19. Order Flow (DOM) ----------------------------
def score_order_flow_dom(data):
    """Approximates Order Flow / Footprint analysis using a REAL MT5 Depth of
    Market (DOM / Level2) snapshot — i.e. genuine resting bid vs ask volume at
    the current price, captured fresh every scan via mt5.market_book_get().

    This is NOT a historical footprint chart (MT5's Python API has no way to
    pull historical per-tick bid/ask volume the way paid 'Order Flow Trader'
    footprint indicators do) and it is NOT broker sentiment data (that's what
    FXSSI Order Book actually shows, sourced from FXSSI's own service — not
    obtainable through the standard MT5 API at all). What this function CAN
    do is read your own broker's live order book, when the symbol supports
    it, and turn the bid/ask volume imbalance into a long/short pressure
    score: heavier resting bids than asks = buy-side pressure/support nearby
    (long bias), heavier asks = sell-side pressure/resistance (short bias).

    Many brokers/symbols don't expose DOM at all — in that case data["dom"]
    will be None (see get_dom_snapshot() in xauusd_mt5_strategy.py) and this
    strategy scores 0/0 rather than erroring, so it simply contributes
    nothing to the confluence vote instead of breaking the scan."""
    dom = data.get("dom")
    if not dom or (not dom.get("bids") and not dom.get("asks")):
        return {"long": 0.0, "short": 0.0,
                "note": "DOM unavailable (broker/symbol may not support Level2)"}

    bid_vol = dom.get("bid_volume", 0.0)
    ask_vol = dom.get("ask_volume", 0.0)
    total = bid_vol + ask_vol
    if total <= 0:
        return {"long": 0.0, "short": 0.0, "note": "DOM empty (no volume on either side)"}

    imbalance = (bid_vol - ask_vol) / total  # +1.0 = all bids, -1.0 = all asks
    # Linear 0-100 scale: 100% bid imbalance = 100 long, 100% ask = 100 short.
    # Previous formula (50 ± imbalance*100) hit 100 at only 50% imbalance,
    # destroying the gradient between moderate and extreme order flow.
    long_score = _clip(imbalance * 100) if imbalance > 0 else 0.0
    short_score = _clip(-imbalance * 100) if imbalance < 0 else 0.0

    detail = f"bid_vol={bid_vol:.0f} ask_vol={ask_vol:.0f} imbalance={imbalance * 100:+.0f}%"
    if imbalance > 0.15:
        note = f"DOM bid-heavy (buy pressure) — {detail}"
    elif imbalance < -0.15:
        note = f"DOM ask-heavy (sell pressure) — {detail}"
    else:
        note = f"DOM roughly balanced — {detail}"
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 20. Macro Bias (Big Data) ----------------------------
def score_macro_bias(data):
    """Institutional "Gold Decision Matrix" — a WEIGHTED big-data bias model,
    per the user's reference doc (USA new trade.docx), which assigns each
    factor an explicit weight instead of counting them equally:

        Factor              Weight   Bullish Gold          Bearish Gold
        DXY                  30%     DXY falling            DXY rising
        US10Y Yield          25%     Yield falling          Yield rising
        Fed Expectation      20%     Cut priced in          Hawkish/hold priced in
        ETF Flow (GLD)       10%     Inflow                 Outflow
        COT Position         10%     Net Long rising        Net Long falling
        COMEX Registered      5%     Registered thinning    Registered building

    "Fed Expectation" has no free, scrapeable CME FedWatch API (real
    FedWatch odds come from a JS-rendered page with no public data feed) so
    it's approximated from the US 2-Year Treasury yield trend — see
    macro_data.fetch_fed_expectation()'s docstring for why this proxy is
    documented honestly as an approximation, not the real FedWatch %.

    Bull Score = sum(weight of bullish factors) / sum(weight of AVAILABLE
    factors) * 100 — unavailable factors (most often ETF Flow, see
    macro_data.py's module docstring) are dropped from BOTH the numerator
    and denominator, so a missing source never silently drags the score
    toward bearish. The doc's probability bucket table is reproduced as the
    `note`'s label:
        0-25   -> Bearish 70-90%      55-75  -> Bullish 60-75%
        25-45  -> Bearish 55-70%      75-100 -> Bullish 75-90%
        45-55  -> Sideway (the doc's own guidance: don't trend-follow here)

    Reads data["macro"] — a dict built by macro_data.get_macro_snapshot() and
    wired into build_market_data() in xauusd_mt5_strategy.py. This data
    updates at most once every few hours (COT is weekly), NOT every scan —
    see CACHE_TTL in macro_data.py. If data["macro"] is missing entirely
    (e.g. macro_data.py not wired up, or first run before any fetch
    succeeded), this scores a neutral 0/0 exactly like the DOM strategy does
    when DOM is unsupported — it never blocks or errors the other 19.

    Also folds in a soft pre-news caution: if a High-impact USD event (NFP,
    CPI, FOMC, PCE, GDP, etc.) is landing within the next 60 minutes, both
    sides are damped by 40% — funds typically stand aside right before these
    prints regardless of which way the rest of the matrix leans."""
    macro = data.get("macro")
    if not macro:
        return {"long": 0.0, "short": 0.0,
                "note": "macro data unavailable (macro_data.py not fetched yet)"}

    macro_result = _macro_bull_score(macro)
    if macro_result is None:
        return {"long": 0.0, "short": 0.0,
                "note": "no macro sources available yet (all fetches pending/failed)"}
    bull_score, detail = macro_result

    if bull_score < 25:
        prob_label = "Bearish 70-90%"
    elif bull_score < 45:
        prob_label = "Bearish 55-70%"
    elif bull_score <= 55:
        prob_label = "Sideway"
    elif bull_score <= 75:
        prob_label = "Bullish 60-75%"
    else:
        prob_label = "Bullish 75-90%"

    long_score = bull_score
    short_score = _clip(100 - bull_score)

    note = f"Gold Decision Matrix: Bull Score {bull_score:.0f}/100 ({prob_label}) — {detail}"

    try:
        from macro_data import upcoming_high_impact_events
        soon = upcoming_high_impact_events(macro.get("calendar"), within_minutes=60)
        if soon:
            long_score *= 0.6
            short_score *= 0.6
            titles = ", ".join(e.get("title", "?") for e in soon[:3])
            note += f" | CAUTION: high-impact news <60min ({titles}) — damped 40%"
    except Exception:
        pass  # never let the news-gate sub-feature break the main matrix score

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- Big Data baseline helper (shared by macro_bias + scalps) --
def _macro_bull_score(macro):
    """Computes the same weighted institutional 'Gold Decision Matrix' bull
    score (0-100, >50 = bullish Gold) that score_macro_bias() displays as its
    own strategy, but as a reusable helper — DXY 30%, US10Y Yield 25%, Fed
    Expectation 20%, ETF Flow 10%, COT Net Long 10%, COMEX Registered 5%.
    Returns None if macro data hasn't been fetched yet or no factor is
    available, so callers can no-op cleanly instead of treating "no data" as
    "bearish"."""
    if not macro:
        return None
    factors = []
    dxy = macro.get("dxy")
    if dxy and dxy.get("change") is not None:
        factors.append(("DXY", 30, dxy["change"] < 0))
    yld = macro.get("yield10y")
    if yld and yld.get("change") is not None:
        factors.append(("US10Y Yield", 25, yld["change"] < 0))
    fed = macro.get("fed_expectation")
    if fed and fed.get("change") is not None:
        factors.append(("Fed Expectation (2Y proxy)", 20, fed["change"] < 0))
    etf = macro.get("etf_flow")
    if etf and etf.get("change_tonnes") is not None:
        factors.append(("ETF Flow (GLD)", 10, etf["change_tonnes"] > 0))
    cot = macro.get("cot")
    if cot and cot.get("managed_money_net_long_change") is not None:
        factors.append(("COT Net Long", 10, cot["managed_money_net_long_change"] > 0))
    comex = macro.get("comex")
    if comex and comex.get("registered_oz") is not None and comex.get("eligible_oz"):
        ratio = comex["registered_oz"] / max(comex["eligible_oz"], 1.0)
        factors.append(("COMEX Registered", 5, ratio < 0.5))
    if not factors:
        return None
    total_weight = sum(w for _, w, _ in factors)
    bull_weight = sum(w for _, w, ok in factors if ok)
    bull_score = _clip((bull_weight / total_weight) * 100)
    detail = ", ".join(f"{name}({w}%)={'bull' if ok else 'bear'}" for name, w, ok in factors)
    return bull_score, detail


def _macro_alignment_multiplier(bull_score, side):
    """side: 'long' or 'short'. Scales a scalp signal by how well it agrees
    with the institutional Big Data bias. A scalp that runs WITH a clearly
    leaning macro tide is sized up to +20%; one running AGAINST a clearly
    leaning tide is cut by up to 40% — the price-action trigger is still
    what fires the trade, this only adjusts conviction. bull_score is the
    0-100 value from _macro_bull_score() (>50 = bullish Gold)."""
    if side == "long":
        if bull_score >= 60:
            return 1.2
        if bull_score >= 50:
            return 1.0
        if bull_score >= 35:
            return 0.8
        return 0.6
    else:
        if bull_score <= 40:
            return 1.2
        if bull_score <= 50:
            return 1.0
        if bull_score <= 65:
            return 0.8
        return 0.6


def _apply_macro_baseline(data, long_score, short_score, note):
    """Shared Big-Data baseline filter for all 4 scalping strategies
    (#21-24): scales the scalp's long/short score using _macro_bull_score()
    / _macro_alignment_multiplier() above. If macro data hasn't been fetched
    yet (data["macro"] missing/empty), this is a no-op — the scalp scores
    stand on their own price-action logic exactly as before, so a build
    without macro_data.py wired up still works."""
    macro_result = _macro_bull_score(data.get("macro"))
    if macro_result is None:
        return long_score, short_score, note
    bull_score, _detail = macro_result
    if long_score > 0:
        long_score = _clip(long_score * _macro_alignment_multiplier(bull_score, "long"))
    if short_score > 0:
        short_score = _clip(short_score * _macro_alignment_multiplier(bull_score, "short"))
    if long_score > 0 or short_score > 0:
        note = f"{note} | Big Data baseline {bull_score:.0f}/100 (DXY/Yield/Fed/ETF/COT/COMEX)"
    return long_score, short_score, note


# ----------------------------- 21. London Open Liquidity Sweep (Scalping) -------
def score_scalp_london_sweep(data, london_start=(10, 0), london_end=(14, 0),
                              asian_start=(4, 0), asian_end=(10, 0)):
    """Scalping strategy #1 (historically ~55-65% win rate per user research).
    Active only during the London Open window on BROKER TIME (UTC+3).
    Builds the Asian session's high/low from M5 bars, then looks for a
    liquidity grab: a wick piercing that Asian high/low which closes back
    inside the range (a fake breakout / stop hunt). The EMA20 vs EMA50
    relationship on M5 is a hard directional filter — EMA20 above EMA50
    allows LONG only, EMA20 below EMA50 allows SHORT only.

    Session defaults corrected from Bangkok time (UTC+7) to broker UTC+3:
      London window: BKK 14:00-18:00 → broker 10:00-14:00 (07:00-11:00 UTC)
      Asian range:   BKK 07:00-12:00 → broker 04:00-10:00 (01:00-07:00 UTC)

    Suggested risk: SL $5-10, TP $10-20 (min R:R 1:2)."""
    df = data.get("m5")
    now = data["now"]
    if df is None or len(df) < 30:
        return {"long": 0.0, "short": 0.0,
                "note": "M5 data not available — add 'm5' to build_market_data()"}

    sess_start = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{london_start[0]:02d}:{london_start[1]:02d}").time())
    sess_end = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{london_end[0]:02d}:{london_end[1]:02d}").time())
    if not (sess_start <= now_ts(now) <= sess_end):
        return {"long": 0.0, "short": 0.0, "note": "outside London Open scalping window"}

    asia_start_ts = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{asian_start[0]:02d}:{asian_start[1]:02d}").time())
    asia_end_ts = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{asian_end[0]:02d}:{asian_end[1]:02d}").time())
    asia = df[(df["time"] >= asia_start_ts) & (df["time"] < asia_end_ts)]
    if asia.empty:
        return {"long": 0.0, "short": 0.0, "note": "Asian session range not available yet"}

    asia_high, asia_low = asia["high"].max(), asia["low"].min()
    atr_now = df["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    last = df.iloc[-1]
    ema20_now, ema50_now = df["ema20"].iloc[-1], df["ema50"].iloc[-1]
    long_allowed = ema20_now > ema50_now
    short_allowed = ema20_now < ema50_now

    long_score = short_score = 0.0
    note = "no liquidity sweep at Asian range yet"

    if last["low"] < asia_low and last["close"] > asia_low and long_allowed:
        pierce = (asia_low - last["low"]) / atr_now
        long_score = _clip(45 + pierce * 100)
        note = f"swept Asian low {asia_low:.2f}, reclaimed — EMA20>EMA50 confirms long"
    elif last["low"] < asia_low and last["close"] > asia_low and not long_allowed:
        note = f"swept Asian low {asia_low:.2f} but EMA20<EMA50 — long blocked by direction filter"

    if last["high"] > asia_high and last["close"] < asia_high and short_allowed:
        s = _clip(45 + ((last["high"] - asia_high) / atr_now) * 100)
        if s > short_score:
            short_score = s
            note = f"swept Asian high {asia_high:.2f}, rejected — EMA20<EMA50 confirms short"
    elif last["high"] > asia_high and last["close"] < asia_high and not short_allowed:
        note = f"swept Asian high {asia_high:.2f} but EMA20>EMA50 — short blocked by direction filter"

    long_score, short_score, note = _apply_macro_baseline(data, long_score, short_score, note)
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 22. EMA Pullback Scalping -------------------------
def score_scalp_ema_pullback(data):
    """Scalping strategy #2 (historically ~60-70% win rate, best in strong
    trends only, per user research). M1 timeframe. Requires the EMA20/50/200
    stack to be fully aligned (trend confirmation), a pullback that touches
    EMA20, and an engulfing candle off that touch to trigger entry — the same
    'touch + engulf' combination the user specified, reusing the engulfing
    detector pattern from score_price_action(). Suggested risk: SL $3-5,
    TP $5-10."""
    df = data.get("m1")
    if df is None or len(df) < 60:
        return {"long": 0.0, "short": 0.0,
                "note": "M1 data not available — add 'm1' to build_market_data()"}

    df = df.tail(60).reset_index(drop=True)
    last, prev = df.iloc[-1], df.iloc[-2]
    e20, e50, e200 = last["ema20"], last["ema50"], last["ema200"]
    atr_now = df["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    bull_stack = e20 > e50 > e200
    bear_stack = e20 < e50 < e200
    touched_ema20 = (last["low"] <= e20 + atr_now * 0.25) and (last["high"] >= e20 - atr_now * 0.25)

    long_score = short_score = 0.0
    note = "no EMA stack + pullback setup"
    if not (bull_stack or bear_stack):
        return {"long": 0.0, "short": 0.0, "note": "EMA20/50/200 not stacked — no trend, skip scalp"}

    body = abs(last["close"] - last["open"])
    prev_body_low, prev_body_high = min(prev["open"], prev["close"]), max(prev["open"], prev["close"])
    cur_bullish = last["close"] > last["open"]
    cur_bearish = last["close"] < last["open"]
    bullish_engulf = (cur_bullish and prev["close"] < prev["open"]
                       and last["open"] <= prev_body_low and last["close"] >= prev_body_high)
    bearish_engulf = (cur_bearish and prev["close"] > prev["open"]
                      and last["open"] >= prev_body_high and last["close"] <= prev_body_low)

    if bull_stack and touched_ema20 and bullish_engulf:
        stack_strength = _clip((e20 - e200) / atr_now * 20)
        long_score = _clip(55 + stack_strength + (body / max(atr_now, 1e-6)) * 10)
        note = "M1 EMA20>EMA50>EMA200 stack + pullback to EMA20 + bullish engulfing"
    if bear_stack and touched_ema20 and bearish_engulf:
        stack_strength = _clip((e200 - e20) / atr_now * 20)
        s = _clip(55 + stack_strength + (body / max(atr_now, 1e-6)) * 10)
        if s > short_score:
            short_score = s
            note = "M1 EMA20<EMA50<EMA200 stack + pullback to EMA20 + bearish engulfing"

    long_score, short_score, note = _apply_macro_baseline(data, long_score, short_score, note)
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 23. NY Session Breakout (Scalping) ---------------
def score_scalp_ny_orb(data, session_start=(19, 30), session_end=(23, 0)):
    """Scalping strategy #3 per user research. Active Bangkok 19:30-23:00
    (NY session). Uses the first 15 minutes as the opening range (3 x M5
    bars) — a break above/below that range scores the breakout direction.
    Best suited for USD high-impact news days (CPI/NFP/FOMC): if
    macro_data.upcoming_high_impact_events() shows one landing soon, the
    breakout score gets a confidence bonus instead of the damping
    score_macro_bias() applies (a clean ORB break *during* a news spike is
    the textbook setup for this strategy, not a reason to stand aside).
    Suggested TP $10-30."""
    df = data.get("m5")
    now = data["now"]
    if df is None or len(df) < 10:
        return {"long": 0.0, "short": 0.0,
                "note": "M5 data not available — add 'm5' to build_market_data()"}

    sess_start = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{session_start[0]:02d}:{session_start[1]:02d}").time())
    sess_end = pd.Timestamp.combine(now.date(), pd.Timestamp(f"{session_end[0]:02d}:{session_end[1]:02d}").time())
    if not (sess_start <= now_ts(now) <= sess_end):
        return {"long": 0.0, "short": 0.0, "note": "outside NY session scalping window"}

    range_end = sess_start + pd.Timedelta(minutes=15)
    opening = df[(df["time"] >= sess_start) & (df["time"] < range_end)]
    after = df[df["time"] >= range_end]
    if opening.empty or after.empty:
        return {"long": 0.0, "short": 0.0, "note": "15-min NY opening range still forming"}

    rng_high, rng_low = opening["high"].max(), opening["low"].min()
    rng_size = max(rng_high - rng_low, 1e-6)
    last = after.iloc[-1]
    body = abs(last["close"] - last["open"])

    news_bonus = 1.0
    news_note = ""
    macro = data.get("macro")
    if macro:
        try:
            from macro_data import upcoming_high_impact_events
            soon = upcoming_high_impact_events(macro.get("calendar"), within_minutes=90)
            if soon:
                news_bonus = 1.25
                titles = ", ".join(e.get("title", "?") for e in soon[:2])
                news_note = f" | high-impact news window ({titles}) — best-fit setup for this strategy"
        except Exception:
            pass

    long_score = short_score = 0.0
    note = "no NY opening-range breakout yet"
    if len(after) - 1 > 10:
        return {"long": 0.0, "short": 0.0, "note": "too far past NY opening-range window"}

    if last["close"] > rng_high:
        breakout_size = (last["close"] - rng_high) / rng_size
        momentum = body / rng_size
        long_score = _clip((50 + breakout_size * 50 + momentum * 20) * news_bonus)
        note = f"NY ORB breakout above {rng_high:.2f}{news_note}"
    elif last["close"] < rng_low:
        breakout_size = (rng_low - last["close"]) / rng_size
        momentum = body / rng_size
        short_score = _clip((50 + breakout_size * 50 + momentum * 20) * news_bonus)
        note = f"NY ORB breakdown below {rng_low:.2f}{news_note}"

    long_score, short_score, note = _apply_macro_baseline(data, long_score, short_score, note)
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 24. EMA20+EMA50+Liquidity Sweep Combo (★ Recommended) --
def score_scalp_combo_sweep(data):
    """The user's TOP-recommended scalping setup: combines an H1 trend
    filter, an M5 EMA20/EMA50 directional filter, a liquidity sweep of a
    recent M5 swing level, and a reclaim back through EMA20 — all four must
    line up, which is why this scores higher (and more selectively) than the
    standalone London-sweep strategy above.
        LONG:  H1 uptrend (close>ema50>ema200) AND M5 ema20>ema50 AND
               price sweeps a prior M5 swing LOW AND reclaims back above M5 ema20.
        SHORT: mirror conditions (H1 downtrend, M5 ema20<ema50, sweep of a
               prior swing HIGH, reclaim back below M5 ema20).
    Risk management (apply at the EA/account level, not per-signal): risk
    no more than 0.5-1% per trade (RISK_PER_TRADE), always use a stop loss,
    cap to ~3 scalp entries per session (MAX_DAILY_TRADES), and stop trading
    for the day after 2 consecutive losers (MAX_CONSECUTIVE_LOSSES=2)."""
    h1 = data.get("h1")
    m5 = data.get("m5")
    if h1 is None or m5 is None or len(h1) < 10 or len(m5) < 30:
        return {"long": 0.0, "short": 0.0,
                "note": "H1 and/or M5 data not available — add 'm5' to build_market_data()"}

    h1_uptrend = h1["close"].iloc[-1] > h1["ema50"].iloc[-1] > h1["ema200"].iloc[-1]
    h1_downtrend = h1["close"].iloc[-1] < h1["ema50"].iloc[-1] < h1["ema200"].iloc[-1]

    df = m5.tail(60).reset_index(drop=True)
    ema20_now, ema50_now = df["ema20"].iloc[-1], df["ema50"].iloc[-1]
    m5_bull_filter = ema20_now > ema50_now
    m5_bear_filter = ema20_now < ema50_now

    atr_now = df["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    highs, lows = _swing_points(df, lookback=60, order=3)
    last = df.iloc[-1]
    long_score = short_score = 0.0
    note = "no aligned combo setup (need H1 trend + M5 EMA filter + sweep + reclaim)"

    recent_lows = [l for l in lows if l[0] < len(df) - 1]
    if recent_lows and h1_uptrend and m5_bull_filter:
        level = min(l[1] for l in recent_lows[-3:])
        if last["low"] < level and last["close"] > ema20_now:
            pierce = (level - last["low"]) / atr_now
            long_score = _clip(60 + pierce * 80)
            note = (f"H1 uptrend + M5 EMA20>EMA50 + swept low {level:.2f} "
                    f"+ reclaimed above M5 EMA20 — full combo aligned")

    recent_highs = [h for h in highs if h[0] < len(df) - 1]
    if recent_highs and h1_downtrend and m5_bear_filter:
        level = max(h[1] for h in recent_highs[-3:])
        if last["high"] > level and last["close"] < ema20_now:
            s = _clip(60 + ((last["high"] - level) / atr_now) * 80)
            if s > short_score:
                short_score = s
                note = (f"H1 downtrend + M5 EMA20<EMA50 + swept high {level:.2f} "
                        f"+ reclaimed below M5 EMA20 — full combo aligned")

    long_score, short_score, note = _apply_macro_baseline(data, long_score, short_score, note)
    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- 25. Myfxbook Retail Sentiment ------------------
def score_myfxbook_sentiment(data):
    """25th strategy — Myfxbook public Community Outlook (retail long/short %
    for XAUUSD). Reads data["macro"]["myfxbook_sentiment"]. Degrades gracefully
    to 0/0 if Myfxbook credentials aren't configured or the fetch failed.

    Two modes via data["myfxbook_contrarian"] (default True):
      contrarian=True  — fade the crowd (default, recommended)
      contrarian=False — vote with the crowd

    Weight (0.8) is intentionally below macro_bias (1.2) — retail sentiment
    from one broker is a secondary confirming vote, not a primary signal.
    """
    macro = data.get("macro")
    if macro is None:
        return {"long": 0.0, "short": 0.0, "note": "macro data unavailable"}
    sentiment = macro.get("myfxbook_sentiment")
    if not sentiment:
        return {"long": 0.0, "short": 0.0,
                "note": "Myfxbook sentiment unavailable (not configured, or fetch pending/failed)"}
    long_pct = float(sentiment.get("long_percentage") or 0)
    short_pct = float(sentiment.get("short_percentage") or 0)
    if long_pct == 0 and short_pct == 0:
        return {"long": 0.0, "short": 0.0,
                "note": "Myfxbook sentiment returned no data for this symbol"}
    contrarian = data.get("myfxbook_contrarian", True)
    if contrarian:
        long_score, short_score = short_pct, long_pct
        mode_label = "contrarian — fading the crowd"
    else:
        long_score, short_score = long_pct, short_pct
        mode_label = "trend-following — with the crowd"
    note = (f"Myfxbook retail sentiment: {long_pct:.0f}% long / {short_pct:.0f}% short "
            f"({mode_label})")
    return {"long": _clip(long_score), "short": _clip(short_score), "note": note}


# ----------------------------- 26. Climax Reversal at S/R -----------------------
def score_climax_reversal_sr(data, move_lookback=8, atr_mult_extreme=2.5,
                              sr_lookback=80, proximity_atr=0.4):
    """26th strategy — catches a sharp reversal right after an extreme,
    exhausted directional move slams into a support/resistance zone: a
    strong multi-bar push in one direction, arriving at a fresh price
    extreme or a prior swing level, then a sharp rejection candle (pin
    bar / engulfing) snapping price back the other way.

    Two gates must BOTH be true before this strategy votes at all:
      1. EXHAUSTION: net move over the last move_lookback H1 bars must be
         at least atr_mult_extreme x ATR(14) in one direction.
      2. AT A LEVEL: the bar's low/high is either a fresh sr_lookback-bar
         extreme OR within proximity_atr x ATR of an existing swing S/R.

    Votes on the last closed H1 bar's close — immediate entry signal,
    no separate breakout/pending-order logic."""
    df = data["h1"].tail(max(sr_lookback, move_lookback) + 10).reset_index(drop=True)
    if len(df) < move_lookback + 15:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    atr_now = data["h1"]["atr14"].iloc[-1]
    atr_now = atr_now if atr_now and not pd.isna(atr_now) else (df["high"] - df["low"]).tail(20).mean()
    atr_now = max(atr_now, 1e-6)

    last, prev = df.iloc[-1], df.iloc[-2]

    # Gate 1: exhaustion — net move over move_lookback bars vs ATR
    ref_close = df["close"].iloc[-1 - move_lookback]
    net_move = last["close"] - ref_close
    extreme_strength = abs(net_move) / atr_now
    if extreme_strength < atr_mult_extreme:
        return {"long": 0.0, "short": 0.0,
                "note": f"no exhausted move yet ({extreme_strength:.1f}x ATR < {atr_mult_extreme:.1f}x required)"}
    move_was_down = net_move < 0
    move_was_up   = net_move > 0

    # Gate 2: at a level — fresh N-bar extreme OR near a known swing point
    recent = df.tail(sr_lookback)
    fresh_low  = last["low"]  <= recent["low"].min()  + 1e-9
    fresh_high = last["high"] >= recent["high"].max() - 1e-9
    highs, lows = _swing_points(df, lookback=sr_lookback, order=3)
    near_swing_low  = any(abs(last["low"]  - l[1]) <= atr_now * proximity_atr for l in lows[-5:])
    near_swing_high = any(abs(last["high"] - h[1]) <= atr_now * proximity_atr for h in highs[-5:])
    at_support    = fresh_low  or near_swing_low
    at_resistance = fresh_high or near_swing_high

    rng  = max(last["high"] - last["low"], 1e-6)
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    prev_body_low  = min(prev["open"], prev["close"])
    prev_body_high = max(prev["open"], prev["close"])
    cur_bullish  = last["close"] > last["open"]
    cur_bearish  = last["close"] < last["open"]
    prev_bearish = prev["close"] < prev["open"]
    prev_bullish = prev["close"] > prev["open"]

    long_score = short_score = 0.0
    note = f"exhausted move ({extreme_strength:.1f}x ATR) but no rejection candle yet at the level"

    if move_was_down and at_support:
        is_pin    = lower_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = (cur_bullish and prev_bearish
                     and last["open"] <= prev_body_low and last["close"] >= prev_body_high)
        if is_pin or is_engulf:
            level_tag = "fresh climax low" if fresh_low else "key support level"
            shape     = "bullish pin bar" if is_pin else "bullish engulfing"
            quality   = (lower_wick / rng) if is_pin else (body / rng)
            long_score = _clip(55 + extreme_strength * 6 + quality * 35)
            note = f"{shape} after {extreme_strength:.1f}x-ATR exhausted sell-off at {level_tag}"

    if move_was_up and at_resistance:
        is_pin    = upper_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = (cur_bearish and prev_bullish
                     and last["open"] >= prev_body_high and last["close"] <= prev_body_low)
        if is_pin or is_engulf:
            level_tag = "fresh climax high" if fresh_high else "key resistance level"
            shape     = "bearish pin bar" if is_pin else "bearish engulfing"
            quality   = (upper_wick / rng) if is_pin else (body / rng)
            short_score = _clip(55 + extreme_strength * 6 + quality * 35)
            note = f"{shape} after {extreme_strength:.1f}x-ATR exhausted rally at {level_tag}"

    return {"long": long_score, "short": short_score, "note": note}


# ----------------------------- MTR regime helpers ------------------------------
def _efficiency_ratio(series, period=20):
    """Kaufman Efficiency Ratio: net directional move / sum of absolute moves.
    Near 1 = strongly trending; near 0 = choppy/ranging."""
    if len(series) < period + 2:
        return 0.5
    direction = abs(float(series.iloc[-1]) - float(series.iloc[-period - 1]))
    path = float(series.diff().abs().iloc[-period:].sum())
    if path < 1e-10:
        return 0.5
    return float(np.clip(direction / path, 0.0, 1.0))


def _wilder_adx(df, period=14):
    """Wilder's ADX. Returns (adx, di_plus, di_minus) as pandas Series."""
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    up = (h - h.shift(1)).fillna(0.0)
    dn = (l.shift(1) - l).fillna(0.0)
    dm_p = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    dm_m = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    alpha = 1.0 / period
    atr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    dip   = dm_p.ewm(alpha=alpha, adjust=False).mean()
    dim   = dm_m.ewm(alpha=alpha, adjust=False).mean()
    di_p  = (dip / atr_w.replace(0, np.nan)) * 100
    di_m  = (dim / atr_w.replace(0, np.nan)) * 100
    dx    = ((di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)) * 100
    adx   = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx, di_p, di_m


def _variance_ratio(series, k=5):
    """Lo-MacKinlay variance ratio. VR < 1 = mean-reversion; VR > 1 = persistence."""
    if len(series) < k + 10:
        return 1.0
    log_ret = np.log(series / series.shift(1)).dropna()
    if len(log_ret) < k + 5:
        return 1.0
    var_1 = float(log_ret.rolling(2).var().iloc[-1])
    log_ret_k = np.log(series / series.shift(k)).dropna()
    var_k = float(log_ret_k.rolling(k + 1).var().iloc[-1])
    if pd.isna(var_1) or var_1 < 1e-20 or pd.isna(var_k):
        return 1.0
    return float(np.clip(var_k / (k * var_1), 0.0, 5.0))


# ----------------------------- 27. MTR Range Regime ---------------------------
def score_mtr_range_regime(data):
    """27th strategy — MTR-inspired quantitative range-regime detector. Votes
    Long AND Short symmetrically when the H1 market is measurably in a
    mean-reversion (ranging) regime. Scores 0/0 in trending or danger regimes.

    Five components (weights from MTR RegimeScore.mqh):
      30% low Efficiency Ratio (ER < 0.35 → weak directional persistence)
      25% ADX flat/falling below 22 (no strong trend)
      20% price inside the 55-bar H1 Donchian band (range-bound)
      15% Variance Ratio(5) < 0.98 (mean-reversion tendency)
      10% ATR not spiking (< 1.3× 50-bar baseline = normal volatility)

    Returns 0/0 if ATR shock > 1.5× baseline (danger/volatility spike)."""
    df = data.get("h1")
    if df is None or len(df) < 65:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data for MTR range regime"}

    close = df["close"]

    # ATR shock — danger gate
    atr_now = float(df["atr14"].iloc[-1]) if not pd.isna(df["atr14"].iloc[-1]) else 0.0
    atr_vals = df["atr14"].dropna()
    atr_baseline = float(atr_vals.iloc[-51:-1].mean()) if len(atr_vals) >= 52 else atr_now
    atr_shock = atr_now / atr_baseline if atr_baseline > 0 else 1.0
    if atr_shock > 1.5:
        return {"long": 0.0, "short": 0.0,
                "note": f"MTR danger: ATR spike {atr_shock:.2f}x — range regime suspended"}

    # Component 1: Efficiency Ratio (low → choppy/range)
    er = _efficiency_ratio(close, 20)
    low_er_score = 1.0 if er < 0.28 else (0.5 if er < 0.38 else 0.0)

    # Component 2: ADX flat/falling (no trend)
    try:
        adx, _, _ = _wilder_adx(df.tail(60).reset_index(drop=True), 14)
        adx_now   = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 25.0
        adx_slope = float(adx.iloc[-1] - adx.iloc[-5]) if len(adx) >= 5 else 0.0
        adx_flat_score = 1.0 if (adx_now < 20 and adx_slope <= 0) else (0.5 if adx_now < 23 else 0.0)
    except Exception:
        adx_now, adx_flat_score = 25.0, 0.0

    # Component 3: price inside Donchian 55-period H1
    don_p = min(55, len(df) - 1)
    don_h = float(df["high"].iloc[-don_p:].max())
    don_l = float(df["low"].iloc[-don_p:].min())
    mid   = float((close.iloc[-1] + close.iloc[-2]) / 2)
    if don_h > don_l:
        pos = (mid - don_l) / (don_h - don_l)
        inside_score = float(np.clip(1.0 - abs(pos - 0.5) * 2, 0.0, 1.0))
    else:
        inside_score = 0.5

    # Component 4: Variance Ratio < 0.98 (mean-reversion)
    vr5 = _variance_ratio(close, 5)
    vr_mr_score = 1.0 if vr5 < 0.90 else (0.5 if vr5 < 0.98 else 0.0)

    # Component 5: normal volatility
    normal_vol_score = 1.0 if atr_shock < 1.1 else (0.5 if atr_shock < 1.3 else 0.0)

    range_score = (0.30 * low_er_score + 0.25 * adx_flat_score + 0.20 * inside_score
                   + 0.15 * vr_mr_score + 0.10 * normal_vol_score)

    if range_score < 0.45:
        return {"long": 0.0, "short": 0.0,
                "note": f"MTR range score {range_score:.2f} below threshold — not a range market"}

    score_pct = _clip(range_score * 100)
    note = (f"MTR range regime: score={range_score:.2f} ER={er:.2f} ADX={adx_now:.1f} "
            f"VR5={vr5:.2f} in_band={inside_score:.2f} atr_shock={atr_shock:.2f}")
    return {"long": score_pct, "short": score_pct, "note": note}


# ----------------------------- 28. MTR Trend Regime ---------------------------
def score_mtr_trend_regime(data):
    """28th strategy — MTR-inspired quantitative trend-regime detector. Votes
    directionally (Long or Short) when the H1 market is in a confirmed trend.
    Complements score_mtr_range_regime — the two are inversely correlated.

    Four components (weights from MTR RegimeScore.mqh):
      30% high Efficiency Ratio (ER > 0.55 → strong directional persistence)
      25% ADX rising above 22 (strengthening trend)
      25% price outside 55-bar Donchian (breakout confirmed)
      20% EMA50 slope alignment (confirms direction)

    Direction: votes Long when EMA50 rising + DI+ > DI−; Short when falling."""
    df = data.get("h1")
    if df is None or len(df) < 65:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data for MTR trend regime"}

    close = df["close"]

    # Component 1: high ER (trending)
    er = _efficiency_ratio(close, 20)
    high_er_score = 1.0 if er > 0.62 else (0.5 if er > 0.50 else 0.0)

    # Component 2: ADX rising and strong
    try:
        adx, di_p, di_m = _wilder_adx(df.tail(60).reset_index(drop=True), 14)
        adx_now   = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 0.0
        adx_slope = float(adx.iloc[-1] - adx.iloc[-5]) if len(adx) >= 5 else 0.0
        adx_rising_score = 1.0 if (adx_now > 25 and adx_slope > 0) else (0.5 if adx_now > 22 else 0.0)
        dip_now = float(di_p.iloc[-1]) if not pd.isna(di_p.iloc[-1]) else 50.0
        dim_now = float(di_m.iloc[-1]) if not pd.isna(di_m.iloc[-1]) else 50.0
        adx_bullish = dip_now > dim_now
    except Exception:
        adx_now, adx_rising_score, adx_bullish = 0.0, 0.0, True

    # Component 3: Donchian breakout
    don_p     = min(55, len(df) - 1)
    don_h     = float(df["high"].iloc[-don_p:].max())
    don_l     = float(df["low"].iloc[-don_p:].min())
    last_c    = float(close.iloc[-1])
    break_up   = last_c >= don_h * 0.998
    break_down = last_c <= don_l * 1.002
    breakout_score = 1.0 if (break_up or break_down) else 0.0

    # Component 4: EMA50 slope
    ema50 = df["ema50"]
    if len(ema50) >= 5 and not pd.isna(ema50.iloc[-1]) and not pd.isna(ema50.iloc[-5]):
        ema50_slope = float(ema50.iloc[-1] - ema50.iloc[-5])
        ema_bullish = ema50_slope > 0
        atr_now = float(df["atr14"].iloc[-1]) if not pd.isna(df["atr14"].iloc[-1]) else 1.0
        slope_score = float(np.clip(abs(ema50_slope) / max(atr_now, 1e-10), 0.0, 1.0))
    else:
        ema50_slope, ema_bullish, slope_score = 0.0, True, 0.0

    # Direction consensus (3 signals)
    bull_votes  = sum([adx_bullish, break_up,   ema_bullish])
    bear_votes  = sum([not adx_bullish, break_down, not ema_bullish])
    if bull_votes == bear_votes:
        return {"long": 0.0, "short": 0.0, "note": "MTR trend: direction signals split — no vote"}

    direction = "long" if bull_votes > bear_votes else "short"

    trend_score = (0.30 * high_er_score + 0.25 * adx_rising_score
                   + 0.25 * breakout_score + 0.20 * slope_score)

    if trend_score < 0.40:
        return {"long": 0.0, "short": 0.0,
                "note": f"MTR trend score {trend_score:.2f} below threshold"}

    score_pct = _clip(trend_score * 100)
    note = (f"MTR trend regime ({direction}): score={trend_score:.2f} ER={er:.2f} "
            f"ADX={adx_now:.1f} slope={ema50_slope:.2f} "
            f"breakout={'up' if break_up else 'down' if break_down else 'none'}")
    return {
        "long":  score_pct if direction == "long"  else 0.0,
        "short": score_pct if direction == "short" else 0.0,
        "note":  note,
    }


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
         reacted to many
         times" step from the course, using H4 (the highest timeframe
         already wired into this bot) in place of the course's Weekly/Daily
         charts -- no new data source required.
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


# ----------------------------- registry + aggregation ---------------------------
STRATEGY_REGISTRY = {
    "order_block": ("Order Block (ICT)", score_order_block),
    "supply_demand": ("Supply & Demand", score_supply_demand),
    "ema_cross": ("EMA Cross", score_ema_cross),
    "rsi_divergence": ("RSI Divergence", score_rsi_divergence),
    "london_breakout": ("London Breakout", score_london_breakout),
    "fibonacci": ("Fibonacci", score_fibonacci),
    "vwap_rejection": ("VWAP Rejection", score_vwap_rejection),
    "news_fade": ("News Fade", score_news_fade),
    "multi_tf_align": ("Multi-TF Align", score_multi_tf_align),
    "bos_choch": ("BOS/CHoCH", score_bos_choch),
    "liquidity_sweep": ("Liquidity Sweep", score_liquidity_sweep),
    "fair_value_gap": ("Fair Value Gap", score_fair_value_gap),
    "opening_range_breakout": ("Opening Range Breakout", score_opening_range_breakout),
    # ---- merged in from the original v1 "10 strategies" list. ema_cross,
    # rsi_divergence, fib_confluence (~= fibonacci), mtf_alignment
    # (~= multi_tf_align), and news_momentum (~= news_fade) were already
    # covered above under different names, so only the 5 genuinely new v1
    # concepts were added here to avoid duplicate/overlapping votes.
    "macd_cross": ("MACD Signal Cross", score_macd_cross),
    "bb_breakout": ("Bollinger Band Breakout", score_bb_breakout),
    "sr_breakout_retest": ("S/R Breakout + Retest", score_sr_breakout_retest),
    "price_action": ("Price Action Candlestick", score_price_action),
    "atr_donchian_breakout": ("ATR/Donchian Breakout", score_atr_donchian_breakout),
    # ---- real MT5 Depth-of-Market (Level2) order-flow approximation. Scores
    # 0/0 gracefully if the broker/symbol doesn't expose DOM data.
    "order_flow_dom": ("Order Flow (DOM)", score_order_flow_dom),
    # ---- weighted institutional "Gold Decision Matrix" (DXY 30%, US10Y
    # yield 25%, Fed Expectation 20%, ETF flow 10%, COT 10%, COMEX 5%).
    # Reads data["macro"] (see macro_data.py) — scores 0/0 gracefully if
    # that hasn't been fetched yet.
    "macro_bias": ("Macro Bias (Big Data)", score_macro_bias),
    # ---- Scalping additions (require "m1"/"m5" in the data dict — see
    # build_market_data() in xauusd_mt5_strategy.py). Score 0/0 gracefully
    # with an explanatory note if m1/m5 haven't been wired in yet.
    "scalp_london_sweep": ("Scalping: London Open Liquidity Sweep", score_scalp_london_sweep),
    "scalp_ema_pullback": ("Scalping: EMA Pullback (M1)", score_scalp_ema_pullback),
    "scalp_ny_orb": ("Scalping: NY Session Breakout", score_scalp_ny_orb),
    "scalp_combo_sweep": ("Scalping: EMA20+EMA50+Liquidity Sweep ★", score_scalp_combo_sweep),
    # ---- 25th: Myfxbook public Community Outlook (retail sentiment).
    # Reads data["macro"]["myfxbook_sentiment"] — scores 0/0 gracefully until
    # Myfxbook credentials are configured in the UI. Weight kept below macro_bias.
    "myfxbook_sentiment": ("Myfxbook Retail Sentiment", score_myfxbook_sentiment),
    # ---- 26th: extreme/exhausted directional move that slams into a fresh
    # extreme or known S/R level and snaps back with a rejection candle.
    # Needs only H1 OHLC + atr14 — no new data source required.
    "climax_reversal_sr": ("Climax Reversal at S/R ★", score_climax_reversal_sr),
    # ---- 27th: MTR-inspired quantitative range-regime detector.
    # Votes Long AND Short symmetrically when ER/ADX/VR/Donchian agree it's ranging.
    "mtr_range_regime": ("MTR Range Regime (ER+ADX+VR+Donchian)", score_mtr_range_regime),
    # ---- 28th: MTR-inspired trend-regime detector.
    # Votes directionally when ER/ADX/Donchian confirm a trend; complement to 27th.
    "mtr_trend_regime": ("MTR Trend Regime (ER+ADX+Donchian)", score_mtr_trend_regime),
    # ---- 29th: user-uploaded gold swing-trading course method -- a multi-touch
    # H4 zone (proxy for the course's Weekly/Daily key levels) plus a nested
    # M15 double-top/double-bottom reversal confirmed by a neckline break.
    # Needs only H4 + M15 OHLC + atr14, already present in every scan.
    "zone_mw_reversal": ("HTF Zone + M/W Reversal ★", score_zone_mw_reversal),
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

DEFAULT_VOTE_THRESHOLD = 50.0  # a strategy's score on a side must be >= this
                                # to count as "voting" for that side at all


def score_all(data, enabled_keys=None, weights=None, bench_check=None):
    """Runs every enabled strategy, returns:
        scores: {key: {"long":.., "short":.., "note":.., "weight":.., "benched": bool}}
        long_combined, short_combined: weighted-average score across VOTING,
            non-benched strategies only (0 if nobody voted that side)
        long_agreeing, short_agreeing: count of voting, non-benched strategies
            per side (used for the MIN_AGREEING_STRATEGIES confluence gate)

    `bench_check(key) -> bool` lets the caller (League System) zero out a
    strategy's influence without removing it from the score display.
    """
    enabled_keys = enabled_keys or list(STRATEGY_REGISTRY.keys())
    weights = weights or {}
    bench_check = bench_check or (lambda k: False)

    scores = {}
    long_weighted_sum = short_weighted_sum = 0.0
    long_weight_total = short_weight_total = 0.0
    long_agreeing = short_agreeing = 0

    for key in enabled_keys:
        meta = STRATEGY_REGISTRY.get(key)
        if meta is None:
            continue
        display, func = meta
        try:
            result = func(data)
        except Exception as exc:  # a single bad strategy must not kill the scan
            result = {"long": 0.0, "short": 0.0, "note": f"error: {exc}"}

        benched = bool(bench_check(key))
        weight = float(weights.get(key, 1.0))
        scores[key] = {
            "display": display,
            "long": round(result.get("long", 0.0), 1),
            "short": round(result.get("short", 0.0), 1),
            "note": result.get("note", ""),
            "weight": weight,
            "benched": benched,
        }

        if benched:
            continue

        if result.get("long", 0.0) >= DEFAULT_VOTE_THRESHOLD:
            long_weighted_sum += result["long"] * weight
            long_weight_total += weight
            long_agreeing += 1
        if result.get("short", 0.0) >= DEFAULT_VOTE_THRESHOLD:
            short_weighted_sum += result["short"] * weight
            short_weight_total += weight
            short_agreeing += 1

    long_combined = (long_weighted_sum / long_weight_total) if long_weight_total else 0.0
    short_combined = (short_weighted_sum / short_weight_total) if short_weight_total else 0.0

    return {
        "scores": scores,
        "long_combined": round(long_combined, 1),
        "short_combined": round(short_combined, 1),
        "long_agreeing": long_agreeing,
        "short_agreeing": short_agreeing,
    }
