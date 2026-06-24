"""
verify_data_sources.py
======================
Health-check for every macro data source used by the XAUUSD bot.
Exit code 0 = all sources healthy.
Exit code 1 = one or more sources failed or are stale.

Usage:
    python verify_data_sources.py          # normal run
    python verify_data_sources.py --fix    # clear stale cache before checking
    python verify_data_sources.py --quiet  # only print failures

Wiring into Task Scheduler:
    Action: python "C:\\...\\verify_data_sources.py"
    Add --fix to force a refresh on each scheduled run.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)
import macro_data as m

PASS = "✅"   # green check
FAIL = "❌"   # red cross
WARN = "⚠️"  # warning

# Maximum age (seconds) before a cached value is considered stale.
# Mirrors 2x the CACHE_TTL so a single missed refresh isn't an alert.
STALE_LIMITS = {
    "dxy":            m.CACHE_TTL["dxy"]            * 2,
    "yield10y":       m.CACHE_TTL["yield10y"]        * 2,
    "fed_expectation":m.CACHE_TTL["fed_expectation"] * 2,
    "cot_gold":       m.CACHE_TTL["cot"]             * 2,
    "etf_gld":        m.CACHE_TTL["etf_flow"]        * 2,
    "comex_gold":     m.CACHE_TTL["comex"]           * 2,
    "calendar":       m.CACHE_TTL["calendar"]        * 2,
}

results = []   # list of (name, status, detail)


def check(name, fetch_fn, required_fields, stale_key=None):
    """Call fetch_fn(), verify required_fields present and non-None,
    check cache age. Appends to global results."""
    t0 = time.time()
    data = None
    err  = None
    try:
        data = fetch_fn()
    except Exception as exc:
        err = exc
    elapsed = time.time() - t0

    # --- check return value ---
    if err is not None:
        results.append((name, "FAIL", f"raised {type(err).__name__}: {err}  ({elapsed:.1f}s)"))
        return
    if data is None:
        results.append((name, "FAIL", f"returned None  ({elapsed:.1f}s)"))
        return

    # --- check required fields ---
    missing = [f for f in required_fields if data.get(f) is None]
    if missing:
        results.append((name, "FAIL",
            f"missing fields {missing} — got: "
            f"{json.dumps({k:v for k,v in data.items() if k!='fetched_at'}, default=str)[:200]}"))
        return

    # --- check staleness from disk cache ---
    stale_msg = ""
    if stale_key:
        cache = m._load_cache()
        entry = cache.get(stale_key)
        if entry and not entry.get("failed"):
            age = time.time() - entry.get("ts", 0)
            limit = STALE_LIMITS.get(stale_key, 86400)
            if age > limit:
                stale_msg = f"  [{WARN} STALE {age/3600:.1f}h > limit {limit/3600:.0f}h]"

    # --- build summary line ---
    summary_fields = {k: v for k, v in data.items()
                      if k not in ("fetched_at", "events") and v is not None}
    # truncate long values
    for k in list(summary_fields):
        if isinstance(summary_fields[k], str) and len(summary_fields[k]) > 60:
            summary_fields[k] = summary_fields[k][:57] + "..."
    src = data.get("source", data.get("proxy", ""))
    src_tag = f"[{src}] " if src else ""
    results.append((name, "PASS",
        f"{src_tag}{json.dumps(summary_fields, default=str)[:180]}  ({elapsed:.1f}s){stale_msg}"))


def check_calendar(name, stale_key):
    """Special check for the calendar — verifies event count and upcoming USD events."""
    t0 = time.time()
    data = None
    err  = None
    try:
        data = m.fetch_economic_calendar()
    except Exception as exc:
        err = exc
    elapsed = time.time() - t0

    if err is not None or data is None:
        results.append((name, "FAIL", f"{'raised '+str(err) if err else 'returned None'}  ({elapsed:.1f}s)"))
        return

    events = data.get("events", [])
    if not events:
        results.append((name, "FAIL", f"returned empty event list  ({elapsed:.1f}s)"))
        return

    high_usd = [e for e in events if e.get("impact") == "High" and e.get("country") == "USD"]
    stale_msg = ""
    cache = m._load_cache()
    entry = cache.get(stale_key)
    if entry and not entry.get("failed"):
        age = time.time() - entry.get("ts", 0)
        limit = STALE_LIMITS.get(stale_key, 86400)
        if age > limit:
            stale_msg = f"  [{WARN} STALE {age/3600:.1f}h > limit {limit/3600:.0f}h]"

    upcoming = m.upcoming_high_impact_events(data, within_minutes=1440)  # 24h
    results.append((name, "PASS",
        f"{len(events)} events this week | {len(high_usd)} High-USD | "
        f"{len(upcoming)} within 24h  ({elapsed:.1f}s){stale_msg}"))


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix",   action="store_true", help="Clear stale/failed cache before checking")
    parser.add_argument("--quiet", action="store_true", help="Only print failures")
    args = parser.parse_args()

    if args.fix:
        cache = m._load_cache()
        cleared = []
        for key in list(cache.keys()):
            if cache[key].get("failed"):
                del cache[key]
                cleared.append(key)
        if cleared:
            m._save_cache(cache)
            print(f"[fix] Cleared {len(cleared)} failed cache entries: {cleared}")

    print(f"\n{'='*70}")
    print(f"  MACRO DATA SOURCE HEALTH CHECK  [{datetime.now():%Y-%m-%d %H:%M:%S}]")
    print(f"{'='*70}\n")

    check("DXY (Dollar Index)",
          m.fetch_dxy,
          ["latest", "prev", "change"],
          stale_key="dxy")

    check("US 10Y Treasury Yield",
          m.fetch_yield_10y,
          ["latest", "prev", "change"],
          stale_key="yield10y")

    check("Fed Expectation (2Y proxy)",
          m.fetch_fed_expectation,
          ["latest", "prev", "change"],
          stale_key="fed_expectation")

    check("COT Report — GOLD",
          lambda: m.fetch_cot_report("GOLD"),
          ["managed_money_net_long", "managed_money_net_long_change", "open_interest"],
          stale_key="cot_gold")

    check("COMEX Inventory (or COT proxy)",
          lambda: m.fetch_comex_inventory("GOLD"),
          ["registered_oz", "eligible_oz"],
          stale_key="comex_gold")

    check("ETF Flow — GLD (or Yahoo proxy)",
          lambda: m.fetch_etf_flow("GLD"),
          ["change_tonnes"],
          stale_key="etf_gld")

    check_calendar("Economic Calendar (ForexFactory)",
                   stale_key="calendar")

    # ---------------------------------------------------------------------------
    # Print results
    # ---------------------------------------------------------------------------
    failures = [r for r in results if r[1] == "FAIL"]
    passes   = [r for r in results if r[1] == "PASS"]

    for name, status, detail in results:
        icon = PASS if status == "PASS" else FAIL
        if args.quiet and status == "PASS":
            continue
        label = f"{icon} {name}"
        print(f"  {label:<40} {detail}")

    print(f"\n{'='*70}")
    print(f"  Result: {len(passes)}/{len(results)} sources healthy"
          f"  |  {len(failures)} failure(s)")
    print(f"{'='*70}\n")

    if failures:
        print("FAILED sources:")
        for name, _, detail in failures:
            print(f"  {FAIL} {name}: {detail}")
        print()
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
