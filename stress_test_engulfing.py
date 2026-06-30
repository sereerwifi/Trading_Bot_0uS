"""
stress_test_engulfing.py
------------------------
Four checks against the H4 bullish_engulfing finding from
analyze_candlestick_patterns.py:

  1. Baseline drift control — is "always long" already at 55%?
  2. Per-horizon breakdown — does the edge hold at 4-bar, 8-bar AND 24-bar?
  3. Split-sample stability — first-half vs second-half win rate
  4. Date range — exactly how much history is 4000 H4 bars?

Read-only. Does not write to strategy_config.json or touch the live bot.
"""

import sys
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

SYMBOL   = "GOLD"
TF_STR   = "H4"
N_BARS   = 4000
HORIZONS = (4, 8, 24)
WIN_THRESHOLD_ATR = 0.25  # same as analyze_candlestick_patterns.py


# ---- reuse detector + ATR from the sibling script ----
sys.path.insert(0, r"C:\Users\Administrator\Desktop\RoBotTrading man 0 V10")
from analyze_candlestick_patterns import bullish_engulfing, _atr


def fetch_with_time(symbol, tf_str, n_bars):
    tf_map = {
        "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
    }
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(symbol, tf_map[tf_str], 0, n_bars)
    mt5.shutdown()
    df = pd.DataFrame(rates)
    df["datetime"] = pd.to_datetime(df["time"], unit="s")
    return df[["datetime", "open", "high", "low", "close"]]


def win_rate_for_mask(df, mask, direction, n_fwd):
    """Forward win rate for mask at a single horizon."""
    atr = df["_atr"]
    idx = np.where(mask.values)[0]
    wins = total = 0
    for i in idx:
        if i + n_fwd >= len(df):
            continue
        entry = df["close"].iloc[i]
        fwd   = df["close"].iloc[i + n_fwd]
        a     = atr.iloc[i]
        if pd.isna(a) or a == 0:
            continue
        move = (fwd - entry) / a * direction
        total += 1
        if move >= WIN_THRESHOLD_ATR:
            wins += 1
    return wins / total if total else float("nan"), total


def always_long_win_rate(df, n_fwd):
    """Baseline: go long on every bar, forward win rate at horizon n_fwd."""
    atr = df["_atr"]
    wins = total = 0
    for i in range(len(df) - n_fwd):
        entry = df["close"].iloc[i]
        fwd   = df["close"].iloc[i + n_fwd]
        a     = atr.iloc[i]
        if pd.isna(a) or a == 0:
            continue
        total += 1
        if (fwd - entry) / a >= WIN_THRESHOLD_ATR:
            wins += 1
    return wins / total if total else float("nan"), total


