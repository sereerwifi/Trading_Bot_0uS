# Prompt for Claude Code (run ON THE VPS) — sync the new 32nd strategy

Paste this into Claude Code **on the VPS**, in the live bot folder (whichever
one is currently authoritative there). This adds the 32nd confluence
strategy, **"Fibonacci Confluence S/R (Major+Minor Swing)"**, which was just
built and verified on the local working copy (`RoBotTrading man 0 V10`). It
is purely additive — no existing strategy, weight, or behavior changes.

**Before touching anything: read the VPS's own `CLAUDE.md` and compare its
"Strategies (N total)" section, its `strategies.py` `STRATEGY_REGISTRY`, and
`xauusd_mt5_strategy.py`'s `_RECOMMENDED_STRATEGY_WEIGHTS` against what's
described below.** The local copy this was built on documents 31 prior
strategies (#1-31, including Myfxbook Sentiment, Climax Reversal at S/R, MTR
Range/Trend Regime, HTF Zone + M/W Reversal, and Smart Money Sweep
Morning/Night). If the VPS's actual numbering, strategy set, or any of these
file's structure differs from that — because something else was synced or
changed there independently and not yet pulled back down — **stop and report
the discrepancy instead of guessing**; adapt the line numbers/anchors below
to match what you actually find, but do not silently overwrite or reorder
unrelated strategies.

## What this strategy does

Finds high-probability support/resistance by combining a **major swing**
(H4) Fibonacci retracement/extension table with a **minor swing** (H1) one,
per the user's MT4-screenshot level table (retracement 0/23.6/38.2/50/61.8/
78.6/88.6/100, extension 127/161.8/200/261.8/300, negative extension
-127/-161.8/-200/-261.8), and treats the price zones where a major-swing
level and a minor-swing level land within ~1 H1-ATR of each other as the
strongest S/R. Each such confluence zone gets extra confirmation points for
also sitting near an EMA20/50/EMA200/SMA50, a prior horizontal swing S/R
level, or a fitted trendline/regression-channel projection. It only votes
once price is AT the nearest confluence zone AND prints a rejection candle
there (pin bar or engulfing) — a zone alone is a level to watch, not a
signal. Needs only H4 + H1 OHLC + `atr14`/`ema20`/`ema50`/`ema200`, all
already present every scan.

