# XAUUSD Bot — Full Code Audit Report (v2)
**Date:** 2026-06-30  
**Files audited:** 22 Python files  
**Findings:** 19 total — 2 HIGH, 5 MEDIUM, 7 LOW, 5 INFO

All 37 findings from `AUDIT_REPORT_2026-06-30.md` are already fixed and are NOT re-reported here.

---

## HIGH

### #1 — HIGH — xauusd_mt5_strategy.py — `_debug_scores_db_connect()`
**Buggy:**
```python
conn = sqlite3.connect(STRATEGY_SCORES_HISTORY_DB_PATH)
conn.execute("""CREATE TABLE IF NOT EXISTS strategy_scores_history ...""")
```
**Problem:** `strategy_scores_history.db` opens without `PRAGMA journal_mode=WAL`. The bot writes to this DB every scan (~30 s) via `log_all_strategy_scores_debug()` while `generate_dashboard.py` and `get_strategy_scores_history()` read it concurrently. Without WAL, concurrent reads during a write return "database is locked". Both sibling DBs (`fib_confluence_history.db`, `harmonic_patterns_history.db`) already enable WAL.  
**Fix:** Add `conn.execute("PRAGMA journal_mode=WAL")` immediately after `sqlite3.connect()`.

---

### #2 — HIGH — macro_data.py — `_db_connect()`
**Buggy:**
```python
conn = sqlite3.connect(DB_FILE, timeout=10)
conn.execute("""CREATE TABLE IF NOT EXISTS macro_history ...""")
```
**Problem:** `macro_data_history.db` also lacks WAL mode. The bot calls `_save_to_db()` on every successful macro fetch while `generate_dashboard.py` reads it via `get_macro_history()`. Same race condition as #1.  
**Fix:** Add `conn.execute("PRAGMA journal_mode=WAL")` after `sqlite3.connect()`.

---

## MEDIUM

### #3 — MEDIUM — xauusd_mt5_strategy.py:2107 — `get_strategy_scores_history()` connection leak
**Buggy:**
```python
try:
    conn = _debug_scores_db_connect()
    ...
    rows = cur.fetchall()
    conn.close()   # skipped on any exception above
    ...
except Exception:
    logger.exception(...)
    return []
```
**Problem:** If any exception is raised after the connection is opened, `conn.close()` is skipped — the connection leaks. Unlike `fib_confluence.get_price_bars()` and `harmonic_patterns.get_pattern_history()`, which both use `conn = None` + `finally`, this function has no `finally` block.  
**Fix:** Assign `conn = None` before the `try`, move `conn.close()` into `finally: if conn is not None: conn.close()`.

---

### #4 — MEDIUM — harmonic_patterns.py:230 — `cd_ratio_implied` miscalculation in `_match_classic()`
**Buggy:**
```python
cd_ratio_implied = _ratio(d_from_xd - C, C - B)
cd_confirmed = _in_band(cd_ratio_implied, spec["cd_bc"])
...
n_confirm = sum([
    _in_band(ab_xa, spec["ab_xa"]),
    _in_band(bc_ab, spec["bc_ab"]),
    bool(cd_confirmed),
])
```
**Problem:** `cd_ratio_implied` measures how far the XD-projected D is from C relative to C-B. This ratio is **determined by the XD spec**, not by the CD leg, so `cd_confirmed` is not an independent check — it trivially passes whenever XD ratio is in spec, inflating `n_confirm` by 1 and adding 12 points to `confluence_score` for patterns that only partially match. The CD/BC band is already baked into `d_from_cd`'s construction via `cd_mid`. The real independent check is already captured by `tightness` (PRZ convergence).  
**Fix:** Remove `cd_ratio_implied`, `cd_confirmed`, and `bool(cd_confirmed)` from `n_confirm`. Keep `cd_bc_implied` in the return dict for debugging.

---

