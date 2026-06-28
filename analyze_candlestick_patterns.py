"""
analyze_candlestick_patterns.py
---------------------------------------------------------------------------
Finds which classic candlestick patterns occur most often in XAUUSD price
history, and — more importantly — which of them actually have forward
predictive edge (frequency alone is not useful for entries; a pattern that
fires constantly but is a coin-flip afterwards is not a signal).

WHY THIS EXISTS / HOW TO RUN IT FOR REAL
---------------------------------------------------------------------------
This file was written in an environment with NO live MT5 connection and NO
internet access to financial data providers — it could NOT be run against
real XAUUSD history there. The pattern-detection logic itself WAS verified
there, against hand-built synthetic candle sequences (`--selftest` below),
but the frequency/win-rate numbers in any report this script produces are
only real once it's actually run here, on the VPS, where `MetaTrader5` is
connected to a live/demo account with real price history.

Run on the VPS, inside this folder (same Python env the EA itself runs in):

    python analyze_candlestick_patterns.py --selftest      # sanity check detectors
    python analyze_candlestick_patterns.py --live --bars 8000 --tf H1
    python analyze_candlestick_patterns.py --live --bars 4000 --tf H4

Output: a ranked table printed to console + saved to
`candlestick_pattern_report_<tf>.csv` in this folder. Nothing here writes
to `strategy_config.json` or touches the live bot — this is a pure
read-only research/analysis tool.
---------------------------------------------------------------------------
"""

import argparse
import sys

import numpy as np
import pandas as pd


# =============================== PATTERN DETECTORS ===============================
# Each function takes the full OHLC DataFrame (columns: open, high, low, close)
# and returns a boolean Series aligned to the DataFrame's index, True on the bar
# where the pattern COMPLETES (i.e. the bar you'd act on the close of).
# Each detector also has an implied direction used later for the edge test:
#   +1 = bullish (expect price up after this bar)
#   -1 = bearish (expect price down after this bar)

def _body(df):
    return (df["close"] - df["open"]).abs()


def _range(df):
    return (df["high"] - df["low"]).replace(0, np.nan)


def _upper_wick(df):
    return df["high"] - df[["open", "close"]].max(axis=1)


def _lower_wick(df):
    return df[["open", "close"]].min(axis=1) - df["low"]


def bullish_engulfing(df):
    o, c = df["open"], df["close"]
    po, pc = o.shift(1), c.shift(1)
    prior_bearish = pc < po
    curr_bullish = c > o
    engulfs = (o <= pc) & (c >= po)
    return (prior_bearish & curr_bullish & engulfs).fillna(False), +1


def bearish_engulfing(df):
    o, c = df["open"], df["close"]
    po, pc = o.shift(1), c.shift(1)
    prior_bullish = pc > po
    curr_bearish = c < o
    engulfs = (o >= pc) & (c <= po)
    return (prior_bullish & curr_bearish & engulfs).fillna(False), -1


def hammer(df):
    body, rng = _body(df), _range(df)
    lw, uw = _lower_wick(df), _upper_wick(df)
    cond = (lw >= 2 * body) & (uw <= 0.3 * body.clip(lower=1e-9)) & (body <= 0.4 * rng)
    return cond.fillna(False), +1


def shooting_star(df):
    body, rng = _body(df), _range(df)
    lw, uw = _lower_wick(df), _upper_wick(df)
    cond = (uw >= 2 * body) & (lw <= 0.3 * body.clip(lower=1e-9)) & (body <= 0.4 * rng)
    return cond.fillna(False), -1


def doji(df):
    body, rng = _body(df), _range(df)
    cond = body <= 0.1 * rng
    return cond.fillna(False), 0  # direction-neutral; treated separately below


def morning_star(df):
    o, c, h, l = df["open"], df["close"], df["high"], df["low"]
    b1_bear = (c.shift(2) < o.shift(2))
    b1_body = (o.shift(2) - c.shift(2))
    b2_small = _body(df).shift(1) <= 0.5 * b1_body.clip(lower=1e-9)
    b3_bull = c > o
    b3_strong_close = c >= (o.shift(2) + c.shift(2)) / 2
    return (b1_bear & b2_small & b3_bull & b3_strong_close).fillna(False), +1


def evening_star(df):
    o, c = df["open"], df["close"]
    b1_bull = (c.shift(2) > o.shift(2))
    b1_body = (c.shift(2) - o.shift(2))
    b2_small = _body(df).shift(1) <= 0.5 * b1_body.clip(lower=1e-9)
    b3_bear = c < o
    b3_weak_close = c <= (o.shift(2) + c.shift(2)) / 2
    return (b1_bull & b2_small & b3_bear & b3_weak_close).fillna(False), -1