It also writes to a brand-new local SQLite file, `fib_confluence_history.db`
(separate from `macro_data_history.db`), with two tables: `price_bars`
(append-only H4/H1 OHLC+indicator history, deduplicated, for future
backtesting) and `fib_confluence_history` (one row per scan — the major/
minor leg and every zone found, for after-the-fact "why did the bot think X
was resistance" audits). Both are best-effort/never-raises, same pattern as
`macro_data.py`'s history table.

## Step 0 — create the new file `fib_confluence.py`

This file does **not** exist on the VPS yet (it's a brand-new standalone
module — only `numpy`/`pandas`/stdlib, no circular-import risk). Create it
at the root of the live bot folder with this exact content:

```python
"""
Fibonacci Major/Minor Swing Confluence — Support & Resistance Engine
=====================================================================
Implements the user-supplied reference doc ("Fibonacci Level.docx" /
"Fibonacci Level (English).docx") as an actual, computable strategy instead
of a manually-drawn chart tool:

  1. Level table — same numbers as the user's MT4 screenshot
     ("FIBO LEVEL ... PLATFORM MT4"):
       retracement:        0, 23.6, 38.2, 50.0, 61.8, 78.6, 88.6, 100
       extension:          127, 161.8, 200, 261.8, 300
       negative extension: -127, -161.8, -200, -261.8
     (stored below as ratios: 0.0-1.0, 1.27-3.0, and -0.27 to -1.618 — the
     MT4 "Level" column values, not the "Description" % labels.)

  2. Major-swing + minor-swing CONFLUENCE — per the doc's worked examples:
     draw Fibonacci on the most recent major swing leg, draw it again on
     the most recent minor swing leg, and treat the price zones where a
     major-swing level and a minor-swing level land close together as the
     strongest support/resistance. This module finds that overlap
     numerically instead of "eyeballing two drawn tools on a chart."

  3. Extra confirmation layers, per the doc's 3rd usage rule ("ปกติแล้วเรา
     จะไม่ได้ใช้ Fibonacci เดี่ยวๆ... จะเอาไปใช้ผูกกับเทรนไลน์ RSI SMA/EMA
     เซ็นทรัลไลน์... ยิ่งมีตัวยืนยันมากยิ่งแม่นยำ"): each confluence zone gets
     bonus confirmation points for sitting near an EMA (already computed by
     strategies.enrich()), a prior horizontal swing high/low, a trendline
     fit through recent swing points, or a regression-channel boundary.

Data flow / where this plugs in (mirrors macro_data.py's relationship with
strategies.score_macro_bias(), see that file's module docstring):

    xauusd_mt5_strategy.build_market_data()
        -> get_fib_confluence_safe()  [in xauusd_mt5_strategy.py, try/except
                                        wrapper, same pattern as
                                        get_macro_snapshot_safe()]
        -> fib_confluence.compute_confluence(data)   (this module)
        -> result stored at data["fib_confluence"]
    strategies.score_fib_confluence_sr(data)
        -> reads data["fib_confluence"], scores 0/0 gracefully if missing/
           errored, exactly like score_macro_bias() does for data["macro"].

Database ("good database structure for record market price for use to
calculation", per the user's request):

  price_bars       — append-only local history of OHLC bars per timeframe,
                      independent of MT5's own history so the bot still has
                      a record after a crash/reinstall and so a deeper
                      lookback than one MT5 copy_rates_from_pos() call can
                      be built up over time for backtesting (see
                      strategy_simulator.py / backtest_sim.py). Deduplicated
                      on (symbol, timeframe, bar_time) so calling
                      save_price_bars() every scan never creates duplicate
                      rows for bars already seen.
  fib_confluence_history — append-only snapshot of every computed major/
                      minor leg + confluence zone set, so "why did the bot
                      think 2,345 was resistance on Tuesday" is answerable
                      after the fact, same audit-trail rationale as
                      macro_data_history.db.

Both tables are best-effort: a disk/locking problem here must never break a
live scan, exactly like macro_data.py's _save_to_db().

Ported 2026-06-29 from the (now-archived) "RoBotTrading man 0 US" sandbox
folder into "RoBotTrading man 0 V10" (the local single source-of-truth
working copy) — no logic changes, only this note added; see that folder's
CLAUDE.md for the strategy-numbering context (registered as the 32nd
strategy, after #27-31).
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fib_confluence_history.db")

# ---------------------------------------------------------------------------
# 1. Fibonacci level table — exact values from the user's MT4 screenshot.
# ---------------------------------------------------------------------------
RETRACEMENT_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 0.886, 1.0]
EXTENSION_RATIOS = [1.27, 1.618, 2.0, 2.618, 3.0]
NEGATIVE_EXTENSION_RATIOS = [-0.27, -0.618, -1.0, -1.618]
ALL_RATIOS = RETRACEMENT_RATIOS + EXTENSION_RATIOS + NEGATIVE_EXTENSION_RATIOS


def level_label(ratio):
    """MT4 "Description" column format, e.g. 0.236 -> '23.6', 1.27 -> '127',
    -0.618 -> '-161.8'."""
    if ratio in EXTENSION_RATIOS or ratio == 1.0 or ratio == 0.0:
        pct = ratio * 100
    else:
        pct = ratio * 100
    if ratio < 0:
        # negative extension mirrors the positive extension's label sign
        mirrored = abs(ratio) * 100
        return f"-{mirrored:g}"
    return f"{pct:g}"


def fib_price(old_price, new_price, ratio):
    """Price at a given Fibonacci ratio for a swing leg running from
    `old_price` (the chronologically earlier anchor) to `new_price` (the
    chronologically more recent anchor).

    Matches the doc's worked examples:
      - retracement (0.0 <= ratio <= 1.0): measured BACKWARD from the recent
        extreme (`new_price`) toward the older one. ratio=0 -> new_price
        (no retracement yet), ratio=1.0 -> old_price (full retracement).
        This is why "23.6%" always lands close to wherever price *currently*
        is (the recent swing extreme) and "88.6%" lands close to the old
        extreme — exactly the doc's "23.6 is the first/shallow level, 88.6
        is the deepest" ordering, for BOTH up-legs and down-legs, with no
        separate up/down branch needed.
      - extension (ratio > 1.0) / negative extension (ratio < 0.0): measured
        FORWARD from `old_price`, continuing past the move in the leg's
        direction (ratio > 1) or the opposite direction (ratio < 0). This is
        the doc's "Fibonacci Extension" — used for new S/R beyond a level
        that has just broken (a fresh new high/low), per its 2nd usage rule.
    """
    diff = new_price - old_price
    if 0.0 <= ratio <= 1.0:
        return new_price - ratio * diff
    return old_price + ratio * diff


def fib_levels_for_leg(old_price, new_price):
    """Returns {ratio: price} for every ratio in ALL_RATIOS for one swing
    leg. This is the numeric equivalent of dropping the MT4 Fibo tool (with
    its Extension sub-tool) on a chart leg."""
    return {r: fib_price(old_price, new_price, r) for r in ALL_RATIOS}


# ---------------------------------------------------------------------------
# 2. Swing-leg detection.
#
# Deliberately NOT importing strategies._swing_points() here even though the
# logic is identical — strategies.py is the module that will call INTO this
# one (score_fib_confluence_sr() reads data["fib_confluence"]), and
# xauusd_mt5_strategy.py imports both. Keeping this module standalone (only
# numpy/pandas/stdlib) avoids any import-order/circularity risk and keeps it
# usable on its own (e.g. from strategy_simulator.py for backtesting) without
# pulling in the whole strategies module.
# ---------------------------------------------------------------------------
def _local_swing_points(df, lookback=80, order=3):
    """Same small local swing-high/low finder as strategies._swing_points()
    (see that function's docstring) — duplicated here on purpose, see module
    note above. Returns two lists of (index, price) tuples, oldest->newest,
    within the lookback window, index is positional within the returned
    (tail-sliced) window."""
    window = df.tail(lookback).reset_index(drop=True)
    highs, lows = [], []
    n = len(window)
    for i in range(order, n - order):
        seg_h = window["high"].iloc[i - order:i + order + 1]
        seg_l = window["low"].iloc[i - order:i + order + 1]
        if window["high"].iloc[i] == seg_h.max():
            highs.append((i, float(window["high"].iloc[i])))
        if window["low"].iloc[i] == seg_l.min():
            lows.append((i, float(window["low"].iloc[i])))
    return highs, lows


def last_swing_leg(df, lookback=80, order=3):
    """Finds the most recent completed swing leg: takes the most recent
    swing high and the most recent swing low (from _local_swing_points) and
    orders them chronologically into (old_price, new_price, direction).

    Returns None if fewer than one swing high AND one swing low were found
    (too little data / too flat). `direction` is "up" if the leg's older
    point is the low (new_price is the high — an up-move just happened) or
    "down" if the older point is the high (a down-move just happened)."""
    highs, lows = _local_swing_points(df, lookback=lookback, order=order)
    if not highs or not lows:
        return None
    last_high_idx, last_high_price = highs[-1]
    last_low_idx, last_low_price = lows[-1]
    if last_high_idx > last_low_idx:
        # the high happened more recently than the low -> up-leg (low -> high)
        return {"old_price": last_low_price, "new_price": last_high_price,
                "old_idx": last_low_idx, "new_idx": last_high_idx, "direction": "up"}
    else:
        # the low happened more recently -> down-leg (high -> low)
        return {"old_price": last_high_price, "new_price": last_low_price,
                "old_idx": last_high_idx, "new_idx": last_low_idx, "direction": "down"}


# ---------------------------------------------------------------------------
# 3. Confirmation layers — trendline / channel / EMA / SMA / horizontal S/R.
# ---------------------------------------------------------------------------
def _fit_trendline(points):
    """Simple linear regression y = m*x + b through a list of (index,
    price) points (e.g. the last few swing lows for an up-trend support
    trendline, or swing highs for a down-trend resistance trendline).
    Returns (m, b) or None if fewer than 2 points."""
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    m, b = np.polyfit(xs, ys, 1)
    return float(m), float(b)


def _trendline_and_channel_value(df, swing_points, at_idx, max_points=4):
    """Projects a trendline fit through the most recent `max_points` swing
    points out to bar index `at_idx`, and also returns the channel
    (parallel line offset by the largest residual — i.e. the regression
    channel's far boundary, same concept as a Donchian/regression channel).
    Returns (trendline_value, channel_value) or (None, None)."""
    pts = swing_points[-max_points:]
    fit = _fit_trendline(pts)
    if fit is None:
        return None, None
    m, b = fit
    trend_val = m * at_idx + b
    residuals = [price - (m * idx + b) for idx, price in pts]
    offset = max(residuals, key=abs) if residuals else 0.0
    channel_val = trend_val + offset
    return trend_val, channel_val


def _simple_sma(series, length):
    return series.tail(length).mean()


# ---------------------------------------------------------------------------
# 4. Confluence computation.
# ---------------------------------------------------------------------------
def _confluence_zones(major_levels, minor_levels, tolerance):
    """Pairs up every major-swing level with every minor-swing level and
    keeps the pairs whose prices land within `tolerance` of each other —
    the doc's "หาจุดที่มันคอนเฟอร์เรนท์กัน... มีการซ้อนทับกันระหว่างเมเจอร์สวิงกับ
    ไมเนอร์สวิง" (find where the major-swing and minor-swing levels
    overlap). Returns a list of zone dicts, each with the average price and
    which two ratios produced it."""
    zones = []
    for m_ratio, m_price in major_levels.items():
        for n_ratio, n_price in minor_levels.items():
            if abs(m_price - n_price) <= tolerance:
                zones.append({
                    "price": (m_price + n_price) / 2.0,
                    "major_ratio": m_ratio,
                    "major_price": m_price,
                    "minor_ratio": n_ratio,
                    "minor_price": n_price,
                })
    return zones


def compute_confluence(data, major_tf="h4", minor_tf="h1",
                        major_lookback=150, major_order=5,
                        minor_lookback=60, minor_order=3,
                        confluence_atr_mult=0.35, confirm_atr_mult=0.4):
    """Main entry point. `data` is the same dict build_market_data() passes
    to every strategy (already-enriched d1/h4/h1/... dataframes). Returns a
    result dict (see bottom of function) describing the major leg, minor
    leg, every confluence zone found, and the single nearest zone above and
    below the current price (candidate resistance / candidate support).
    Never raises on bad/short data — returns a dict with "error" instead, so
    the caller's safe-wrapper + score_fib_confluence_sr() can both degrade
    gracefully exactly like the macro_bias / DOM strategies do.
    """
    df_major = data.get(major_tf)
    df_minor = data.get(minor_tf)
    if df_major is None or df_minor is None or len(df_major) < major_lookback // 2 \
            or len(df_minor) < minor_lookback // 2:
        return {"error": "insufficient data for major/minor swing detection"}

    major_leg = last_swing_leg(df_major, lookback=major_lookback, order=major_order)
    minor_leg = last_swing_leg(df_minor, lookback=minor_lookback, order=minor_order)
    if major_leg is None or minor_leg is None:
        return {"error": "no clear major/minor swing leg found"}

    major_levels = fib_levels_for_leg(major_leg["old_price"], major_leg["new_price"])
    minor_levels = fib_levels_for_leg(minor_leg["old_price"], minor_leg["new_price"])

    atr_minor = df_minor["atr14"].iloc[-1]
    if atr_minor is None or pd.isna(atr_minor):
        atr_minor = (df_minor["high"] - df_minor["low"]).tail(20).mean()
    atr_minor = max(float(atr_minor), 1e-6)

    tolerance = atr_minor * confluence_atr_mult
    zones = _confluence_zones(major_levels, minor_levels, tolerance)

    # --- confirmation layers, scored onto each zone --------------------
    minor_highs, minor_lows = _local_swing_points(df_minor, lookback=minor_lookback, order=minor_order)
    last_idx = len(df_minor.tail(minor_lookback)) - 1
    last_close = float(df_minor["close"].iloc[-1])
    ema20 = float(df_minor["ema20"].iloc[-1])
    ema50 = float(df_minor["ema50"].iloc[-1])
    ema200 = float(df_minor["ema200"].iloc[-1])
    sma50 = float(_simple_sma(df_minor["close"], 50))
    moving_avgs = [ema20, ema50, ema200, sma50]

    support_trend_val, support_channel_val = _trendline_and_channel_value(df_minor, minor_lows, last_idx)
    resist_trend_val, resist_channel_val = _trendline_and_channel_value(df_minor, minor_highs, last_idx)
    trend_channel_vals = [v for v in
                           [support_trend_val, support_channel_val, resist_trend_val, resist_channel_val]
                           if v is not None]

    # prior horizontal S/R = swing highs/lows NOT already used as the major/minor leg anchors
    used_prices = {major_leg["old_price"], major_leg["new_price"], minor_leg["old_price"], minor_leg["new_price"]}
    horizontal_levels = [p for _, p in (minor_highs + minor_lows) if p not in used_prices]

    confirm_tol = atr_minor * confirm_atr_mult
    for zone in zones:
        confirmations = []
        if any(abs(zone["price"] - ma) <= confirm_tol for ma in moving_avgs):
            confirmations.append("ema_sma")
        if any(abs(zone["price"] - lvl) <= confirm_tol for lvl in horizontal_levels):
            confirmations.append("horizontal_sr")
        if any(abs(zone["price"] - v) <= confirm_tol for v in trend_channel_vals):
            confirmations.append("trendline_channel")
        zone["confirmations"] = confirmations
        zone["confluence_score"] = min(100.0, 45.0 + 18.0 * len(confirmations))
        zone["distance_from_price"] = zone["price"] - last_close

    zones.sort(key=lambda z: abs(z["distance_from_price"]))

    resistance_zones = sorted((z for z in zones if z["distance_from_price"] > 0),
                               key=lambda z: z["distance_from_price"])
    support_zones = sorted((z for z in zones if z["distance_from_price"] < 0),
                            key=lambda z: -z["distance_from_price"])

    major_bias = "bullish" if major_leg["direction"] == "up" else "bearish"

    result = {
        "symbol_price": last_close,
        "atr_minor": atr_minor,
        "major_leg": major_leg,
        "minor_leg": minor_leg,
        "major_bias": major_bias,
        "zones": zones,
        "nearest_resistance": resistance_zones[0] if resistance_zones else None,
        "nearest_support": support_zones[0] if support_zones else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_snapshot(result)
    return result


# ---------------------------------------------------------------------------
# 5. SQLite persistence — price bars + confluence snapshots.
#    Same append-only, best-effort pattern as macro_data.py's
#    _db_connect()/_save_to_db() (see that file's docstring for the
#    "ป้องกันกรณี bot error ข้อมูลจะได้ไม่หาย" rationale this mirrors).
# ---------------------------------------------------------------------------
def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS price_bars (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        bar_time REAL NOT NULL,
        bar_time_iso TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, tick_volume REAL,
        atr14 REAL, ema20 REAL, ema50 REAL, ema200 REAL,
        inserted_at REAL NOT NULL
    )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_price_bars_unique "
                 "ON price_bars(symbol, timeframe, bar_time)")
    conn.execute("""CREATE TABLE IF NOT EXISTS fib_confluence_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at REAL NOT NULL,
        scanned_at_iso TEXT NOT NULL,
        price REAL,
        major_leg_json TEXT,
        minor_leg_json TEXT,
        zones_json TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fib_history_time "
                 "ON fib_confluence_history(scanned_at)")
    return conn


def save_price_bars(symbol, timeframe, df, tail_n=5):
    """Appends the most recent `tail_n` CLOSED bars of `df` to the local
    price_bars table — the "good database structure for record market price
    for use to calculation" the user asked for. Only the last few bars are
    passed in each call (the rest were already inserted on prior scans); the
    UNIQUE(symbol, timeframe, bar_time) index makes repeat inserts of the
    same bar a silent no-op (INSERT OR IGNORE) rather than a duplicate row,
    so this is cheap and safe to call every scan for every timeframe.
    Best-effort only — never raises, never blocks a live scan."""
    try:
        if df is None or len(df) == 0:
            return
        rows = df.tail(tail_n)
        conn = _db_connect()
        with conn:
            for _, r in rows.iterrows():
                bar_time = pd.Timestamp(r["time"]).timestamp()
                conn.execute(
                    "INSERT OR IGNORE INTO price_bars "
                    "(symbol, timeframe, bar_time, bar_time_iso, open, high, low, close, "
                    "tick_volume, atr14, ema20, ema50, ema200, inserted_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (symbol, timeframe, bar_time, pd.Timestamp(r["time"]).isoformat(),
                     float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                     float(r.get("tick_volume", 0) or 0),
                     float(r["atr14"]) if "atr14" in r and pd.notna(r["atr14"]) else None,
                     float(r["ema20"]) if "ema20" in r and pd.notna(r["ema20"]) else None,
                     float(r["ema50"]) if "ema50" in r and pd.notna(r["ema50"]) else None,
                     float(r["ema200"]) if "ema200" in r and pd.notna(r["ema200"]) else None,
                     time.time()))
        conn.close()
    except Exception:
        pass


def get_price_bars(symbol, timeframe, limit=500):
    """Reads back stored bars, oldest -> newest. Returns a list of dicts;
    never raises (returns [] on any DB problem) — usable directly by
    strategy_simulator.py / backtest_sim.py for a deeper history than one
    live MT5 call provides."""
    try:
        conn = _db_connect()
        cur = conn.execute(
            "SELECT bar_time_iso, open, high, low, close, tick_volume, atr14, ema20, ema50, ema200 "
            "FROM price_bars WHERE symbol = ? AND timeframe = ? "
            "ORDER BY bar_time DESC LIMIT ?", (symbol, timeframe, limit))
        rows = cur.fetchall()
        conn.close()
        cols = ["time", "open", "high", "low", "close", "tick_volume", "atr14", "ema20", "ema50", "ema200"]
        return [dict(zip(cols, row)) for row in reversed(rows)]
    except Exception:
        return []


def _save_snapshot(result):
    """Appends one fib_confluence_history row — best-effort, see module
    docstring. Stores the major/minor leg and every zone found so a past
    scan's reasoning can be audited later."""
    try:
        conn = _db_connect()
        with conn:
            conn.execute(
                "INSERT INTO fib_confluence_history "
                "(scanned_at, scanned_at_iso, price, major_leg_json, minor_leg_json, zones_json) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), result.get("computed_at", datetime.now(timezone.utc).isoformat()),
                 result.get("symbol_price"),
                 json.dumps(result.get("major_leg"), default=str),
                 json.dumps(result.get("minor_leg"), default=str),
                 json.dumps(result.get("zones"), default=str)))
        conn.close()
    except Exception:
        pass


def get_confluence_history(limit=200):
    """Reads back past confluence snapshots, newest first. Never raises."""
    try:
        conn = _db_connect()
        cur = conn.execute(
            "SELECT scanned_at_iso, price, major_leg_json, minor_leg_json, zones_json "
            "FROM fib_confluence_history ORDER BY scanned_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        out = []
        for iso, price, major_json, minor_json, zones_json in rows:
            try:
                out.append({
                    "scanned_at_iso": iso, "price": price,
                    "major_leg": json.loads(major_json) if major_json else None,
                    "minor_leg": json.loads(minor_json) if minor_json else None,
                    "zones": json.loads(zones_json) if zones_json else [],
                })
            except Exception:
                continue
        return out
    except Exception:
        return []
```

## Step 1 — `strategies.py`

First, near the top of the file (after the `import numpy as np` / `import
pandas as pd` / `from collections import deque` block), add a defensive
import for the new level-name formatter — same try/except pattern already
used for every other optional data source in this file:

```python
# 32nd strategy's level-name formatter — try/except import so a missing/
# broken fib_confluence.py degrades to a plain percentage string instead of
# breaking this whole module (same defensive pattern as the macro_bias /
# DOM strategies below, which never let an optional data source raise).
try:
    from fib_confluence import level_label
except Exception:
    def level_label(ratio):
        return f"{ratio * 100:g}"
```

Then add the scoring function itself. Paste it immediately before
`STRATEGY_REGISTRY = {` (i.e. after `score_smart_money_sweep` /
`_variance_ratio`/whatever the last strategy function on the VPS copy is):

```python
def score_fib_confluence_sr(data, proximity_atr=0.4):
    """32nd strategy — Fibonacci Confluence S/R (Major+Minor Swing) ★.

    User-requested: find accurate support/resistance by combining a major
    swing (H4) Fibonacci retracement/extension table with a minor swing
    (H1) one, per the user's reference doc and MT4 level screenshot — see
    fib_confluence.py's module docstring for the full level table (0/23.6/
    38.2/50/61.8/78.6/88.6/100 retracement, 127/161.8/200/261.8/300
    extension, -127/-161.8/-200/-261.8 negative extension) and the
    major+minor confluence-zone math.

    This function does NOT recompute any of that — it only reads the
    pre-computed snapshot at data["fib_confluence"] (built once per scan by
    get_fib_confluence_safe() in xauusd_mt5_strategy.py, mirroring how
    score_macro_bias() reads data["macro"]) and adds the final entry
    trigger: price must currently be at the nearest confluence zone AND
    print a rejection candle there (same pin-bar/engulfing check as
    score_climax_reversal_sr above) — the confluence zone by itself is a
    level to watch, not a signal to vote on.

    Scores 0/0 gracefully if data["fib_confluence"] is missing, errored, or
    no zone/leg was found — never raises."""
    snap = data.get("fib_confluence")
    if not snap or snap.get("error"):
        reason = snap.get("error") if snap else "fib_confluence snapshot unavailable"
        return {"long": 0.0, "short": 0.0, "note": f"fib confluence: {reason}"}

    df = data.get("h1")
    if df is None or len(df) < 3:
        return {"long": 0.0, "short": 0.0, "note": "insufficient H1 data"}

    atr_now = snap.get("atr_minor")
    if not atr_now or pd.isna(atr_now):
        atr_now = float((df["high"] - df["low"]).tail(20).mean())
    atr_now = max(float(atr_now), 1e-6)

    last, prev = df.iloc[-1], df.iloc[-2]
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
    note = "no confluence zone within range of current price"

    support = snap.get("nearest_support")
    if support and abs(last["low"] - support["price"]) <= atr_now * proximity_atr:
        is_pin    = lower_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = (cur_bullish and prev_bearish
                     and last["open"] <= prev_body_low and last["close"] >= prev_body_high)
        if is_pin or is_engulf:
            shape = "bullish pin bar" if is_pin else "bullish engulfing"
            n_confirm = len(support.get("confirmations", []))
            quality = (lower_wick / rng) if is_pin else (body / rng)
            long_score = _clip(support["confluence_score"] * 0.5 + quality * 30 + n_confirm * 5)
            note = (f"{shape} at Fib confluence support {support['price']:.2f} "
                    f"(major {level_label(support['major_ratio'])}% / minor {level_label(support['minor_ratio'])}%, "
                    f"{n_confirm} confirmation(s))")
        else:
            note = f"price at Fib confluence support {support['price']:.2f} but no rejection candle yet"

    resistance = snap.get("nearest_resistance")
    if resistance and abs(last["high"] - resistance["price"]) <= atr_now * proximity_atr:
        is_pin    = upper_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = (cur_bearish and prev_bullish
                     and last["open"] >= prev_body_high and last["close"] <= prev_body_low)
        if is_pin or is_engulf:
            shape = "bearish pin bar" if is_pin else "bearish engulfing"
            n_confirm = len(resistance.get("confirmations", []))
            quality = (upper_wick / rng) if is_pin else (body / rng)
            short_score = _clip(resistance["confluence_score"] * 0.5 + quality * 30 + n_confirm * 5)
            note = (f"{shape} at Fib confluence resistance {resistance['price']:.2f} "
                    f"(major {level_label(resistance['major_ratio'])}% / minor {level_label(resistance['minor_ratio'])}%, "
                    f"{n_confirm} confirmation(s))")
        elif long_score == 0.0:
            note = f"price at Fib confluence resistance {resistance['price']:.2f} but no rejection candle yet"

    return {"long": long_score, "short": short_score, "note": note}
```

Then add the registry entry. Find the end of `STRATEGY_REGISTRY = {` (it
should currently end with the `smart_money_sweep_night` entry — a 3-tuple
lambda wrapping `score_smart_money_sweep`). Add, right before the closing
`}`:

```python
    # ---- 32nd: Fibonacci Confluence S/R (Major+Minor Swing) ★. Ported in
    # from the fib_confluence.py module (H4 major-swing + H1 minor-swing
    # Fibonacci level tables, confluence zones confirmed by EMA/SMA,
    # horizontal S/R, and trendline/channel proximity — see that module's
    # docstring for the full level table and confluence math). Reads
    # data["fib_confluence"] (built once per scan by get_fib_confluence_safe()
    # in xauusd_mt5_strategy.py) and only votes once price is at the
    # nearest confluence zone AND prints a rejection candle there. Scores
    # 0/0 gracefully if the snapshot is missing/errored.
    "fib_confluence_sr": ("Fibonacci Confluence S/R (Major+Minor Swing) ★", score_fib_confluence_sr),
}
```

## Step 2 — `xauusd_mt5_strategy.py`

**2a.** Near the top, find the existing import block (`import macro_data`,
`import symbol_normalize`, etc.) and add:

```python
import fib_confluence
```

**2b.** Find `def get_macro_snapshot_safe():` and its full function body.
Add a new sibling function immediately after it (before
`def _mtr_is_danger(data):`, if that function exists on the VPS copy —
otherwise immediately before `def build_market_data():`):

```python
def get_fib_confluence_safe(data):
    """Wraps fib_confluence.compute_confluence() so a problem in the
    Fibonacci major/minor swing confluence engine can never break a scan —
    same try/except pattern as get_macro_snapshot_safe() above. Also
    best-effort appends the latest H4/H1 bars to fib_confluence's local
    price_bars history table before computing (each call is cheap/no-op for
    bars already stored, see fib_confluence.save_price_bars()'s docstring).
    On any error this returns None and score_fib_confluence_sr() treats
    that exactly like macro_bias/DOM-unsupported: a graceful 0/0."""
    try:
        fib_confluence.save_price_bars(SYMBOL, "h4", data.get("h4"))
        fib_confluence.save_price_bars(SYMBOL, "h1", data.get("h1"))
    except Exception:
        pass
    try:
        return fib_confluence.compute_confluence(data)
    except Exception:
        logger.exception("fib_confluence.compute_confluence() failed — fib_confluence_sr strategy will score 0/0 this scan.")
        return None
```

**2c.** Find `def build_market_data():` and its `return {...}` statement
(it builds and returns the `data` dict with keys like `"d1"`, `"h4"`,
`"h1"`, `"dom"`, `"macro"`, etc.). Change it from a single `return {...}`
into building the dict first, then adding the new key before returning —
for example, if it currently ends with:

```python
    dom = get_dom_snapshot(SYMBOL)
    macro = get_macro_snapshot_safe()
    return {"d1": df_d1, "h4": df_h4, "h1": df_h1, "m15": df_m15, "m5": df_m5, "m1": df_m1,
            "now": datetime.now(), "dom": dom, "macro": macro,
            "myfxbook_contrarian": MYFXBOOK_CONTRARIAN}
```

change it to:

```python
    dom = get_dom_snapshot(SYMBOL)
    macro = get_macro_snapshot_safe()
    data = {"d1": df_d1, "h4": df_h4, "h1": df_h1, "m15": df_m15, "m5": df_m5, "m1": df_m1,
            "now": datetime.now(), "dom": dom, "macro": macro,
            "myfxbook_contrarian": MYFXBOOK_CONTRARIAN}
    data["fib_confluence"] = get_fib_confluence_safe(data)
    return data
```

(If the VPS's actual dict has more/different keys than shown above — e.g.
extra keys added by strategies #27-31 — keep all of them; only add the two
new lines, don't remove or reorder anything else.)

**2d.** Find `_RECOMMENDED_STRATEGY_WEIGHTS = {` and its closing `}`
(should currently end with `"smart_money_sweep_night": 1.0, ...`). Add one
new entry:

```python
    "smart_money_sweep_night": 1.0,  # 31st -- same logic, US-close window 02-04 BKK
    "fib_confluence_sr": 1.2,  # 32nd -- H4 major-swing + H1 minor-swing Fibonacci confluence + rejection candle
}
```

(No other change needed here — `STRATEGY_WEIGHTS` is derived automatically
from `strategies.STRATEGY_REGISTRY`, so the new strategy is picked up
automatically once Step 1 is done; this just sets its *recommended*
default weight instead of silently falling back to 1.0.)

## Step 3 — `strategy_config_ui.py`

**3a.** Find the end of `DEFAULT_CONFIG["confluence"]["strategies"]` (should
currently end with the `smart_money_sweep_morning`/`smart_money_sweep_night`
entries). Add, right before the closing `},`:

```python
            "smart_money_sweep_morning": {"enabled": True, "weight": 1.0},
            "smart_money_sweep_night": {"enabled": True, "weight": 1.0},
            # 32nd: ported-in Fibonacci Confluence S/R (Major+Minor Swing).
            # H4 major-swing + H1 minor-swing Fibonacci tables, confluence
            # zones confirmed by EMA/SMA/horizontal-S/R/trendline, entry
            # gated on a rejection candle at the nearest zone.
            "fib_confluence_sr": {"enabled": True, "weight": 1.2},
        },
    },
```

**3b.** Find `STRATEGY13_LABELS = {` and its line for
`smart_money_sweep_night`. Add, right after it:

```python
    "smart_money_sweep_night":   "31. Smart Money Sweep — Night (US-close 02-04) ★ 🩳",
    "fib_confluence_sr": "32. Fibonacci Confluence S/R (Major+Minor Swing) ★",
}
```

## Step 4 — `generate_dashboard.py`

Two small text-only changes (the table itself is generated dynamically
from `strategy_scores.json`, so it will automatically show all 32 rows
once the EA produces them — these are just the header/comment text):

- `    # ---- confluence multi-strategy (31) scores ----` → change `(31)`
  to `(32)`
- `  <h2>Multi-Strategy Confluence (31) — Live Scores</h2>` → change `(31)`
  to `(32)`

(If the VPS copy currently shows a different number than 31 in these two
spots, that means it's already out of sync with the local copy in some
other way — report that instead of assuming 31 → 32.)

## Step 5 — `CLAUDE.md`

Find the `## Strategies (31 total)` section header and change it to
`## Strategies (32 total)`. Then, immediately before the `## Hard rules`
section, add:

```markdown
And a 32nd: **Fibonacci Confluence S/R (Major+Minor Swing)**
(`score_fib_confluence_sr` in `strategies.py`, key `fib_confluence_sr`,
weight `1.2`). User-requested: "create new strategy to find accurate
resistance and support level... config the best Fibonacci level... combine
major swing and minor swing than combine with indicator trend line,
channel, SMA/EMA, horizontal line." Built from a new standalone module,
`fib_confluence.py` (only numpy/pandas/stdlib — no circular import risk),
mirroring the `macro_data.py` / `score_macro_bias()` pattern exactly:
  - **Level table**: exact ratios from the user's MT4 screenshot —
    retracement 0/23.6/38.2/50/61.8/78.6/88.6/100, extension
    127/161.8/200/261.8/300, negative extension -127/-161.8/-200/-261.8.
  - **Major+minor swing confluence**: draws the level table on the most
    recent H4 swing leg (major) and the most recent H1 swing leg (minor)
    and keeps the price zones where a major level and a minor level land
    within one H1-ATR of each other (`fib_confluence.compute_confluence()`).
  - **Confirmation layers**: each confluence zone gets tagged with which of
    EMA20/50/EMA200/SMA50, a prior horizontal swing S/R level, or a fitted
    trendline/regression-channel projection it also sits near.
  - **Entry trigger**: only votes once price is at the nearest such zone AND
    prints a rejection candle there (same pin-bar/engulfing check as the
    26th strategy's `score_climax_reversal_sr`).
  - **New local price database**: `fib_confluence_history.db`, with
    `price_bars` (append-only H4/H1 OHLC+indicator history) and
    `fib_confluence_history` (one row per scan, for audit purposes). Both
    best-effort/never-raises.
  - Synced from the local `RoBotTrading man 0 V10` working copy on
    [today's VPS date] via this prompt.
```

## Verification before calling this done

1. Syntax-check all 5 touched files (`python -m py_compile fib_confluence.py
   strategies.py strategy_config_ui.py xauusd_mt5_strategy.py
   generate_dashboard.py`).
2. Run a quick synthetic logic test directly against
   `fib_confluence.compute_confluence()` and `score_fib_confluence_sr()`
   (no MT5 needed — both only touch pandas DataFrames): build synthetic
   H4 (250 bars) and H1 (300 bars) OHLC series with a clear swing leg,
   run them through `strategies.enrich()`, call `compute_confluence({"h4":
   df_h4, "h1": df_h1})`, confirm it returns a dict with `major_leg`,
   `minor_leg`, `zones`, `nearest_support`/`nearest_resistance` (no
   `"error"` key). Then confirm `score_fib_confluence_sr({})` and
   `score_fib_confluence_sr({"fib_confluence": None})` both return a
   graceful `{"long": 0.0, "short": 0.0, ...}` with an explanatory note
   instead of raising. This was already verified on the local copy with
   the exact same synthetic-data approach — just confirm the VPS copy
   behaves identically after the edit.
3. Confirm `strategy_config.json` on the VPS still loads correctly after a
   restart (the new key should backfill via `_deep_merge` the same way
   `myfxbook`/`climax_reversal_sr`/etc. did) — check the UI's strategies
   tab shows "32. Fibonacci Confluence S/R (Major+Minor Swing) ★" as a new
   checkbox/weight row.
4. Confirm `fib_confluence_history.db` gets created in the bot's working
   directory and that `price_bars` / `fib_confluence_history` tables exist
   after the first live scan (or the synthetic test in step 2).
5. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, any other
   strategy's weight, or any risk/lot parameter as part of this sync — this
   is purely additive, matching the local copy exactly.
6. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running — apply the edits, syntax-check, run the synthetic
   test, and report back; let the user decide when to restart so the new
   strategy actually takes effect in the live scan.