### #5 — MEDIUM — macro_data.py:898 — Myfxbook password exposed in exception
**Buggy:**
```python
url = ("https://www.myfxbook.com/api/login.json?"
       + urllib.parse.urlencode({"email": email, "password": password}))
raw = _http_get(url)   # exception propagates with URL in traceback
```
**Problem:** If `_http_get(url)` raises a network exception, the URL (containing the password) propagates up through `_get_myfxbook_session()` into `_cached()`'s `except` block, which logs `f"...fetch failed ({type(fetch_err).__name__}: {fetch_err})..."`. Some exception types embed the full URL in `str(exc)`. Violates CLAUDE.md: "Never log the Myfxbook email/password."  
**Fix:** Wrap `_http_get(url)` in a try/except that scrubs the URL before re-raising.

---

### #6 — MEDIUM — backtest_sim.py:67 — `build_data()` references `LOOKBACK` defined only inside `__main__`
**Buggy:**
```python
def build_data(t):
    data = {"now": t, "dom": None, "macro": None}
    for name, n in LOOKBACK.items():   # NameError if called outside __main__
        sl = slice_before(all_tf[name], t, n)  # NameError on all_tf too
```
**Problem:** `LOOKBACK` is only assigned inside the `if __name__ == "__main__":` block (line 110). Any caller that imports `build_data` and calls it will get `NameError: name 'LOOKBACK' is not defined`. `LOOKBACK` is a static constant dict that doesn't depend on MT5 or live data.  
**Fix:** Move `LOOKBACK = {"d1": 260, ...}` to module level.

---

### #7 — MEDIUM — backtest_sim.py:35,41 — MT5 and EA imported at module level cause side effects
**Buggy:**
```python
import MetaTrader5 as mt5           # raises ImportError if MT5 not installed
import xauusd_mt5_strategy as ea    # triggers setup_logging(), file handler creation
```
**Problem:** Any script that `import backtest_sim` triggers MT5 initialization attempt and bot logger setup (rotating file handlers) even if no backtesting is done. The `__main__` guard was added to prevent side effects but these two imports still bypass it.  
**Fix:** Move `import MetaTrader5 as mt5` and `import xauusd_mt5_strategy as ea` inside the `if __name__ == "__main__":` block.

---

## LOW

### #8 — LOW — strategy_simulator.py:73 — `save_state()` missing PermissionError retry
**Buggy:**
```python
os.replace(tmp_path, path)   # no retry on Windows PermissionError
```
**Problem:** `shadow_positions.json` is written every scan (~30 s) — highest write frequency. `league.save_state()` has a 3-attempt retry for `PermissionError` (Windows file-locking). `strategy_simulator.save_state()` does not; an unhandled `PermissionError` loses that scan's paper-trade result.  
**Fix:** Add the same 3-attempt retry loop as `league.save_state()`.

---

### #9 — LOW — league.py:101 — local `winrate` shadows the module-level `winrate()` function
**Buggy:**
```python
winrate = 100.0 * sum(lookback) / len(lookback)
if winrate < min_winrate_pct:
```
**Problem:** `winrate` shadows the module-level function `winrate(state, key)` (line 171). A future caller adding `winrate(state, key)` inside this block would get `TypeError: 'float' object is not callable`.  
**Fix:** Rename to `winrate_val`.

---

### #10 — LOW — symbol_normalize.py:20 — dead entries in `_GOLD_ALIASES`
**Buggy:**
```python
_GOLD_ALIASES = {
    "GOLD", "XAUUSD", "XAUUSDM", "XAUUSDC", "XAUUSD.", "XAUUSD#", ...
}
```
**Problem:** `_clean()` strips everything except `[A-Z0-9/]`, so `"XAUUSD."` and `"XAUUSD#"` become `"XAUUSD"` before the set lookup — the dotted/hashed versions in the set can never be matched. They are already covered by the `startswith("XAUUSD")` fallback.  
**Fix:** Remove `"XAUUSD."` and `"XAUUSD#"` from `_GOLD_ALIASES`.

---