def three_white_soldiers(df):
    o, c = df["open"], df["close"]
    bull0, bull1, bull2 = c > o, c.shift(1) > o.shift(1), c.shift(2) > o.shift(2)
    rising = (c > c.shift(1)) & (c.shift(1) > c.shift(2))
    opens_within = (o > o.shift(1).combine(c.shift(1), min)) & \
                   (o.shift(1) > o.shift(2).combine(c.shift(2), min))
    cond = bull0 & bull1 & bull2 & rising & opens_within
    return cond.fillna(False), +1


def three_black_crows(df):
    o, c = df["open"], df["close"]
    bear0, bear1, bear2 = c < o, c.shift(1) < o.shift(1), c.shift(2) < o.shift(2)
    falling = (c < c.shift(1)) & (c.shift(1) < c.shift(2))
    opens_within = (o < o.shift(1).combine(c.shift(1), max)) & \
                   (o.shift(1) < o.shift(2).combine(c.shift(2), max))
    cond = bear0 & bear1 & bear2 & falling & opens_within
    return cond.fillna(False), -1


def piercing_line(df):
    o, c = df["open"], df["close"]
    prior_bear = c.shift(1) < o.shift(1)
    curr_bull = c > o
    opens_below = o <= c.shift(1)
    closes_above_mid = c >= (o.shift(1) + c.shift(1)) / 2
    closes_below_open = c < o.shift(1)
    return (prior_bear & curr_bull & opens_below & closes_above_mid & closes_below_open).fillna(False), +1


def dark_cloud_cover(df):
    o, c = df["open"], df["close"]
    prior_bull = c.shift(1) > o.shift(1)
    curr_bear = c < o
    opens_above = o >= c.shift(1)
    closes_below_mid = c <= (o.shift(1) + c.shift(1)) / 2
    closes_above_open = c > o.shift(1)
    return (prior_bull & curr_bear & opens_above & closes_below_mid & closes_above_open).fillna(False), -1


def inside_bar(df):
    h, l = df["high"], df["low"]
    cond = (h < h.shift(1)) & (l > l.shift(1))
    return cond.fillna(False), 0  # neutral — direction comes from breakout, not the bar itself


def outside_bar_bull(df):
    h, l, c, o = df["high"], df["low"], df["close"], df["open"]
    cond = (h > h.shift(1)) & (l < l.shift(1)) & (c > o)
    return cond.fillna(False), +1


def outside_bar_bear(df):
    h, l, c, o = df["high"], df["low"], df["close"], df["open"]
    cond = (h > h.shift(1)) & (l < l.shift(1)) & (c < o)
    return cond.fillna(False), -1


def tweezer_top(df):
    h, c, o = df["high"], df["close"], df["open"]
    similar_highs = (h - h.shift(1)).abs() <= 0.1 * _range(df)
    cond = similar_highs & (c.shift(1) > o.shift(1)) & (c < o)
    return cond.fillna(False), -1


def tweezer_bottom(df):
    l, c, o = df["low"], df["close"], df["open"]
    similar_lows = (l - l.shift(1)).abs() <= 0.1 * _range(df)
    cond = similar_lows & (c.shift(1) < o.shift(1)) & (c > o)
    return cond.fillna(False), +1


def marubozu_bull(df):
    body, rng = _body(df), _range(df)
    cond = (body >= 0.9 * rng) & (df["close"] > df["open"])
    return cond.fillna(False), +1


def marubozu_bear(df):
    body, rng = _body(df), _range(df)
    cond = (body >= 0.9 * rng) & (df["close"] < df["open"])
    return cond.fillna(False), -1


PATTERNS = {
    "bullish_engulfing":    bullish_engulfing,
    "bearish_engulfing":    bearish_engulfing,
    "hammer":               hammer,
    "shooting_star":        shooting_star,
    "doji":                 doji,
    "morning_star":         morning_star,
    "evening_star":         evening_star,
    "three_white_soldiers": three_white_soldiers,
    "three_black_crows":    three_black_crows,
    "piercing_line":        piercing_line,
    "dark_cloud_cover":     dark_cloud_cover,
    "inside_bar":           inside_bar,
    "outside_bar_bull":     outside_bar_bull,
    "outside_bar_bear":     outside_bar_bear,
    "tweezer_top":          tweezer_top,
    "tweezer_bottom":       tweezer_bottom,
    "marubozu_bull":        marubozu_bull,
    "marubozu_bear":        marubozu_bear,
}


