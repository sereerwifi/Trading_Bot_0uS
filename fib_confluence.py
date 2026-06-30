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
    -0.27 -> '-127', -0.618 -> '-161.8'."""
    if ratio < 0:
        # Negative extension labels: -0.27 -> 127, -0.618 -> 161.8, etc.
        # Formula: (abs(ratio) + 1.0) * 100, matching the MT4 "Description" column.
        mirrored = (abs(ratio) + 1.0) * 100
        return f"-{mirrored:g}"
    return f"{ratio * 100:g}"


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
        return {"old_price": last_low_price, "new_price": last_high_price,
                "old_idx": last_low_idx, "new_idx": last_high_idx, "direction": "up"}
    else:
        return {"old_price": last_high_price, "new_price": last_low_price,
                "old_idx": last_high_idx, "new_idx": last_low_idx, "direction": "down"}


# ---------------------------------------------------------------------------
# 3. Confirmation layers — trendline / channel / EMA / SMA / horizontal S/R.
# ---------------------------------------------------------------------------
def _fit_trendline(points):
    """Simple linear regression y = m*x + b through a list of (index,
    price) points. Returns (m, b) or None if fewer than 2 points."""
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    m, b = np.polyfit(xs, ys, 1)
    return float(m), float(b)


def _trendline_and_channel_value(df, swing_points, at_idx, max_points=4):
    """Projects a trendline fit through the most recent `max_points` swing
    points out to bar index `at_idx`, and also returns the channel
    (parallel line offset by the largest residual). Returns
    (trendline_value, channel_value) or (None, None)."""
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
    keeps the pairs whose prices land within `tolerance` of each other."""
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
    to every strategy. Returns a result dict describing the major leg, minor
    leg, every confluence zone found, and the single nearest zone above and
    below the current price. Never raises on bad/short data — returns a dict
    with "error" instead."""
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
# ---------------------------------------------------------------------------
def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent reads while bot writes every scan
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
    price_bars table. Deduplicated on (symbol, timeframe, bar_time) —
    safe to call every scan. Best-effort only — never raises.
    Always skips the last row of the tail (the currently-forming candle)
    whose OHLC is still live and would be stored stale."""
    try:
        if df is None or len(df) == 0:
            return
        # Fetch tail_n+1 then drop the last row (forming candle) so only
        # fully-closed bars enter the DB.
        rows = df.tail(tail_n + 1).iloc[:-1]
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
    """Reads back stored bars, oldest -> newest. Never raises."""
    conn = None
    try:
        conn = _db_connect()
        cur = conn.execute(
            "SELECT bar_time_iso, open, high, low, close, tick_volume, atr14, ema20, ema50, ema200 "
            "FROM price_bars WHERE symbol = ? AND timeframe = ? "
            "ORDER BY bar_time DESC LIMIT ?", (symbol, timeframe, limit))
        rows = cur.fetchall()
        conn.close()
        conn = None
        cols = ["time", "open", "high", "low", "close", "tick_volume", "atr14", "ema20", "ema50", "ema200"]
        return [dict(zip(cols, row)) for row in reversed(rows)]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def _save_snapshot(result):
    """Appends one fib_confluence_history row — best-effort."""
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
    conn = None
    try:
        conn = _db_connect()
        cur = conn.execute(
            "SELECT scanned_at_iso, price, major_leg_json, minor_leg_json, zones_json "
            "FROM fib_confluence_history ORDER BY scanned_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        conn = None
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
    finally:
        if conn is not None:
            conn.close()