def stress_test_pattern(df, mask, direction, label):
    print(f"\n{'='*60}")
    print(f"Pattern: {label}  (n={int(mask.sum())})")
    print(f"{'='*60}")

    atr = df["_atr"]
    idx = np.where(mask.values)[0]
    n = len(idx)

    # ---- Check 2: per-horizon breakdown ----
    print("\n[2] Per-horizon win rate:")
    per_horizon = {}
    for h in HORIZONS:
        wr, total = win_rate_for_mask(df, mask, direction, h)
        per_horizon[h] = (wr, total)
        print(f"    {h:>2}-bar forward:  {wr:.3f}  (n={total})")

    avg_wr = np.nanmean([v[0] for v in per_horizon.values()])
    print(f"    Average:        {avg_wr:.3f}")

    # ---- Check 1: always-long baseline ----
    print("\n[1] Always-long baseline (same win threshold, same horizons):")
    baselines = {}
    for h in HORIZONS:
        bwr, btotal = always_long_win_rate(df, h)
        baselines[h] = bwr
        edge = per_horizon[h][0] - bwr
        print(f"    {h:>2}-bar:  always-long={bwr:.3f}  pattern={per_horizon[h][0]:.3f}  net_edge={edge:+.3f}")

    avg_baseline = np.nanmean(list(baselines.values()))
    avg_edge = avg_wr - avg_baseline
    print(f"    Average net edge vs baseline: {avg_edge:+.3f}")

    # ---- Check 3: split-sample stability ----
    mid = n // 2
    idx_first  = idx[:mid]
    idx_second = idx[mid:]

    def half_wr(half_idx):
        wins_all = total_all = 0
        for h in HORIZONS:
            for i in half_idx:
                if i + h >= len(df):
                    continue
                entry = df["close"].iloc[i]
                fwd   = df["close"].iloc[i + h]
                a     = atr.iloc[i]
                if pd.isna(a) or a == 0:
                    continue
                total_all += 1
                if (fwd - entry) / a * direction >= WIN_THRESHOLD_ATR:
                    wins_all += 1
        return wins_all / total_all if total_all else float("nan")

    wr_first  = half_wr(idx_first)
    wr_second = half_wr(idx_second)
    dt_first_end  = df["datetime"].iloc[idx_first[-1]]  if len(idx_first)  else "—"
    dt_second_start = df["datetime"].iloc[idx_second[0]] if len(idx_second) else "—"

    print(f"\n[3] Split-sample stability:")
    print(f"    First  half (n={len(idx_first)},  up to  {dt_first_end}):  win_rate={wr_first:.3f}")
    print(f"    Second half (n={len(idx_second)}, from {dt_second_start}):  win_rate={wr_second:.3f}")
    gap = abs(wr_first - wr_second)
    stability = "STABLE (gap < 0.05)" if gap < 0.05 else ("MARGINAL (gap 0.05-0.10)" if gap < 0.10 else "UNSTABLE (gap >= 0.10)")
    print(f"    Gap: {gap:.3f}  ->  {stability}")

    return avg_wr, avg_baseline, avg_edge, wr_first, wr_second


def main():
    print(f"Fetching {N_BARS} {TF_STR} bars for {SYMBOL} (including timestamps)...")
    df = fetch_with_time(SYMBOL, TF_STR, N_BARS)
    df["_atr"] = _atr(df, 14)

    # ---- Check 4: date range ----
    dt_min = df["datetime"].min()
    dt_max = df["datetime"].max()
    n_months = (dt_max - dt_min).days / 30.44
    print(f"\n[4] Date range of {N_BARS} H4 bars:")
    print(f"    From: {dt_min}  To: {dt_max}")
    print(f"    Span: ~{n_months:.1f} months ({n_months/12:.1f} years)")

    # ---- bullish_engulfing ----
    mask_be, dir_be = bullish_engulfing(df)
    avg_wr_be, avg_bl_be, avg_edge_be, wr1_be, wr2_be = stress_test_pattern(
        df, mask_be, dir_be, "bullish_engulfing (H4)")

    # ---- Summary verdict ----
    print(f"\n{'='*60}")
    print("VERDICT — bullish_engulfing on H4")
    print(f"{'='*60}")
    print(f"  Avg win rate (all horizons):     {avg_wr_be:.3f}")
    print(f"  Always-long baseline:            {avg_bl_be:.3f}")
    print(f"  Net edge after drift removal:    {avg_edge_be:+.3f}")
    print(f"  Split-sample gap:                {abs(wr1_be - wr2_be):.3f}")

    if avg_edge_be >= 0.03 and abs(wr1_be - wr2_be) < 0.10:
        verdict = "YES — edge survives drift removal AND split-sample test."
    elif avg_edge_be >= 0.01 and abs(wr1_be - wr2_be) < 0.10:
        verdict = "MARGINAL — small net edge after drift, stable across halves. Thin signal."
    elif avg_edge_be < 0.01:
        verdict = "NO — net edge after removing drift is near zero. Pattern does not outperform always-long."
    else:
        verdict = "NO — edge collapses in split-sample test. Regime-dependent, not robust."
    print(f"\n  Answer: {verdict}")


if __name__ == "__main__":
    main()