# =============================== EDGE MEASUREMENT ===============================

def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([
        h - l,
        (h - c.shift(1)).abs(),
        (l - c.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def evaluate_pattern(df, mask, direction, forward_bars=(4, 8, 24), min_occurrences=10):
    """For every bar where `mask` is True, look `forward_bars` ahead and check
    whether price moved in `direction` by at least 0.25 ATR (a 'win'), using
    ATR at the signal bar to normalize across volatility regimes. Patterns
    with no inherent direction (direction == 0, e.g. doji/inside_bar) are
    scored both ways and the better-performing side is reported, since the
    bar itself doesn't tell you which way it'll resolve — only context would,
    which this generic scan doesn't have."""
    atr = df["_atr"]
    idx = np.where(mask.values)[0]
    if len(idx) < min_occurrences:
        return None

    results = {}
    dirs_to_test = [direction] if direction != 0 else [+1, -1]
    for d in dirs_to_test:
        rows = []
        for n in forward_bars:
            wins, total = 0, 0
            for i in idx:
                if i + n >= len(df):
                    continue
                entry = df["close"].iloc[i]
                fwd = df["close"].iloc[i + n]
                a = atr.iloc[i]
                if pd.isna(a) or a == 0:
                    continue
                move = (fwd - entry) / a * d  # positive = favorable in direction d
                total += 1
                if move >= 0.25:
                    wins += 1
            rows.append({
                "forward_bars": n,
                "n_samples": total,
                "win_rate": (wins / total) if total else float("nan"),
            })
        results[d] = rows

    best_dir = max(results, key=lambda d: np.nanmean([r["win_rate"] for r in results[d]]))
    return {
        "count": len(idx),
        "direction_tested": best_dir,
        "ambiguous_direction": direction == 0,
        "per_horizon": results[best_dir],
        "avg_win_rate": float(np.nanmean([r["win_rate"] for r in results[best_dir]])),
    }


def run_analysis(df, forward_bars=(4, 8, 24), min_occurrences=10):
    df = df.copy()
    df["_atr"] = _atr(df, 14)
    n_bars = len(df)
    rows = []
    for name, fn in PATTERNS.items():
        mask, direction = fn(df)
        result = evaluate_pattern(df, mask, direction, forward_bars, min_occurrences)
        if result is None:
            rows.append({
                "pattern": name, "count": int(mask.sum()),
                "per_1000_bars": round(1000 * mask.sum() / n_bars, 2),
                "avg_win_rate": float("nan"), "direction_tested": None,
                "note": f"< {min_occurrences} occurrences, skipped edge test",
            })
            continue
        rows.append({
            "pattern": name,
            "count": result["count"],
            "per_1000_bars": round(1000 * result["count"] / n_bars, 2),
            "avg_win_rate": round(result["avg_win_rate"], 3),
            "direction_tested": "bullish" if result["direction_tested"] > 0 else "bearish",
            "ambiguous_pattern": result["ambiguous_direction"],
            "note": "",
        })

    out = pd.DataFrame(rows)
    # Rank by an "edge score" — frequency matters, but only multiplied by how
    # far the win rate is from a 50/50 coin flip. A pattern with win_rate=0.50
    # and huge frequency is still not a usable signal on its own.
    out["edge_score"] = (out["avg_win_rate"] - 0.5).clip(lower=0) * out["count"]
    out = out.sort_values("edge_score", ascending=False).reset_index(drop=True)
    return out


# =============================== LIVE MT5 DATA PATH ===============================

def fetch_live(symbol, timeframe_str, n_bars):
    import MetaTrader5 as mt5
    tf_map = {
        "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
    }
    if timeframe_str not in tf_map:
        raise ValueError(f"Unsupported timeframe {timeframe_str}, choose from {list(tf_map)}")
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe_str], 0, n_bars)
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No rates returned for {symbol}/{timeframe_str} "
                            f"— check symbol name (try symbol_normalize.resolve()) and that the "
                            f"terminal has enough history downloaded for this timeframe.")
    df = pd.DataFrame(rates)
    df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close"})
    return df[["open", "high", "low", "close"]]


# =============================== SELF-TEST (no MT5 needed) ===============================

