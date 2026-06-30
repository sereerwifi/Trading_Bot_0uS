# Prompt for Claude Code (run ON THE VPS) — sync the new 33rd strategy

Paste this into Claude Code **on the VPS**, in the live bot folder (whichever
one is currently authoritative there). This adds the 33rd confluence
strategy, **"Harmonic Patterns (XABCD)"**, which was just built and verified
on the local working copy (`RoBotTrading man 0 V10`). It is purely additive
— no existing strategy, weight, or behavior changes.

**Before touching anything: read the VPS's own `CLAUDE.md` and compare its
"Strategies (N total)" section, its `strategies.py` `STRATEGY_REGISTRY`, and
`xauusd_mt5_strategy.py`'s `_RECOMMENDED_STRATEGY_WEIGHTS` against what's
described below.** The local copy this was built on documents 32 prior
strategies (#1-32, including Myfxbook Sentiment, Climax Reversal at S/R, MTR
Range/Trend Regime, HTF Zone + M/W Reversal, Smart Money Sweep Morning/Night,
and Fibonacci Confluence S/R). If the VPS's actual numbering, strategy set,
or any of these files' structure differs from that — because something else
was synced or changed there independently and not yet pulled back down —
**stop and report the discrepancy instead of guessing**; adapt the line
numbers/anchors below to match what you actually find, but do not silently
overwrite or reorder unrelated strategies.

**Hard dependency**: this strategy reads `data["fib_confluence"]`, which is
only present if strategy #32 (Fibonacci Confluence S/R,
`VPS_SYNC_FIB_CONFLUENCE_SR_PROMPT.md`) has already been synced to this VPS
copy. If `fib_confluence.py` / `get_fib_confluence_safe()` /
`data["fib_confluence"]` do not exist yet on the VPS, **sync that strategy
first** — this one is additive on top of it and will still degrade
gracefully without it (the Fib-alignment bonus just never fires), but the
intent is for both to be live together.

## What this strategy does

