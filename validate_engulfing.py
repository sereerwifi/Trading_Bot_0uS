"""
validate_engulfing.py
---------------------
Three rigorous corrections to the prior stress-test of bullish_engulfing/H4:

  1. 95% confidence intervals (normal approximation)
  2. Trend-filter baseline (close > SMA50) replacing always-long
  3. Signal independence / de-duplication (24-bar overlap check)

Read-only. Does not touch strategies.py, xauusd_mt5_strategy.py, or config.
"""

import sys, math
import numpy as np
import pandas as pd
import MetaTrader5 as mt5

sys.path.insert(0, r"C:\Users\Administrator\Desktop\RoBotTrading man 0 V10")
from analyze_candlestick_patterns import bullish_engulfing, _atr

SYMBOL   = "GOLD"
N_BARS   = 4000
HORIZONS = (4, 8, 24)
WIN_ATR  = 0.25
DEDUP_WINDOW = 24   # longest horizon — signals within this many bars share forward windows


def fetch(symbol, n):
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, n)
    mt5.shutdown()
    df = pd.DataFrame(rates)
    df["datetime"] = pd.to_datetime(df["time"], unit="s")
    return df[["datetime", "open", "high", "low", "close"]]


def ci95(p, n):
    """95% CI via normal approximation: p +/- 1.96*SE."""
    if n == 0 or math.isnan(p):
        return (float("nan"), float("nan"))
    se = math.sqrt(p * (1 - p) / n)
    return (p - 1.96 * se, p + 1.96 * se)


def winrate_for_indices(df, idx_list, direction, horizon):
    """Win rate for a given list of bar indices at one forward horizon."""
    atr = df["_atr"]
    wins = total = 0
    for i in idx_list:
        if i + horizon >= len(df):
            continue
        entry = df["close"].iloc[i]
        fwd   = df["close"].iloc[i + horizon]
        a     = atr.iloc[i]
        if pd.isna(a) or a == 0:
            continue
        total += 1
        if (fwd - entry) / a * direction >= WIN_ATR:
            wins += 1
    return (wins / total, total) if total else (float("nan"), 0)


def trend_filter_winrate(df, horizon, direction=1):
    """Win rate of going long only when close > SMA(50), same methodology."""
    sma50 = df["close"].rolling(50).mean()
    atr   = df["_atr"]
    wins  = total = 0
    for i in range(len(df)):
        if i + horizon >= len(df):
            continue
        if pd.isna(sma50.iloc[i]) or df["close"].iloc[i] <= sma50.iloc[i]:
            continue
        a = atr.iloc[i]
        if pd.isna(a) or a == 0:
            continue
        total += 1
        entry = df["close"].iloc[i]
        fwd   = df["close"].iloc[i + horizon]
        if (fwd - entry) / a * direction >= WIN_ATR:
            wins += 1
    return (wins / total, total) if total else (float("nan"), 0)


def dedup_indices(idx_arr, window):
    """Keep only the first signal in any cluster where consecutive signals
    are within `window` bars of each other. Returns the deduplicated indices."""
    if len(idx_arr) == 0:
        return idx_arr
    kept = [idx_arr[0]]
    for i in idx_arr[1:]:
        if i - kept[-1] > window:
            kept.append(i)
    return kept


