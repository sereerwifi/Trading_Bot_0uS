"""
backtest_sim.py — walk-forward shadow simulation over historical MT5 data.

Usage:
    python backtest_sim.py [days]      default: 30

Walks the last N days bar-by-bar at H1 cadence, scores all 24 strategies
at each bar, opens virtual long/short positions (entry = bar close, SL/TP
from ATR * config multipliers), and resolves them at subsequent bars using
OHLC high/low as an intrabar proxy.  No real orders placed.

Reports per strategy:
  - Bars scored (total bars where score was computed)
  - Fire count / fire rate  (score >= DEFAULT_VOTE_THRESHOLD)
  - Win / Loss / Pending
  - Win rate
  - Average score when the strategy fired
  - Direction split (long% vs short% of fires)
"""

import os
import sys
import warnings
warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Setup — ensure we can import the EA modules
# ---------------------------------------------------------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

import logging
logging.getLogger("xauusd_ea").disabled = True   # silence bot logger completely

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

import strategies
import xauusd_mt5_strategy as ea

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 30
BUFFER_DAYS = 12       # extra history so indicators are warm at sim start
VOTE_THRESHOLD = strategies.DEFAULT_VOTE_THRESHOLD   # 50.0
SL_ATR_MULT    = ea.CONFLUENCE_SL_ATR_MULT           # 1.5
TP_RR          = ea.CONFLUENCE_TP_RR                 # 2.0
MAX_HOLD_BARS  = 48    # force-close after this many H1 bars (=48h)

# ---------------------------------------------------------------------------
# MT5 connect & fetch
# ---------------------------------------------------------------------------
logging.getLogger("xauusd_ea").disabled = True
ea.load_ui_config()
logging.getLogger("xauusd_ea").disabled = True

if not mt5.initialize():
    print("ERROR: MT5 initialize() failed — is MetaTrader 5 running?")
    sys.exit(1)

SYMBOL = ea.SYMBOL
print(f"\nBacktest: {SYMBOL}  |  last {DAYS} days  |  SL={SL_ATR_MULT}xATR  |  TP={TP_RR}R\n")

END   = datetime.now()
START = END - timedelta(days=DAYS + BUFFER_DAYS)

TF = {
    "d1":  mt5.TIMEFRAME_D1,
    "h4":  mt5.TIMEFRAME_H4,
    "h1":  mt5.TIMEFRAME_H1,
    "m15": mt5.TIMEFRAME_M15,
    "m5":  mt5.TIMEFRAME_M5,
    "m1":  mt5.TIMEFRAME_M1,
}
LOOKBACK = {"d1": 260, "h4": 250, "h1": 300, "m15": 200, "m5": 200, "m1": 200}