Detects the 6 "classic" XABCD harmonic patterns — Gartley, Bat, Butterfly,
Crab, Deep Crab, and Cypher — from the last 4 confirmed zigzag swing points
on H1, projects each pattern's Potential Reversal Zone (PRZ — point D), and
cross-checks that PRZ against the existing Fibonacci Confluence S/R engine
(`fib_confluence.py`, strategy #32) so a PRZ that also lines up with an
independently-computed major/minor-swing Fib confluence zone scores higher —
this is the "use data from the Fibonacci strategy to make the signal more
accurate" behavior the user explicitly asked for, implemented by
cross-referencing two independently-computed structures via the shared
`data` dict (no direct dependency of one module's internals on the other's).
It only votes once price is AT the best-matched pattern's PRZ AND prints a
rejection candle there (pin bar or engulfing) — a PRZ alone is "a location
tool... rather than a standalone buy or sell signal," per the user's
reference doc. Needs only H1 OHLC + `atr14`, already present every scan.

It also writes to a brand-new local SQLite file,
`harmonic_patterns_history.db` (separate from `macro_data_history.db` and
`fib_confluence_history.db`), with one table, `harmonic_pattern_history`
(one row per scan: the X/A/B/C swing points and every matched pattern, for
after-the-fact "why did the bot think this was a Bat pattern" audits).
Best-effort/never-raises, same pattern as `macro_data.py`'s `_save_to_db()`.

## Step 0 — create the new file `harmonic_patterns.py`

This file does **not** exist on the VPS yet (it's a brand-new standalone
module — only `numpy`/`pandas`/stdlib, no circular-import risk). Create it
at the root of the live bot folder with this exact content:

```python
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

_RATIO_TOL = 0.07          # tolerance band added around each textbook ratio
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
```

## Step 1 — `strategies.py`

Add the scoring function. Paste it immediately before `STRATEGY_REGISTRY =
{` (i.e. after `score_fib_confluence_sr`, or after whichever is the last
strategy function on the VPS copy):

```python
def score_harmonic_patterns(data, proximity_atr=0.5):
    """33rd strategy -- Harmonic Patterns (XABCD) ★. Ported in from the new
    harmonic_patterns.py module (Gartley/Bat/Butterfly/Crab/Deep Crab/Cypher
    XABCD pattern detection + PRZ projection -- see that module's docstring
    for the full ratio tables and PRZ math). Reads data["harmonic"] (built
    once per scan by get_harmonic_patterns_safe() in
    xauusd_mt5_strategy.py) and only votes once price is at the
    best-matched pattern's PRZ AND prints a rejection candle there.
    Cross-checks each PRZ against data["fib_confluence"] (the 32nd
    strategy) for an extra confluence bonus -- this is the "combine with
    the existing Fibonacci strategy" behavior the user asked for. Scores
    0/0 gracefully if the snapshot is missing/errored or no pattern is
    currently near price."""
    snap = data.get("harmonic")
    if not snap or snap.get("error"):
        reason = snap.get("error") if snap else "harmonic snapshot unavailable"
        return {"long": 0.0, "short": 0.0, "note": f"harmonic patterns: {reason}"}

    df = data.get(snap.get("timeframe", "h1"))
    if df is None or len(df) < 3:
        return {"long": 0.0, "short": 0.0, "note": "insufficient data for entry-candle check"}

    atr_now = snap.get("atr_now")
    if not atr_now or pd.isna(atr_now):
        atr_now = float((df["high"] - df["low"]).tail(20).mean())
    atr_now = max(float(atr_now), 1e-6)

    matches = [m for m in (snap.get("matches") or [])
               if abs(df["low" if snap.get("direction") == "bullish" else "high"].iloc[-1] - m["prz_price"]) <= atr_now * proximity_atr
               or abs(df["close"].iloc[-1] - m["prz_price"]) <= atr_now * proximity_atr]
    if not matches:
        return {"long": 0.0, "short": 0.0, "note": "no harmonic PRZ within range of current price"}

    best = max(matches, key=lambda m: m["confluence_score"])

    last, prev = df.iloc[-1], df.iloc[-2]
    rng = max(last["high"] - last["low"], 1e-6)
    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])
    prev_body_low, prev_body_high = min(prev["open"], prev["close"]), max(prev["open"], prev["close"])
    cur_bullish, cur_bearish = last["close"] > last["open"], last["close"] < last["open"]
    prev_bearish, prev_bullish = prev["close"] < prev["open"], prev["close"] > prev["open"]

    long_score = short_score = 0.0
    fib_note = " (Fib-confluence aligned)" if best.get("fib_aligned") else ""
    base_note = (f"{best['pattern']} PRZ {best['prz_price']:.2f} "
                 f"({best['n_confirm']} ratio confirm., score {best['confluence_score']:.0f}){fib_note}")

    if snap.get("direction") == "bullish":
        is_pin = lower_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = cur_bullish and prev_bearish and last["open"] <= prev_body_low and last["close"] >= prev_body_high
        if is_pin or is_engulf:
            shape = "bullish pin bar" if is_pin else "bullish engulfing"
            quality = (lower_wick / rng) if is_pin else (body / rng)
            long_score = _clip(best["confluence_score"] * 0.6 + quality * 30)
            note = f"{shape} at {base_note}"
        else:
            note = f"price at {base_note} but no rejection candle yet"
    else:
        is_pin = upper_wick >= rng * 0.5 and body <= rng * 0.4
        is_engulf = cur_bearish and prev_bullish and last["open"] >= prev_body_high and last["close"] <= prev_body_low
        if is_pin or is_engulf:
            shape = "bearish pin bar" if is_pin else "bearish engulfing"
            quality = (upper_wick / rng) if is_pin else (body / rng)
            short_score = _clip(best["confluence_score"] * 0.6 + quality * 30)
            note = f"{shape} at {base_note}"
        else:
            note = f"price at {base_note} but no rejection candle yet"

    return {"long": long_score, "short": short_score, "note": note}
```

`_clip` is the same helper already used by every other strategy function in
this file — do not redefine it, just call it.

Then add the registry entry. Find the end of `STRATEGY_REGISTRY = {` (it
should currently end with the `fib_confluence_sr` entry). Add, right before
the closing `}`:

```python
    # ---- 33rd: Harmonic Patterns (XABCD) ★. Ported in from the new
    # harmonic_patterns.py module (Gartley/Bat/Butterfly/Crab/Deep
    # Crab/Cypher XABCD pattern detection + PRZ projection, per the user's
    # uploaded reference doc -- see that module's docstring for the full
    # ratio tables and PRZ math). Reads data["harmonic"] (built once per
    # scan by get_harmonic_patterns_safe() in xauusd_mt5_strategy.py) and
    # only votes once price is at the best-matched pattern's PRZ AND prints
    # a rejection candle there. Cross-checks each PRZ against
    # data["fib_confluence"] (the 32nd strategy) for an extra confluence
    # bonus -- this is the "combine with the existing Fibonacci strategy"
    # behavior the user asked for. Scores 0/0 gracefully if the snapshot is
    # missing/errored or no pattern is currently near price.
    "harmonic_patterns": ("Harmonic Patterns (XABCD) ★", score_harmonic_patterns),
}
```

## Step 2 — `xauusd_mt5_strategy.py`

**2a.** Near the top, find `import fib_confluence` and add immediately
after it:

```python
import harmonic_patterns
```

**2b.** Find `def get_fib_confluence_safe(data):` and its full function
body. Add a new sibling function immediately after it (before
`def _mtr_is_danger(data):`, if that function exists on the VPS copy —
otherwise immediately before `def build_market_data():`):

```python
def get_harmonic_patterns_safe(data):
    """Wraps harmonic_patterns.compute_harmonic_patterns() so a problem in
    the XABCD harmonic-pattern engine can never break a scan -- same
    try/except pattern as get_fib_confluence_safe() above. Must run AFTER
    data["fib_confluence"] has already been set (see build_market_data()
    below) because compute_harmonic_patterns() cross-checks each pattern's
    PRZ against the fib_confluence snapshot. On any error this returns None
    and score_harmonic_patterns() treats that exactly like
    macro_bias/fib_confluence-unsupported: a graceful 0/0."""
    try:
        return harmonic_patterns.compute_harmonic_patterns(data)
    except Exception:
        logger.exception("harmonic_patterns.compute_harmonic_patterns() failed — harmonic_patterns strategy will score 0/0 this scan.")
        return None
```

**2c.** Find `def build_market_data():` and the line that sets
`data["fib_confluence"] = get_fib_confluence_safe(data)` (added by the prior
sync). **Order matters**: add the new line immediately AFTER that one,
before the `return data` statement — for example, if it currently ends with:

```python
    data["fib_confluence"] = get_fib_confluence_safe(data)
    return data
```

change it to:

```python
    data["fib_confluence"] = get_fib_confluence_safe(data)
    data["harmonic"] = get_harmonic_patterns_safe(data)
    return data
```

(If the VPS's actual `build_market_data()` structure differs from this —
e.g. it doesn't yet have the `fib_confluence` line because strategy #32
hasn't been synced — sync that first; see the "Hard dependency" note at the
top of this file.)

**2d.** Find `_RECOMMENDED_STRATEGY_WEIGHTS = {` and its closing `}`
(should currently end with `"fib_confluence_sr": 1.2, ...`). Add one new
entry:

```python
    "fib_confluence_sr": 1.2,  # 32nd -- H4 major-swing + H1 minor-swing Fibonacci confluence + rejection candle
    "harmonic_patterns": 1.3,  # 33rd -- XABCD harmonic pattern PRZ (Gartley/Bat/Butterfly/Crab/Deep Crab/Cypher) + Fib-confluence cross-check + rejection candle
}
```

## Step 3 — `strategy_config_ui.py`

**3a.** Find the end of `DEFAULT_CONFIG["confluence"]["strategies"]` (should
currently end with the `fib_confluence_sr` entry). Add, right before the
closing `},`:

```python
            "fib_confluence_sr": {"enabled": True, "weight": 1.2},
            # 33rd: Harmonic Patterns (XABCD) -- Gartley/Bat/Butterfly/Crab/
            # Deep Crab/Cypher PRZ detection, cross-checked against the
            # fib_confluence_sr zones for an accuracy bonus, entry gated on
            # a rejection candle at the PRZ.
            "harmonic_patterns": {"enabled": True, "weight": 1.3},
        },
    },
```

**3b.** Find `STRATEGY13_LABELS = {` and its line for `fib_confluence_sr`.
Add, right after it:

```python
    "fib_confluence_sr": "32. Fibonacci Confluence S/R (Major+Minor Swing) ★",
    "harmonic_patterns": "33. Harmonic Patterns (XABCD: Gartley/Bat/Butterfly/Crab/Cypher) ★",
}
```

## Step 4 — `generate_dashboard.py`

Two small text-only changes (the table itself is generated dynamically from
`strategy_scores.json`, so it will automatically show all 33 rows once the
EA produces them — these are just the header/comment text):

- `    # ---- confluence multi-strategy (32) scores ----` → change `(32)`
  to `(33)`
- `  <h2>Multi-Strategy Confluence (32) — Live Scores</h2>` → change `(32)`
  to `(33)`

(If the VPS copy currently shows a different number than 32 in these two
spots, that means it's already out of sync with the local copy in some other
way — report that instead of assuming 32 → 33.)

## Step 5 — `CLAUDE.md`

Find the `## Strategies (32 total)` section header and change it to
`## Strategies (33 total)`. Then, immediately before the `## Hard rules`
section, add:

```markdown
And a 33rd: **Harmonic Patterns (XABCD)** (`score_harmonic_patterns` in
`strategies.py`, key `harmonic_patterns`, weight `1.3`). Built from the
user's uploaded `Harmonic Patterns.docx`, with the explicit requirement to
combine it with the existing Fibonacci Confluence S/R strategy (#32) for a
more accurate signal. New standalone module, `harmonic_patterns.py` (only
numpy/pandas/stdlib):
  - **Swing detection**: a dedicated ATR-based zigzag (`_zigzag_points()`,
    different algorithm from `fib_confluence`'s window-based swing finder —
    harmonic patterns need strictly alternating *confirmed* high/low
    pivots, not just any local extrema) finds the last 4 confirmed pivots
    on H1 and labels them X→A→B→C, chronologically.
  - **6 classic patterns**: Gartley, Bat, Butterfly, Crab, Deep Crab
    (ratio-band matching on AB/XA, BC/AB, CD/BC against each pattern's
    textbook Fibonacci ratios, with a small tolerance), and Cypher (handled
    separately — its CD ratio retraces XC, not BC, a genuinely different
    structure per the doc).
  - **PRZ (Potential Reversal Zone)**: each match projects point D two
    independent ways (from the XD ratio, and from the CD extension of BC)
    and scores higher the more tightly those two projections converge —
    the doc's "PRZ convergence" concept.
  - **Combined with the Fibonacci strategy**: each pattern's PRZ is
    cross-checked against `data["fib_confluence"]`'s nearest
    support/resistance zone (the 32nd strategy); a PRZ that also lines up
    with an independently-computed Fib confluence zone gets
    `fib_aligned=True` and a confluence-score bonus.
  - **Entry trigger**: only votes once price is at the best-matched
    pattern's PRZ AND prints a rejection candle there (same pin-bar/
    engulfing check as `score_fib_confluence_sr`/`score_climax_reversal_sr`).
  - **New local price database**: `harmonic_patterns_history.db`, with one
    `harmonic_pattern_history` row per scan, best-effort/never-raises.
  - Synced from the local `RoBotTrading man 0 V10` working copy on
    [today's VPS date] via this prompt.
```

## Verification before calling this done

1. Syntax-check all 5 touched files (`python -m py_compile
   harmonic_patterns.py strategies.py strategy_config_ui.py
   xauusd_mt5_strategy.py generate_dashboard.py`).
2. Run a quick synthetic logic test directly against
   `harmonic_patterns.compute_harmonic_patterns()` and
   `score_harmonic_patterns()` (no MT5 needed — both only touch pandas
   DataFrames): build a synthetic H1 OHLC series (250+ bars) with a clean
   XABCD zigzag engineered into the tail (a Gartley-ratio leg is easiest:
   AB ≈ 61.8% XA, BC ≈ 50% AB), run it through `strategies.enrich()`, call
   `compute_harmonic_patterns({"h1": df_h1, "fib_confluence": {...}})`,
   confirm it returns a dict with `points` (X/A/B/C), `matches` (a non-empty
   list including the matched pattern with a `prz_price` and
   `confluence_score`), and `best_match` (no `"error"` key). Then confirm
   `score_harmonic_patterns({})` and
   `score_harmonic_patterns({"harmonic": {"error": "x"}})` both return a
   graceful `{"long": 0.0, "short": 0.0, ...}` with an explanatory note
   instead of raising. This was already verified on the local copy with
   exactly this approach (a synthetic Gartley pattern matched with PRZ
   correctly projected and both graceful-degradation paths returning 0/0)
   — just confirm the VPS copy behaves identically after the edit.
3. Confirm `strategy_config.json` on the VPS still loads correctly after a
   restart (the new key should backfill via `_deep_merge` the same way
   `fib_confluence_sr`/`climax_reversal_sr`/etc. did) — check the UI's
   strategies tab shows "33. Harmonic Patterns (XABCD: ...) ★" as a new
   checkbox/weight row.
4. Confirm `harmonic_patterns_history.db` gets created in the bot's working
   directory and that the `harmonic_pattern_history` table exists after the
   first live scan (or the synthetic test in step 2).
5. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, any other
   strategy's weight, or any risk/lot parameter as part of this sync — this
   is purely additive, matching the local copy exactly.
6. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running — apply the edits, syntax-check, run the synthetic
   test, and report back; let the user decide when to restart so the new
   strategy actually takes effect in the live scan.