def main():
    print(f"Fetching {N_BARS} H4 bars for {SYMBOL}...")
    df = fetch(SYMBOL, N_BARS)
    df["_atr"] = _atr(df, 14)
    df["sma50"] = df["close"].rolling(50).mean()

    mask, direction = bullish_engulfing(df)
    all_idx = np.where(mask.values)[0]
    n_raw = len(all_idx)

    dt_min, dt_max = df["datetime"].min(), df["datetime"].max()
    print(f"Date range: {dt_min} to {dt_max} ({(dt_max - dt_min).days / 30.44:.1f} months)")
    print(f"Raw bullish_engulfing signals: n={n_raw}")

    # =========================================================
    # PROBLEM 1 — 95% confidence intervals
    # =========================================================
    print("\n" + "="*60)
    print("PROBLEM 1 — 95% Confidence Intervals")
    print("="*60)

    # Overall average win rate (replicate prior result)
    all_wins = all_n = 0
    per_h_raw = {}
    for h in HORIZONS:
        wr, n = winrate_for_indices(df, all_idx, direction, h)
        per_h_raw[h] = (wr, n)
        all_wins += round(wr * n)
        all_n    += n

    overall_wr = all_wins / all_n if all_n else float("nan")
    ci_overall = ci95(overall_wr, all_n)
    print(f"\nOverall (avg across horizons):  {overall_wr:.3f}  95% CI [{ci_overall[0]:.3f}, {ci_overall[1]:.3f}]  n={all_n}")
    excludes_half = ci_overall[0] > 0.50
    print(f"  -> CI {'EXCLUDES' if excludes_half else 'DOES NOT EXCLUDE'} 0.50  ({'distinguishable from coin-flip' if excludes_half else 'NOT distinguishable from coin-flip at 95%'})")

    print("\nPer-horizon:")
    for h in HORIZONS:
        wr, n = per_h_raw[h]
        lo, hi = ci95(wr, n)
        excl = "* excludes 0.50" if lo > 0.50 else "  includes 0.50"
        print(f"  {h:>2}-bar:  {wr:.3f}  CI [{lo:.3f}, {hi:.3f}]  n={n}  {excl}")

    # Split-sample CIs
    mid = n_raw // 2
    idx_first  = all_idx[:mid]
    idx_second = all_idx[mid:]

    wins1 = n1 = wins2 = n2 = 0
    for h in HORIZONS:
        wr1, c1 = winrate_for_indices(df, idx_first,  direction, h)
        wr2, c2 = winrate_for_indices(df, idx_second, direction, h)
        wins1 += round(wr1 * c1); n1 += c1
        wins2 += round(wr2 * c2); n2 += c2
    wr1_avg = wins1 / n1 if n1 else float("nan")
    wr2_avg = wins2 / n2 if n2 else float("nan")
    ci1 = ci95(wr1_avg, n1)
    ci2 = ci95(wr2_avg, n2)
    dt_split = df["datetime"].iloc[idx_second[0]]
    print(f"\nSplit-sample:")
    print(f"  First  half (n signals={mid}, n_obs={n1}): {wr1_avg:.3f}  CI [{ci1[0]:.3f}, {ci1[1]:.3f}]")
    print(f"  Second half (n signals={n_raw-mid}, n_obs={n2}): {wr2_avg:.3f}  CI [{ci2[0]:.3f}, {ci2[1]:.3f}]  (from {dt_split})")
    print(f"  SE at n~{mid}: {math.sqrt(0.25/mid):.3f}  -- gap of {abs(wr1_avg-wr2_avg):.3f} is {abs(wr1_avg-wr2_avg)/math.sqrt(0.25/mid):.1f} SE")
    ci_overlap = ci1[0] < ci2[1] and ci2[0] < ci1[1]  # rough overlap check
    print(f"  CIs overlap? {'YES -- halves not distinguishable from each other' if ci_overlap else 'NO -- halves are distinguishable'}")

    # =========================================================
    # PROBLEM 2 — trend-filter baseline (close > SMA50)
    # =========================================================
    print("\n" + "="*60)
    print("PROBLEM 2 — Trend-Filter Baseline (close > SMA50)")
    print("="*60)

    print("\nPattern vs always-long vs trend-filter (close>SMA50):")
    print(f"  {'Horizon':>7}  {'Pattern':>8}  {'Always-Long':>12}  {'TrendFilter':>12}  {'vsAlwaysLong':>13}  {'vsTrendFilter':>14}")

    always_long_wrs = {}
    trend_filter_wrs = {}
    for h in HORIZONS:
        # always-long
        w = t = 0
        for i in range(len(df) - h):
            a = df["_atr"].iloc[i]
            if pd.isna(a) or a == 0: continue
            t += 1
            if (df["close"].iloc[i+h] - df["close"].iloc[i]) / a >= WIN_ATR:
                w += 1
        al_wr = w / t if t else float("nan")
        always_long_wrs[h] = al_wr

        tf_wr, tf_n = trend_filter_winrate(df, h, direction)
        trend_filter_wrs[h] = tf_wr

        pat_wr = per_h_raw[h][0]
        vs_al  = pat_wr - al_wr
        vs_tf  = pat_wr - tf_wr
        print(f"  {h:>5}-bar  {pat_wr:>8.3f}  {al_wr:>12.3f}  {tf_wr:>12.3f}  {vs_al:>+13.3f}  {vs_tf:>+14.3f}")

    avg_vs_tf = np.nanmean([per_h_raw[h][0] - trend_filter_wrs[h] for h in HORIZONS])
    avg_vs_al = np.nanmean([per_h_raw[h][0] - always_long_wrs[h]  for h in HORIZONS])
    print(f"\n  Average net edge vs always-long:    {avg_vs_al:+.3f}")
    print(f"  Average net edge vs trend-filter:   {avg_vs_tf:+.3f}")

    # =========================================================
    # PROBLEM 3 — signal independence / de-duplication
    # =========================================================
    print("\n" + "="*60)
    print(f"PROBLEM 3 — Signal Independence (dedup window={DEDUP_WINDOW} bars)")
    print("="*60)

    gaps = np.diff(all_idx)
    n_within_window = int((gaps <= DEDUP_WINDOW).sum())
    print(f"\nGap distribution between consecutive signals:")
    print(f"  Min gap: {gaps.min():.0f} bars  Median: {np.median(gaps):.0f}  Max: {gaps.max():.0f}")
    print(f"  Signals within {DEDUP_WINDOW} bars of prior signal: {n_within_window} / {n_raw-1} ({100*n_within_window/(n_raw-1):.0f}%)")

    dedup_idx = dedup_indices(all_idx, DEDUP_WINDOW)
    n_dedup = len(dedup_idx)
    print(f"\n  Raw n={n_raw}  ->  De-duplicated effective n={n_dedup}  (kept {100*n_dedup/n_raw:.0f}%)")

    print(f"\nDe-duplicated win rates:")
    dedup_wins = dedup_n = 0
    for h in HORIZONS:
        wr, n = winrate_for_indices(df, dedup_idx, direction, h)
        lo, hi = ci95(wr, n)
        excl = "* excludes 0.50" if lo > 0.50 else "  includes 0.50"
        print(f"  {h:>2}-bar:  {wr:.3f}  CI [{lo:.3f}, {hi:.3f}]  n={n}  {excl}")
        if not math.isnan(wr):
            dedup_wins += round(wr * n); dedup_n += n
    dedup_avg = dedup_wins / dedup_n if dedup_n else float("nan")
    ci_dedup  = ci95(dedup_avg, dedup_n)
    print(f"  Average:  {dedup_avg:.3f}  CI [{ci_dedup[0]:.3f}, {ci_dedup[1]:.3f}]")
    print(f"  -> CI {'EXCLUDES' if ci_dedup[0] > 0.50 else 'DOES NOT EXCLUDE'} 0.50")

    # =========================================================
    # FINAL VERDICT
    # =========================================================
    print("\n" + "="*60)
    print("FINAL VERDICT")
    print("="*60)
    ci_ok      = ci_overall[0] > 0.50
    tf_edge_ok = avg_vs_tf > 0.02
    dedup_ok   = ci_dedup[0] > 0.50

    print(f"\n  [1] 95% CI on overall win rate excludes 0.50:       {'YES' if ci_ok else 'NO'}")
    print(f"  [2] Net edge over trend-filter baseline (avg):       {avg_vs_tf:+.3f}  ({'meaningful' if tf_edge_ok else 'negligible or negative'})")
    print(f"  [3] De-duplicated CI still excludes 0.50:            {'YES' if dedup_ok else 'NO'}  (eff-n={n_dedup})")

    if ci_ok and tf_edge_ok and dedup_ok:
        verdict = "YES — all three checks pass. Real edge, statistically distinguishable, adds to trend-filter."
    elif ci_ok and not tf_edge_ok and dedup_ok:
        verdict = "PARTIAL — statistically significant but does not beat a plain trend filter. This is gold being in an uptrend, not a candlestick edge."
    elif not ci_ok:
        verdict = "NO — win rate not distinguishable from 50% at 95% confidence. Do not implement."
    else:
        verdict = "NO — at least one critical check failed. Not ready for implementation."

    print(f"\n  Answer: {verdict}")


if __name__ == "__main__":
    main()