def fetch(symbol, tf_id, start, end):
    rates = mt5.copy_rates_range(symbol, tf_id, start, end)
    if rates is None or len(rates) == 0:
        print(f"  WARNING: no data for {symbol} tf={tf_id}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return strategies.enrich(df)

print("Fetching historical data from MT5...")
all_tf = {}
for name, tf_id in TF.items():
    all_tf[name] = fetch(SYMBOL, tf_id, START, END)
    print(f"  {name}: {len(all_tf[name])} bars")

mt5.shutdown()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def slice_before(df, t, n):
    """Return the last `n` rows of df where time <= t."""
    sub = df[df["time"] <= t]
    return sub.tail(n).reset_index(drop=True) if not sub.empty else pd.DataFrame()

def build_data(t):
    data = {"now": t, "dom": None, "macro": None}
    for name, n in LOOKBACK.items():
        sl = slice_before(all_tf[name], t, n)
        data[name] = sl if len(sl) >= 5 else None
    return data

# ---------------------------------------------------------------------------
# Simulation state
# ---------------------------------------------------------------------------
# Per-strategy accumulators
ST = {k: {
    "bars":    0,
    "fires":   0,
    "long_fires": 0,
    "short_fires": 0,
    "wins":    0,
    "losses":  0,
    "pending": 0,
    "scores_when_fired": [],
    "open_pos": None,   # dict: {dir, entry, sl, tp, open_bar_i, score}
} for k in strategies.STRATEGY_REGISTRY}

# ---------------------------------------------------------------------------
# Walk forward on H1 bars
# ---------------------------------------------------------------------------
sim_start = END - timedelta(days=DAYS)
h1_bars = all_tf["h1"]
sim_bars = h1_bars[h1_bars["time"] >= sim_start].reset_index(drop=True)

print(f"\nSimulating {len(sim_bars)} H1 bars ({DAYS} days)...\n")

for i, bar in sim_bars.iterrows():
    t          = bar["time"]
    bar_open   = bar["open"]
    bar_high   = bar["high"]
    bar_low    = bar["low"]
    bar_close  = bar["close"]
    atr_now    = bar.get("atr14", None)
    if pd.isna(atr_now) or not atr_now:
        atr_now = None

    data = build_data(t)
    if data["h1"] is None or len(data["h1"]) < 50:
        continue

    # Score all strategies
    try:
        result = strategies.score_all(data)
        scores_dict = result["scores"]
    except Exception as exc:
        continue

    for key, s in scores_dict.items():
        st = ST.get(key)
        if st is None:
            continue
        st["bars"] += 1

        long_score  = s.get("long",  0.0) or 0.0
        short_score = s.get("short", 0.0) or 0.0
        best_score  = max(long_score, short_score)

        # --- Check open position ---
        pos = st["open_pos"]
        if pos is not None:
            d   = pos["dir"]
            age = i - pos["open_bar_i"]

            # OHLC proxy: check SL first (conservative), then TP
            hit = None
            if d == "long":
                if bar_low  <= pos["sl"]:  hit = False
                elif bar_high >= pos["tp"]: hit = True
            else:
                if bar_high >= pos["sl"]:  hit = False
                elif bar_low  <= pos["tp"]: hit = True

            # Force-close stale positions at bar close
            if hit is None and age >= MAX_HOLD_BARS:
                hit = bar_close > pos["entry"] if d == "long" else bar_close < pos["entry"]

            if hit is not None:
                if hit:
                    st["wins"]   += 1
                else:
                    st["losses"] += 1
                st["open_pos"] = None
                pos = None

        # --- Open new position if no open pos and score fires ---
        if pos is None and atr_now and best_score >= VOTE_THRESHOLD:
            direction = "long" if long_score >= short_score else "short"
            entry = bar_close
            sl_dist = atr_now * SL_ATR_MULT
            if direction == "long":
                sl = entry - sl_dist
                tp = entry + sl_dist * TP_RR
            else:
                sl = entry + sl_dist
                tp = entry - sl_dist * TP_RR
            st["open_pos"]         = {"dir": direction, "entry": entry, "sl": sl, "tp": tp, "open_bar_i": i}
            st["fires"]           += 1
            st["scores_when_fired"].append(best_score)
            if direction == "long":
                st["long_fires"]  += 1
            else:
                st["short_fires"] += 1

# Count pending (still-open) positions at end of sim
for key, st in ST.items():
    if st["open_pos"] is not None:
        st["pending"] += 1

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("=" * 110)
print(f"{'Strategy':<32} {'Bars':>5} {'Fires':>6} {'Fire%':>6} {'Long%':>6} {'Shrt%':>6} "
      f"{'W':>4} {'L':>4} {'Pend':>5} {'WR%':>6} {'AvgScr':>7}")
print("-" * 110)

# Sort by fire rate descending
rows = []
for key, st in ST.items():
    if st["bars"] == 0:
        continue
    fire_rate   = st["fires"] / st["bars"] * 100
    win_rate    = (st["wins"] / (st["wins"] + st["losses"]) * 100) if (st["wins"] + st["losses"]) > 0 else float("nan")
    avg_score   = np.mean(st["scores_when_fired"]) if st["scores_when_fired"] else 0.0
    long_pct    = (st["long_fires"]  / st["fires"] * 100) if st["fires"] > 0 else 0.0
    short_pct   = (st["short_fires"] / st["fires"] * 100) if st["fires"] > 0 else 0.0
    display     = strategies.STRATEGY_REGISTRY[key][0] if key in strategies.STRATEGY_REGISTRY else key
    rows.append((key, display, st["bars"], st["fires"], fire_rate, long_pct, short_pct,
                 st["wins"], st["losses"], st["pending"], win_rate, avg_score))

rows.sort(key=lambda r: r[4], reverse=True)  # sort by fire rate

for row in rows:
    key, display, bars, fires, fire_rate, lp, sp, w, l, pend, wr, avg = row
    wr_str  = f"{wr:.1f}%" if not np.isnan(wr) else "  n/a"
    warn    = ""
    if not np.isnan(wr) and wr < 40 and fires >= 5:
        warn = " ⚠"
    if fire_rate == 0:
        warn = " ✗"
    print(f"{display:<32} {bars:>5} {fires:>6} {fire_rate:>5.1f}% {lp:>5.1f}% {sp:>5.1f}% "
          f"{w:>4} {l:>4} {pend:>5} {wr_str:>6} {avg:>7.1f}{warn}")

print("=" * 110)
print()
print("Notes:")
print("  ⚠ = win rate < 40% with ≥5 completed trades (underperforming, review logic)")
print("  ✗ = never fired in this period (signal may be too restrictive or session-gated)")
print(f"  SL={SL_ATR_MULT}xATR  TP={TP_RR}R  Vote threshold={VOTE_THRESHOLD}  Max hold={MAX_HOLD_BARS}h")
print(f"  Period: {sim_bars['time'].iloc[0]} → {sim_bars['time'].iloc[-1]}  ({len(sim_bars)} H1 bars)")
print(f"  Position check: SL checked before TP within each bar (conservative OHLC proxy)")
print()

# Overall summary
total_w = sum(st["wins"]   for st in ST.values())
total_l = sum(st["losses"] for st in ST.values())
total_f = sum(st["fires"]  for st in ST.values())
total_p = sum(st["pending"] for st in ST.values())
overall_wr = total_w / (total_w + total_l) * 100 if (total_w + total_l) > 0 else 0
print(f"Overall (all strategies combined): {total_f} fires  {total_w}W / {total_l}L / {total_p} pending  WR={overall_wr:.1f}%")