### #11 — LOW — verify_data_sources.py:47 — module-level `results` list accumulates across `main()` calls
**Buggy:**
```python
results = []   # module-level

def check(...):
    results.append(...)   # appends to module global

def main():
    ...   # no reset of results before use
```
**Problem:** A second call to `main()` (e.g., from a test runner) accumulates results from both runs and double-counts failures.  
**Fix:** Reset `results` at the top of `main()` with `global results; results = []`.

---

### #12 — LOW — backtest_sim.py:84 — duplicate `logging.disabled = True`
**Buggy:**
```python
if __name__ == "__main__":
    logging.getLogger("xauusd_ea").disabled = True   # dead — ea not yet imported
    ea.load_ui_config()                               # reconfigures logger
    logging.getLogger("xauusd_ea").disabled = True   # this one is needed
```
**Problem:** The first disable call (before `ea.load_ui_config()`) is dead code — `load_ui_config()` calls `setup_logging()` which re-enables the logger anyway. Only the second call (after) is effective.  
**Fix:** Remove the first `logging.getLogger("xauusd_ea").disabled = True`.

---

### #13 — LOW — telegram_alert.py:4 — docstring incorrectly claims dependency-free
**Buggy:**
```python
"""Uses only the standard library (urllib) so no extra `pip install` is needed..."""
import symbol_normalize  # project module — breaks import if cwd != project root
```
**Problem:** The module imports `symbol_normalize`, a project module. If `telegram_alert` is imported from a context where the project root is not in `sys.path`, `import symbol_normalize` raises `ModuleNotFoundError`. The docstring is misleading.  
**Fix:** Update the docstring to accurately reflect the dependency.

---

### #14 — LOW — analyze_candlestick_patterns.py:341 — dead `df.rename()` call
**Buggy:**
```python
df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close"})
```
**Problem:** Renames each column to itself — a no-op. MT5's `copy_rates_from_pos()` already produces lowercase column names.  
**Fix:** Remove the line.

---

## INFO (no fix required)

### #15 — INFO — fib_confluence.py — trendline confirmation edge case undocumented
When the recent window has fewer than 2 swing lows/highs, `_trendline_and_channel_value()` returns `(None, None)` silently. Handled correctly, but not documented in the function's docstring.

### #16 — INFO — xauusd_mt5_strategy.py — module-level mutable singletons (`_LEAGUE_STATE`, `_SHADOW_STATE`)
Not a current bug (single-threaded scan loop), but would be a thread-safety issue if the scanning loop ever moves to threads. Flag if `ThreadingHTTPServer` is extended.

### #17 — INFO — bot_monitor.py — `wmic` deprecated in newer Windows
`wmic` is deprecated in Windows 11 and removed from some Server builds. The fallback in `is_bot_running()` would return `False` on those platforms, triggering repeated restart alerts. Not triggered on Windows Server 2019.

### #18 — INFO — harmonic_patterns.py — Cypher `n_confirm` unconditional +1
Cypher always gets `n_confirm = 2 + 1 = 3` while classic patterns with 2/3 bands confirmed get `n_confirm = 2`. The +1 inflates Cypher scores relative to partially-matching classic patterns. Intentional or accidental — document if intentional.

### #19 — INFO — backtest_sim.py — `build_data()` also has NameError on `all_tf`
Even after fixing `LOOKBACK` (finding #6), `all_tf` is still only defined inside `__main__`. Acceptable since `build_data()` is only ever called inside `__main__`.

---

## Summary

| Severity | Count | Key files |
|---|---|---|
| HIGH | 2 | xauusd_mt5_strategy.py, macro_data.py |
| MEDIUM | 5 | harmonic_patterns.py, macro_data.py, backtest_sim.py |
| LOW | 7 | strategy_simulator.py, league.py, symbol_normalize.py, verify_data_sources.py, backtest_sim.py, telegram_alert.py, analyze_candlestick_patterns.py |
| INFO | 5 | (no action required) |

**Highest priority before next VPS sync:** #1 and #2 (WAL modes — most likely to cause real "database is locked" errors under production scan load). #4 (harmonic cd_ratio_implied — silently inflates scores of weak matches, makes the entry trigger fire on lower-quality patterns than intended). #5 (Myfxbook password in exception — security).