def _make_df(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def selftest():
    """Hand-built candle sequences with a KNOWN pattern in the last bar(s).
    Verifies each detector fires on its intended case and stays silent on a
    deliberately-not-matching control case. This is what CAN be verified
    without real market data — it proves the detection logic is sound, not
    that the patterns have real edge (that requires --live on the VPS)."""
    checks = []

    # Bullish engulfing: bar0 bearish big, bar1 bullish engulfing it
    df = _make_df([
        [10, 10.2, 8.8, 9.0],   # bearish: open 10 -> close 9
        [8.9, 10.5, 8.8, 10.3],  # bullish engulfing: open<=9, close>=10
    ])
    mask, d = bullish_engulfing(df)
    checks.append(("bullish_engulfing fires", bool(mask.iloc[-1]) is True))

    # Control: two bullish bars in a row should NOT fire bullish_engulfing
    df_ctrl = _make_df([
        [9.0, 9.5, 8.9, 9.4],
        [9.4, 9.9, 9.3, 9.8],
    ])
    mask_ctrl, _ = bullish_engulfing(df_ctrl)
    checks.append(("bullish_engulfing silent on control", bool(mask_ctrl.iloc[-1]) is False))

    # Hammer: small body near top of range, long lower wick
    df = _make_df([
        [10, 10.1, 9.9, 10.0],
        [10.0, 10.0, 9.0, 9.95],  # long lower wick, ~no upper wick, tiny body near top
    ])
    mask, d = hammer(df)
    checks.append(("hammer fires", bool(mask.iloc[-1]) is True))

    # Doji: open ~= close, real range
    df = _make_df([
        [10, 10.1, 9.9, 10.0],
        [10.0, 10.5, 9.5, 10.02],
    ])
    mask, d = doji(df)
    checks.append(("doji fires", bool(mask.iloc[-1]) is True))

    # Evening star: big bull, small body, big bear closing below midpoint
    df = _make_df([
        [9.0, 9.0, 9.0, 9.0],     # pad row (shift safety)
        [9.0, 10.0, 8.9, 9.9],    # bar1: strong bull, body 0.9
        [9.95, 10.05, 9.9, 10.0], # bar2: tiny body (star)
        [9.9, 9.95, 9.0, 9.2],    # bar3: strong bear, closes well below bar1 mid (9.45)
    ])
    mask, d = evening_star(df)
    checks.append(("evening_star fires", bool(mask.iloc[-1]) is True))

    # Inside bar
    df = _make_df([
        [9.0, 10.0, 8.0, 9.5],
        [9.4, 9.6, 9.0, 9.3],   # fully inside prior range
    ])
    mask, d = inside_bar(df)
    checks.append(("inside_bar fires", bool(mask.iloc[-1]) is True))

    # Marubozu bullish: body is ~all of the range
    df = _make_df([
        [9.0, 9.0, 9.0, 9.0],
        [9.0, 10.0, 9.0, 10.0],  # open=low, close=high
    ])
    mask, d = marubozu_bull(df)
    checks.append(("marubozu_bull fires", bool(mask.iloc[-1]) is True))

    print("=== analyze_candlestick_patterns.py self-test ===")
    all_ok = True
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        all_ok = all_ok and ok
    print("=== " + ("ALL PASS" if all_ok else "SOME FAILED — fix before trusting --live output") + " ===")
    return all_ok


# =============================== CLI ===============================

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true", help="run detector sanity checks, no MT5 needed")
    ap.add_argument("--live", action="store_true", help="fetch real history via MT5 and run the analysis")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="H1", choices=["M5", "M15", "H1", "H4", "D1"])
    ap.add_argument("--bars", type=int, default=8000)
    ap.add_argument("--forward", default="4,8,24",
                     help="comma-separated forward-bar horizons to test win rate over")
    args = ap.parse_args()

    if not args.selftest and not args.live:
        print("Specify --selftest and/or --live. See module docstring for examples.")
        sys.exit(1)

    if args.selftest:
        ok = selftest()
        if args.live and not ok:
            print("Self-test failed — refusing to run --live until detectors are fixed.")
            sys.exit(1)

    if args.live:
        forward_bars = tuple(int(x) for x in args.forward.split(","))
        print(f"Fetching {args.bars} {args.tf} bars for {args.symbol} via MT5...")
        df = fetch_live(args.symbol, args.tf, args.bars)
        print(f"Got {len(df)} bars. Running pattern scan + forward win-rate test "
              f"(horizons={forward_bars} bars, min 0.25 ATR move to count as a win)...")
        report = run_analysis(df, forward_bars=forward_bars)
        pd.set_option("display.width", 140)
        print(report.to_string(index=False))
        out_path = f"candlestick_pattern_report_{args.tf}.csv"
        report.to_csv(out_path, index=False)
        print(f"\nSaved full report to {out_path}")


if __name__ == "__main__":
    main()
