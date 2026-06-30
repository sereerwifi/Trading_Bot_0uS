"""
Diagnostic script — checks which Big Data / macro sources are actually
reachable from THIS machine (e.g. your VPS), since each fetch_* function in
macro_data.py swallows exceptions and silently returns None on failure (by
design, so a blocked feed never crashes the EA). This script bypasses that
and prints the real exception for every source so you can see exactly which
ones are blocked and why.

Run on the VPS with:
    python diagnose_macro_data.py
"""

import traceback
import macro_data as md

CHECKS = [
    ("DXY (Dollar Index)",            md._fetch_dxy_raw),
    ("US 10Y Treasury Yield",         md._fetch_yield10y_raw),
    ("Fed Expectation (2Y proxy)",    md._fetch_fed_expectation_raw),
    ("CFTC COT Report",               lambda: md._fetch_cot_raw("GOLD")),
    ("ETF Flow (GLD)",                lambda: md._fetch_etf_flow_raw("GLD")),
    ("COMEX Inventory",               lambda: md._fetch_comex_inventory_raw("GOLD")),
    ("Economic Calendar",             md._fetch_calendar_raw),
]

if __name__ == "__main__":
    print("=" * 70)
    print("Macro data source connectivity check")
    print("=" * 70)

    results = {}
    for name, fn in CHECKS:
        print(f"\n--- {name} ---")
        try:
            result = fn()
            if result is None:
                print("  -> Returned None (request likely succeeded but data "
                      "didn't parse as expected, or a soft bot-wall was hit).")
                results[name] = "EMPTY"
            else:
                print(f"  -> OK: {result}")
                results[name] = "OK"
        except Exception as exc:
            print(f"  -> FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=2)
            results[name] = "ERROR"

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    for name, status in results.items():
        print(f"  {status:6s}  {name}")

    failed = [n for n, s in results.items() if s != "OK"]
    if failed:
        print(f"\n{len(failed)} source(s) not returning data: {', '.join(failed)}")
        print("If these work on your home/dev machine but fail here, the VPS's "
              "outbound firewall or hosting provider is likely blocking those "
              "specific domains. Try opening the failing URLs in a browser on "
              "the VPS itself to confirm.")
    else:
        print("\nAll sources returned data successfully.")
