"""
Harmonic Patterns (XABCD) — Potential Reversal Zone Engine
=============================================================
Implements the user-supplied reference doc ("Harmonic Patterns.docx") as a
computable strategy: detects the 6 "classic" XABCD harmonic patterns —
Gartley, Bat, Butterfly, Crab, Deep Crab, and Cypher — from confirmed swing
points on H1, projects each pattern's Potential Reversal Zone (PRZ), and
cross-checks that PRZ against the existing Fibonacci Confluence S/R engine
(`fib_confluence.py`, the bot's 32nd strategy) so a PRZ that also lines up
with an independently-computed major/minor-swing Fib confluence zone scores
higher — this is the "use data from the Fibonacci strategy to make the
signal more accurate" behavior the user asked for, done by cross-referencing
two independently-computed structures rather than one depending on the
other's internals.

Pattern structure, straight from the reference doc:

    X -> A -> B -> C -> D
    XA = initial impulse move
    AB = retracement of XA
    BC = retracement of AB
    CD = final extension leading to reversal
    D  = Potential Reversal Zone (PRZ)

X, A, B, C are taken from the last 4 *confirmed* zigzag swing points (see
`_zigzag_points()` below) — alternating high/low by construction, so this
module never has to guess which point is which. D is deliberately NOT a
confirmed swing point: it is the anticipated reversal level the pattern
projects forward, and `score_harmonic_patterns()` (in `strategies.py`) only
votes once price actually reaches that projected zone AND prints a
rejection candle there — exactly the doc's "Trading Rules": "Wait for price
to reach D... Enter long only after confirmation."

Ratio formulas (doc's "Formula" sections per pattern, encoded below in
`PATTERNS`):

    Gartley:   AB=61.8%XA,        BC=38.2-88.6%AB, CD=127.2-161.8%BC, XD=78.6%XA
    Bat:       AB=38.2-50%XA,     BC=38.2-88.6%AB, CD=161.8-261.8%BC, XD=88.6%XA
    Butterfly: AB=78.6%XA,        BC=38.2-88.6%AB, CD=161.8-261.8%BC, XD=127.2%XA
    Crab:      AB=38.2-61.8%XA,   BC=38.2-88.6%AB, CD=224-361.8%BC,   XD=161.8%XA
    Deep Crab: AB=88.6%XA,        BC=38.2-88.6%AB, CD=224-361.8%BC,   XD=161.8%XA
    Cypher:    AB=38.2-61.8%XA,   XC=127.2-141.4%XA (not BC -- Cypher measures
               C's distance from X directly), CD=78.6% retracement of XC
               (not BC) -- handled as a separate code path, see
               `_match_cypher()`, because its structure genuinely differs
               from the other five (doc: "CD = 78.6% XC").

Each ratio band gets a small tolerance added (`_RATIO_TOL`) because live
swing points essentially never land on the exact textbook ratio — the doc
itself frames the PRZ as "where several Fibonacci projections converge"
rather than one exact number, so a small band plus the dual XD/CD
convergence check (below) does the same job a discretionary trader does by
eye on a chart.

D-price projection (two independent formulas per match, same idea as the
doc's worked PRZ example "XD=88.6%XA AND CD=2.618BC... when all measurements
cluster... the probability of a reversal generally improves"):

    d_from_xd = A + xd_ratio * (X - A)     -- linear point on the X-A line,
                                                xd_ratio=1.0 lands exactly on
                                                X, >1.0 extends past X (this
                                                is why Butterfly/Crab's D is
                                                a fresh extreme beyond X).
    d_from_cd = C + cd_ratio * (C - B)     -- continues the B->C direction
                                                past C by cd_ratio x the BC
                                                distance.

`confluence_score` rewards: (a) how many of the AB/BC/XD ratio bands were
actually matched, (b) how tightly d_from_xd and d_from_cd agree with each
other (the doc's "PRZ convergence"), and (c) whether the projected D also
lines up with an independently-computed `fib_confluence` zone.

This module is intentionally standalone (only numpy/pandas/stdlib, like
`fib_confluence.py`) so there is no import-order/circularity risk and it
stays usable from `strategy_simulator.py`/`backtest_sim.py` for backtesting.

Data flow (mirrors fib_confluence.py's relationship to the rest of the bot):

    xauusd_mt5_strategy.build_market_data()
        -> get_harmonic_patterns_safe(data)  [try/except wrapper, same
                                                pattern as
                                                get_fib_confluence_safe()]
        -> harmonic_patterns.compute_harmonic_patterns(data)   (this module)
        -> result stored at data["harmonic"]
    strategies.score_harmonic_patterns(data)
        -> reads data["harmonic"], scores 0/0 gracefully if missing/errored,
           exactly like score_fib_confluence_sr() does for
           data["fib_confluence"].

Database: `harmonic_patterns_history.db` (separate SQLite file, same
append-only/best-effort/never-raises pattern as `macro_data.py`'s
`_save_to_db()` and `fib_confluence.py`'s history table) — one row per scan
with every matched pattern, so "why did the bot think this was a Bat
pattern" is answerable after the fact.

Built 2026-06-29 in "RoBotTrading man 0 V10" (the single source-of-truth
local working copy) per the user's uploaded "Harmonic Patterns.docx" and
request to combine it with the existing Fibonacci Confluence S/R strategy.
Registered as the bot's 33rd strategy — see this folder's CLAUDE.md.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harmonic_patterns_history.db")

_RATIO_TOL = 0.09          # tolerance band added around each textbook ratio
# Widened from 0.07 -> 0.09 on 2026-06-30 after a live-VPS analysis of the
# 09:05-10:30 reversal found ZERO XABCD matches across 135 consecutive scans
# in that window (see ANALYSIS_REVERSAL_2026-06-30_0905-1030.md) -- 0.07 was
# evidently too tight for XAUUSD's real intraday swing noise. If this still
# produces too few matches, the next step up the report suggested was 0.10;
# if it now produces too MANY low-quality matches, drop back toward 0.07 --
# PRZ convergence (_XD_CONVERGENCE_ATR) and the fib_confluence cross-check
# bonus are the secondary filters that should keep low-quality matches from
# actually voting once entry-trigger (rejection candle at PRZ) is required.
_XD_CONVERGENCE_ATR = 1.0  # d_from_xd vs d_from_cd must agree within this many ATRs to count as "converged"

PATTERNS = {
    "Gartley": {
        "ab_xa": (0.618 - _RATIO_TOL, 0.618 + _RATIO_TOL),
        "bc_ab": (0.382 - _RATIO_TOL, 0.886 + _RATIO_TOL),
        "cd_bc": (1.272 - _RATIO_TOL, 1.618 + _RATIO_TOL),
        "xd_xa": 0.786,
    },
    "Bat": {
        "ab_xa": (0.382 - _RATIO_TOL, 0.50 + _RATIO_TOL),
        "bc_ab": (0.382 - _RATIO_TOL, 0.886 + _RATIO_TOL),
        "cd_bc": (1.618 - _RATIO_TOL, 2.618 + _RATIO_TOL),
        "xd_xa": 0.886,
    },
    "Butterfly": {
        "ab_xa": (0.786 - _RATIO_TOL, 0.786 + _RATIO_TOL),
        "bc_ab": (0.382 - _RATIO_TOL, 0.886 + _RATIO_TOL),
        "cd_bc": (1.618 - _RATIO_TOL, 2.618 + _RATIO_TOL),
        "xd_xa": 1.272,
    },
    "Crab": {
        "ab_xa": (0.382 - _RATIO_TOL, 0.618 + _RATIO_TOL),
        "bc_ab": (0.382 - _RATIO_TOL, 0.886 + _RATIO_TOL),
        "cd_bc": (2.24 - _RATIO_TOL, 3.618 + _RATIO_TOL),
        "xd_xa": 1.618,
    },
    "Deep Crab": {
        "ab_xa": (0.886 - _RATIO_TOL, 0.886 + _RATIO_TOL),
        "bc_ab": (0.382 - _RATIO_TOL, 0.886 + _RATIO_TOL),
        "cd_bc": (2.24 - _RATIO_TOL, 3.618 + _RATIO_TOL),
        "xd_xa": 1.618,
    },
}
CYPHER_AB_XA = (0.382 - _RATIO_TOL, 0.618 + _RATIO_TOL)
CYPHER_XC_XA = (1.272 - _RATIO_TOL, 1.414 + _RATIO_TOL)
CYPHER_CD_XC = 0.786


def level_label(ratio):
    return f"{ratio * 100:g}"


def _zigzag_points(df, lookback=200, atr_mult=2.0):
    """Returns a chronological list of (idx, price, 'high'/'low') confirmed
    pivots within the tail `lookback` bars of df."""
    sub = df.tail(lookback).reset_index(drop=True)
    n = len(sub)
    if n < 10:
        return []
    atr = sub["atr14"]
    atr_fallback = float((sub["high"] - sub["low"]).tail(20).mean()) or 1.0

    high_price, high_idx = float(sub["high"].iloc[0]), 0
    low_price, low_idx = float(sub["low"].iloc[0]), 0
    trend = 0  # 0=undetermined, 1=tracking up toward a high, -1=tracking down toward a low
    pivots = []

    for i in range(1, n):
        a = atr.iloc[i]
        t = (float(a) if pd.notna(a) and a > 0 else atr_fallback) * atr_mult
        hi, lo = float(sub["high"].iloc[i]), float(sub["low"].iloc[i])

        if trend in (0, 1):
            if hi > high_price:
                high_price, high_idx = hi, i
            if high_price - lo >= t and trend != -1:
                pivots.append((high_idx, high_price, "high"))
                trend = -1
                low_price, low_idx = lo, i
                high_price, high_idx = hi, i
                continue

        if trend in (0, -1):
            if lo < low_price:
                low_price, low_idx = lo, i
            if hi - low_price >= t and trend != 1:
                pivots.append((low_idx, low_price, "low"))
                trend = 1
                high_price, high_idx = hi, i
                low_price, low_idx = lo, i
                continue

    return pivots


def _ratio(numer, denom):
    denom = abs(denom)
    return abs(numer) / denom if denom > 1e-9 else None


def _in_band(value, band):
    return value is not None and band[0] <= value <= band[1]


def _match_classic(name, spec, X, A, B, C, atr):
    ab_xa = _ratio(B - A, A - X)
    bc_ab = _ratio(C - B, B - A)
    if not _in_band(ab_xa, spec["ab_xa"]) or not _in_band(bc_ab, spec["bc_ab"]):
        return None

    xd_ratio = spec["xd_xa"]
    d_from_xd = A + xd_ratio * (X - A)

    cd_lo, cd_hi = spec["cd_bc"]
    cd_mid = (cd_lo + cd_hi) / 2.0
    d_from_cd = C + cd_mid * (C - B)
    cd_ratio_implied = _ratio(d_from_xd - C, C - B)
    cd_confirmed = _in_band(cd_ratio_implied, spec["cd_bc"])

    convergence = abs(d_from_xd - d_from_cd)
    n_confirm = sum([
        _in_band(ab_xa, spec["ab_xa"]),
        _in_band(bc_ab, spec["bc_ab"]),
        bool(cd_confirmed),
    ])
    tightness = max(0.0, 1.0 - min(convergence / max(atr * _XD_CONVERGENCE_ATR, 1e-6), 1.0))
    confluence_score = min(100.0, 40.0 + n_confirm * 12.0 + tightness * 24.0)

    return {
        "pattern": name,
        "prz_price": d_from_xd,
        "d_from_xd": d_from_xd,
        "d_from_cd": d_from_cd,
        "convergence_atr": convergence / atr if atr else None,
        "ab_xa": ab_xa, "bc_ab": bc_ab, "cd_bc_implied": cd_ratio_implied,
        "n_confirm": n_confirm,
        "confluence_score": confluence_score,
    }


def _match_cypher(X, A, B, C, atr):
    ab_xa = _ratio(B - A, A - X)
    xc_xa = _ratio(C - X, A - X)
    if not _in_band(ab_xa, CYPHER_AB_XA) or not _in_band(xc_xa, CYPHER_XC_XA):
        return None

    d_from_cd = C - CYPHER_CD_XC * (C - X)
    n_confirm = sum([_in_band(ab_xa, CYPHER_AB_XA), _in_band(xc_xa, CYPHER_XC_XA)]) + 1
    confluence_score = min(100.0, 40.0 + n_confirm * 12.0)

    return {
        "pattern": "Cypher",
        "prz_price": d_from_cd,
        "d_from_xd": d_from_cd,
        "d_from_cd": d_from_cd,
        "convergence_atr": 0.0,
        "ab_xa": ab_xa, "bc_ab": None, "cd_bc_implied": xc_xa,
        "n_confirm": n_confirm,
        "confluence_score": confluence_score,
    }


def compute_harmonic_patterns(data, timeframe="h1", lookback=200, atr_mult=2.0,
                               fib_proximity_atr=1.0):
    """Main entry point. `data` is the same dict build_market_data() passes
    to every strategy. Returns a result dict (see bottom of function)
    describing the X/A/B/C points, every matched pattern (with PRZ +
    confluence_score, including a fib_aligned cross-check against
    data["fib_confluence"]), and the best match. Never raises on bad/short
    data -- returns a dict with "error" instead, mirroring
    fib_confluence.compute_confluence()'s degrade-gracefully contract."""
    df = data.get(timeframe)
    if df is None or len(df) < lookback // 3:
        return {"error": f"insufficient {timeframe} data for swing detection"}

    pivots = _zigzag_points(df, lookback=lookback, atr_mult=atr_mult)
    if len(pivots) < 4:
        return {"error": "fewer than 4 confirmed swing points -- no XABCD chain yet"}

    (x_idx, x_price, x_type), (a_idx, a_price, a_type), \
        (b_idx, b_price, b_type), (c_idx, c_price, c_type) = pivots[-4:]

    direction = "bullish" if x_type == "high" else "bearish"

    atr_now = df["atr14"].iloc[-1] if "atr14" in df.columns else None
    if atr_now is None or pd.isna(atr_now):
        atr_now = float((df["high"] - df["low"]).tail(20).mean())
    atr_now = max(float(atr_now), 1e-6)

    matches = []
    for name, spec in PATTERNS.items():
        m = _match_classic(name, spec, x_price, a_price, b_price, c_price, atr_now)
        if m:
            matches.append(m)
    cypher = _match_cypher(x_price, a_price, b_price, c_price, atr_now)
    if cypher:
        matches.append(cypher)

    fib_snap = data.get("fib_confluence") or {}
    fib_zone = fib_snap.get("nearest_support") if direction == "bullish" else fib_snap.get("nearest_resistance")
    for m in matches:
        m["fib_aligned"] = False
        m["fib_zone_price"] = None
        if fib_zone and abs(m["prz_price"] - fib_zone["price"]) <= atr_now * fib_proximity_atr:
            m["fib_aligned"] = True
            m["fib_zone_price"] = fib_zone["price"]
            m["confluence_score"] = min(100.0, m["confluence_score"] + 15.0)

    matches.sort(key=lambda m: m["confluence_score"], reverse=True)

    result = {
        "timeframe": timeframe,
        "symbol_price": float(df["close"].iloc[-1]),
        "atr_now": atr_now,
        "direction": direction,
        "points": {
            "X": {"idx": x_idx, "price": x_price, "type": x_type},
            "A": {"idx": a_idx, "price": a_price, "type": a_type},
            "B": {"idx": b_idx, "price": b_price, "type": b_type},
            "C": {"idx": c_idx, "price": c_price, "type": c_type},
        },
        "matches": matches,
        "best_match": matches[0] if matches else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_snapshot(result)
    return result


def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS harmonic_pattern_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at REAL NOT NULL,
        scanned_at_iso TEXT NOT NULL,
        timeframe TEXT,
        direction TEXT,
        price REAL,
        best_pattern TEXT,
        best_score REAL,
        points_json TEXT,
        matches_json TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_harmonic_history_time "
                 "ON harmonic_pattern_history(scanned_at)")
    return conn


