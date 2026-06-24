"""
strategy_simulator.py — continuous per-strategy shadow ("paper") trading.
====================================================================
Implements the "use all result of strategy than test or simulation" half of
the ML/decision-making feature: every strategy gets a virtual position
opened the moment its own long/short score crosses the vote threshold —
regardless of whether it's currently a real-trade contributor, down-weighted,
or fully benched — so the League System always has fresh win/loss data to
judge it on, even during a losing streak when it isn't trading for real.

This module places NO real orders and touches MT5 in no way. It only reads
the tick price + ATR that the EA already fetched this scan, and tracks
purely-virtual entry/SL/TP levels in a local JSON state file. Zero real
risk; it exists purely to keep feeding league.py's win-rate history.

Mechanics
---------
Each scan (run once per confluence scan, unconditionally — same cadence as
real scoring):
  1. For every strategy with an OPEN virtual position, check the current
     bid/ask against its stored SL/TP. If hit, record the result via
     league.record_result() and close the virtual position.
  2. For every strategy WITHOUT an open virtual position, check this scan's
     score: if long or short crosses strategies.DEFAULT_VOTE_THRESHOLD,
     open a new virtual position sized the same way real confluence trades
     are (entry = tick price, SL/TP = ATR * CONFLUENCE_SL_ATR_MULT /
     CONFLUENCE_TP_RR).

Because checks only happen once per scan (not tick-by-tick), this is an
approximation of real fill behavior — fine for the purpose (a rolling
win-rate signal), not a perfectly precise backtest. A stale-position
safety valve (MAX_SHADOW_AGE_HOURS) force-closes any virtual position that
never reaches SL/TP within a reasonable window, so a flat/ranging market
can't leave a strategy's history stuck mid-trade forever.

State is persisted to shadow_positions.json next to this file:
{
  "order_block": {"direction": "long", "entry": 2345.6, "sl": 2340.1,
                   "tp": 2356.5, "opened_at": "2026-06-23T10:15:00"},
  ...
}
"""

import json
import os
from datetime import datetime

import league

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shadow_positions.json")

# Safety valve: force-close (score at whatever side of breakeven price
# currently sits on) any virtual position that's been open this long without
# hitting SL/TP, so one frozen position can't block a strategy's win-rate
# history from updating indefinitely in a dead/flat market.
MAX_SHADOW_AGE_HOURS = 48


def load_state(path=STATE_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state, path=STATE_PATH):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def _check_close(pos, bid, ask):
    """Returns True/False (win/loss) if this scan's price closed the virtual
    position, else None (still open)."""
    price = bid if pos["direction"] == "long" else ask
    if pos["direction"] == "long":
        if price <= pos["sl"]:
            return False
        if price >= pos["tp"]:
            return True
    else:
        if price >= pos["sl"]:
            return False
        if price <= pos["tp"]:
            return True
    return None


def update_all(shadow_state, league_state, scores, bid, ask, atr_now,
               sl_atr_mult, tp_rr, vote_threshold, now=None):
    """Runs one shadow-simulation step for every strategy present in
    `scores` (strategies.score_all()'s "scores" dict for THIS scan — reused
    as-is, so the simulator never re-derives a strategy's signal separately
    from what real trading just saw).

    Mutates `shadow_state` and `league_state` in place. Caller is
    responsible for persisting both (strategy_simulator.save_state() and
    league.save_state()) after calling this.

    Returns a list of (strategy_key, won, reason) for anything that closed
    this scan — useful for logging only.
    """
    now = now or datetime.now()
    closed = []

    for key, s in scores.items():
        pos = shadow_state.get(key)

        if pos is not None:
            won = _check_close(pos, bid, ask)

            if won is None:
                try:
                    opened_at = datetime.fromisoformat(pos["opened_at"])
                except (KeyError, ValueError):
                    opened_at = now
                if (now - opened_at).total_seconds() > MAX_SHADOW_AGE_HOURS * 3600:
                    if pos["direction"] == "long":
                        won = bid > pos["entry"]
                    else:
                        won = ask < pos["entry"]
                    closed.append((key, won, "stale shadow position force-closed"))
            else:
                closed.append((key, won, "shadow SL/TP hit"))

            if won is not None:
                league.record_result(league_state, key, won)
                del shadow_state[key]
                pos = None

        if pos is None:
            long_score = s.get("long", 0.0) or 0.0
            short_score = s.get("short", 0.0) or 0.0
            direction = None
            if long_score >= vote_threshold and long_score >= short_score:
                direction = "long"
            elif short_score >= vote_threshold:
                direction = "short"

            if direction and atr_now and atr_now > 0:
                entry_price = ask if direction == "long" else bid
                sl_distance = atr_now * sl_atr_mult
                if direction == "long":
                    sl = entry_price - sl_distance
                    tp = entry_price + sl_distance * tp_rr
                else:
                    sl = entry_price + sl_distance
                    tp = entry_price - sl_distance * tp_rr
                shadow_state[key] = {
                    "direction": direction,
                    "entry": entry_price,
                    "sl": sl,
                    "tp": tp,
                    "opened_at": now.isoformat(),
                }

    return closed


def status_snapshot(shadow_state):
    """Flat list of currently-open virtual positions, for dashboard/debug
    display."""
    return [
        {"key": key, **pos}
        for key, pos in shadow_state.items()
    ]
