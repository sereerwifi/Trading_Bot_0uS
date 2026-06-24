"""
League System — per-strategy performance tracking and auto-bench.
====================================================================
Tracks a rolling win/loss record for each of the 13 strategies and benches
(temporarily disables the influence of) any strategy that triggers EITHER
configured rule:
    1. N consecutive losses in a row, OR
    2. rolling win-rate over its last M trades falls below a floor %

Both N/M and the win-rate floor, and the bench duration, are configurable
(read from strategy_config.json by the caller — see LEAGUE_* settings in
xauusd_mt5_strategy.py). This module only manages the state file; it does
not read config itself, so it stays simple/testable standalone.

State is persisted to strategy_league.json next to this file so it survives
restarts. Structure:
{
  "order_block": {
      "results": [true, false, true, ...],   # True = win, oldest->newest, capped
      "consecutive_losses": 0,
      "benched_until": null | "2026-06-21T19:30:00"
  },
  ...
}
"""

import json
import os
from datetime import datetime, timedelta

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_league.json")
MAX_HISTORY_PER_STRATEGY = 100  # cap stored results so the file doesn't grow forever


def _empty_entry():
    return {"results": [], "consecutive_losses": 0, "benched_until": None}


def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state, path=STATE_PATH):
    import time as _time
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    for attempt in range(3):
        try:
            os.replace(tmp_path, path)
            break
        except PermissionError:
            if attempt == 2:
                raise
            _time.sleep(0.05)


def is_benched(state, strategy_key, now=None):
    entry = state.get(strategy_key)
    if not entry or not entry.get("benched_until"):
        return False
    now = now or datetime.now()
    try:
        until = datetime.fromisoformat(entry["benched_until"])
    except ValueError:
        return False
    return now < until


def record_trade_result(state, strategy_key, won, max_consecutive_losses, min_winrate_pct,
                         winrate_lookback_trades, bench_hours, now=None):
    """Updates one strategy's record after a trade attributed to it closes.
    Applies BOTH bench rules (whichever trips first wins — bench duration is
    the same either way, set by `bench_hours`). Returns the updated entry."""
    now = now or datetime.now()
    entry = state.setdefault(strategy_key, _empty_entry())

    entry["results"].append(bool(won))
    entry["results"] = entry["results"][-MAX_HISTORY_PER_STRATEGY:]

    if won:
        entry["consecutive_losses"] = 0
    else:
        entry["consecutive_losses"] = entry.get("consecutive_losses", 0) + 1

    should_bench = False
    reason = None

    if max_consecutive_losses is not None and entry["consecutive_losses"] >= max_consecutive_losses:
        should_bench = True
        reason = f"{entry['consecutive_losses']} consecutive losses"

    lookback = entry["results"][-winrate_lookback_trades:] if winrate_lookback_trades else entry["results"]
    if min_winrate_pct is not None and len(lookback) >= max(3, (winrate_lookback_trades or 0) // 2):
        winrate = 100.0 * sum(lookback) / len(lookback)
        if winrate < min_winrate_pct:
            should_bench = True
            wr_reason = f"win-rate {winrate:.1f}% over last {len(lookback)} trades < floor {min_winrate_pct}%"
            reason = f"{reason} + {wr_reason}" if reason else wr_reason

    if should_bench and bench_hours:
        entry["benched_until"] = (now + timedelta(hours=bench_hours)).isoformat()
        entry["bench_reason"] = reason

    state[strategy_key] = entry
    return entry, (reason if should_bench else None)


def record_result(state, strategy_key, won, max_history=MAX_HISTORY_PER_STRATEGY):
    """Lightweight, append-only result recording — no bench side effects.

    Used by strategy_simulator.py to keep building win/loss history for a
    strategy from continuous shadow/paper trades, even while that strategy
    is currently down-weighted or fully benched and isn't taking real
    trades. This is what makes recovery possible: the same `auto_weight()`
    below reads this same "results" list, so a strategy whose simulated
    results climb back above the win-rate floor is restored automatically
    on the very next scan — no fixed cooldown to wait out.

    Real trade closes should keep using record_trade_result() instead,
    which appends to this same list AND applies the consecutive-loss /
    time-based bench rules on top."""
    entry = state.setdefault(strategy_key, _empty_entry())
    entry["results"].append(bool(won))
    entry["results"] = entry["results"][-max_history:]
    state[strategy_key] = entry
    return entry


def auto_weight(state, strategy_key, min_winrate_pct, lookback_trades, min_samples=5):
    """Continuous, performance-based weight multiplier in [0.0, 1.0],
    recomputed fresh every scan from ALL recorded results for this strategy
    — real trade closes AND shadow/simulated closes combined (whatever is
    in `entry["results"]`). This is the "use all result of strategy than
    test or simulation" behavior: there is no separate bucket for
    simulated vs. real outcomes, they all feed the same rolling win-rate.

    Rules:
      - Fewer than `min_samples` results yet over the lookback window
        -> 1.0 (don't judge a strategy on too little data)
      - rolling win-rate >= min_winrate_pct -> 1.0 (full weight)
      - rolling win-rate <  min_winrate_pct -> winrate / min_winrate_pct
        (scales smoothly toward 0.0 as win-rate approaches 0%, so a badly
        broken strategy is effectively disabled while one just barely
        under the floor only loses a little influence)

    Because this reads the live results list on every call, the moment
    new results (real or shadow) push the rolling win-rate back to/above
    `min_winrate_pct`, the multiplier snaps back to 1.0 on the very next
    scan — recovery is performance-driven, not time-driven."""
    if min_winrate_pct is None or min_winrate_pct <= 0:
        return 1.0
    entry = state.get(strategy_key)
    if not entry or not entry.get("results"):
        return 1.0
    lookback = entry["results"][-lookback_trades:] if lookback_trades else entry["results"]
    if len(lookback) < max(1, min_samples):
        return 1.0
    wr = 100.0 * sum(lookback) / len(lookback)
    if wr >= min_winrate_pct:
        return 1.0
    return max(0.0, wr / min_winrate_pct)


def winrate(state, strategy_key, lookback=None):
    entry = state.get(strategy_key)
    if not entry or not entry.get("results"):
        return None
    results = entry["results"][-lookback:] if lookback else entry["results"]
    if not results:
        return None
    return 100.0 * sum(results) / len(results)


def status_snapshot(state, now=None, min_winrate_pct=None, winrate_lookback_trades=None, min_samples=5):
    """Returns a flat list of dicts for dashboard display: one row per
    strategy currently tracked, with win-rate, streak, and bench status.
    If `min_winrate_pct` is supplied, also includes the live `auto_weight`
    multiplier (see auto_weight() above) for transparency on the
    dashboard."""
    now = now or datetime.now()
    rows = []
    for key, entry in state.items():
        row = {
            "key": key,
            "trades": len(entry.get("results", [])),
            "winrate": winrate(state, key),
            "consecutive_losses": entry.get("consecutive_losses", 0),
            "benched": is_benched(state, key, now),
            "benched_until": entry.get("benched_until"),
            "bench_reason": entry.get("bench_reason"),
        }
        if min_winrate_pct is not None:
            row["auto_weight"] = round(
                auto_weight(state, key, min_winrate_pct, winrate_lookback_trades, min_samples), 3
            )
        rows.append(row)
    return rows