def _save_snapshot(result):
    try:
        best = result.get("best_match") or {}
        conn = _db_connect()
        with conn:
            conn.execute(
                "INSERT INTO harmonic_pattern_history "
                "(scanned_at, scanned_at_iso, timeframe, direction, price, best_pattern, "
                "best_score, points_json, matches_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), result.get("computed_at", datetime.now(timezone.utc).isoformat()),
                 result.get("timeframe"), result.get("direction"), result.get("symbol_price"),
                 best.get("pattern"), best.get("confluence_score"),
                 json.dumps(result.get("points"), default=str),
                 json.dumps(result.get("matches"), default=str)))
        conn.close()
    except Exception:
        pass


def get_pattern_history(limit=200):
    try:
        conn = _db_connect()
        cur = conn.execute(
            "SELECT scanned_at_iso, timeframe, direction, price, best_pattern, best_score, "
            "points_json, matches_json FROM harmonic_pattern_history "
            "ORDER BY scanned_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        out = []
        for iso, tf, direction, price, best_pattern, best_score, points_json, matches_json in rows:
            try:
                out.append({
                    "scanned_at_iso": iso, "timeframe": tf, "direction": direction, "price": price,
                    "best_pattern": best_pattern, "best_score": best_score,
                    "points": json.loads(points_json) if points_json else None,
                    "matches": json.loads(matches_json) if matches_json else [],
                })
            except Exception:
                continue
        return out
    except Exception:
        return []
