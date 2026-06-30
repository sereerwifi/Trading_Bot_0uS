# Prompt for Claude Code (run ON THE VPS) — sync 3 fixes from the 2026-06-30 reversal post-mortem

Paste this into Claude Code **on the VPS**, in the live bot folder. These are
three small, additive fixes that came out of analyzing a real reversal
(`ANALYSIS_REVERSAL_2026-06-30_0905-1030.md`) on this exact bot. None of them
change scoring weights, entry thresholds, or risk parameters — two are bug
fixes (a dead code path, a logging gap) and one is a single tolerance
constant. All were built and verified on the local working copy
(`RoBotTrading man 0 V10`) first.

**Before touching anything: read the VPS's own `CLAUDE.md` and compare it
against what's described below.** If `xauusd_mt5_strategy.py`'s
`get_fib_confluence_safe()`, `save_scores_snapshot()`/`run_logic_groups_scan()`/
`run_confluence_scan()`, or `harmonic_patterns.py`'s `_RATIO_TOL` don't match
the "before" snippets shown here — because something else was changed on the
VPS independently and not yet pulled back down — **stop and report the
discrepancy instead of guessing**; adapt the anchors to match what you
actually find.

---

## Fix 1 — `price_bars` table has been empty this whole time

**Bug**: `fib_confluence.save_price_bars()` exists and is correctly written,
but nothing ever calls it. `get_fib_confluence_safe()` only calls
`fib_confluence.compute_confluence()`. This was supposed to be wired in when
strategy #32 was synced (see this same prompt pattern in
`VPS_SYNC_FIB_CONFLUENCE_SR_PROMPT.md`, which documents the intent) but the
actual call never made it into the function body — confirmed missing on
both the local copy and (per the `price_bars: 0 rows` finding in the
analysis) the VPS.

**File**: `xauusd_mt5_strategy.py`

Find:
```python
def get_fib_confluence_safe(data):
    """Wraps fib_confluence.compute_confluence() so a Fibonacci computation
    error can never break a scan. Returns None on any unexpected error and
    score_fib_confluence_sr() treats that as a graceful 0/0, exactly like
    score_macro_bias() does when data["macro"] is None."""
    try:
        return fib_confluence.compute_confluence(data)
    except Exception:
        logger.exception("fib_confluence.compute_confluence() failed — fib_confluence_sr will score 0/0 this scan.")
        return None
```

Replace with:
```python
def get_fib_confluence_safe(data):
    """Wraps fib_confluence.compute_confluence() so a Fibonacci computation
    error can never break a scan. Returns None on any unexpected error and
    score_fib_confluence_sr() treats that as a graceful 0/0, exactly like
    score_macro_bias() does when data["macro"] is None.

    Also persists the latest H4/H1 OHLC bars to price_bars via
    fib_confluence.save_price_bars() every scan, independent of whether the
    confluence calc itself succeeds — this was supposed to happen already
    (see fib_confluence.py's module docstring + VPS_SYNC_FIB_CONFLUENCE_SR_
    PROMPT.md) but the call was never actually wired in here, which is why
    price_bars stayed empty in production. save_price_bars() is itself
    best-effort/never-raises, so this can't introduce a new failure mode."""
    try:
        fib_confluence.save_price_bars(SYMBOL, "h4", data.get("h4"))
        fib_confluence.save_price_bars(SYMBOL, "h1", data.get("h1"))
    except Exception:
        logger.exception("fib_confluence.save_price_bars() failed — price_bars history will be missing this scan.")
    try:
        return fib_confluence.compute_confluence(data)
    except Exception:
        logger.exception("fib_confluence.compute_confluence() failed — fib_confluence_sr will score 0/0 this scan.")
        return None
```

(`SYMBOL` is the existing module-level broker symbol constant, e.g.
`SYMBOL = "GOLD"` — already defined near the top of the file; no new import
needed beyond what's already there.)

After this fix, `fib_confluence_history.db`'s `price_bars` table should
start accumulating rows on the very next scan. Verify with:
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('fib_confluence_history.db')
print(conn.execute('SELECT COUNT(*) FROM price_bars').fetchone())
"
```
(Run it once right after restart, then again a minute later — the count
should increase.)

---

## Fix 2 — `logic_groups` mode only logs each group's WINNING strategy

**Bug**: every scan, `strategies.score_all()` computes a score for **all 33**
strategies, but only the strategy that wins each group's priority cascade
ever gets persisted anywhere durable. `strategy_scores.json` does contain the
full `scores` dict, but it's overwritten every scan — there is no history.
This blocked confirming whether `smart_money_sweep_morning` (#30) or
`climax_reversal_sr` (#26) actually fired during the analyzed bounce.

**Fix**: a new SQLite history table, `strategy_scores_history.db`, populated
every scan (or every Nth scan) with the full per-strategy scores dict —
purely additive logging, no behavior change.

### Step 2a — add the `sqlite3` import

**File**: `xauusd_mt5_strategy.py`

Find (near the top, with the other stdlib imports):
```python
import time
import json
import os
import logging
import logging.handlers
```

Replace with:
```python
import time
import json
import os
import sqlite3
import logging
import logging.handlers
```

### Step 2b — add the config flags

Find (the `MIN_STRATEGY_SCORE` block — adjust if the VPS's surrounding
comments differ slightly):
```python
MIN_STRATEGY_SCORE = 70.0
```

Replace with:
```python
MIN_STRATEGY_SCORE = 70.0

# Debug history logging: in ENTRY_MODE == "logic_groups", only the strategy
# that WINS each group's priority cascade gets logged per scan — the other
# ~31 strategies' scores exist in memory (strategies.score_all()'s full
# `scores` dict) and are dumped into strategy_scores.json, but that file is
# OVERWRITTEN every scan, so there is no historical record. A live-VPS
# post-mortem of the 2026-06-30 09:05-10:30 reversal hit this wall directly
# -- reversal-specific strategies (smart_money_sweep_morning, climax_
# reversal_sr) couldn't be confirmed as having fired during the bounce
# because nothing preserved their per-scan scores. This flag, when True,
# appends every strategy's long/short score + note to a local SQLite
# history table every DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N scans, so future
# "did strategy X fire at time Y" questions can be answered from data
# instead of "unknown — log doesn't show it". Purely additive/read-only
# logging — does not change scoring, weights, or entry decisions in any way.
# See log_all_strategy_scores_debug() / STRATEGY_SCORES_HISTORY_DB_PATH.
DEBUG_LOG_ALL_STRATEGY_SCORES = True
DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N = 1
```

### Step 2c — add the DB path constant

Find:
```python
SCORES_SNAPSHOT_PATH = os.path.join(_THIS_DIR, "strategy_scores.json")     # latest scan, for the dashboard
```

Replace with:
```python
SCORES_SNAPSHOT_PATH = os.path.join(_THIS_DIR, "strategy_scores.json")     # latest scan, for the dashboard
STRATEGY_SCORES_HISTORY_DB_PATH = os.path.join(_THIS_DIR, "strategy_scores_history.db")  # per-scan history of ALL strategy scores (see DEBUG_LOG_ALL_STRATEGY_SCORES)
```

### Step 2d — load the flags from config (in `load_ui_config()`)

Find the `global` declaration line:
```python
    global ENTRY_MODE, SCAN_INTERVAL_SECONDS, MIN_STRATEGY_SCORE, MIN_AGREEING_STRATEGIES
```

Replace with:
```python
    global ENTRY_MODE, SCAN_INTERVAL_SECONDS, MIN_STRATEGY_SCORE, MIN_AGREEING_STRATEGIES
    global DEBUG_LOG_ALL_STRATEGY_SCORES, DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N
```

Then find where the logging section of config is parsed:
```python
    LOG_BACKUP_COUNT = int(lg.get("backup_count", LOG_BACKUP_COUNT))
    setup_logging(LOG_DIR, LOG_LEVEL, LOG_TO_CONSOLE, LOG_FILE_MAX_BYTES, LOG_BACKUP_COUNT)

    d = cfg.get("daily_filter", {})
```

Replace with:
```python
    LOG_BACKUP_COUNT = int(lg.get("backup_count", LOG_BACKUP_COUNT))
    setup_logging(LOG_DIR, LOG_LEVEL, LOG_TO_CONSOLE, LOG_FILE_MAX_BYTES, LOG_BACKUP_COUNT)
    DEBUG_LOG_ALL_STRATEGY_SCORES = bool(lg.get("debug_log_all_strategy_scores", DEBUG_LOG_ALL_STRATEGY_SCORES))
    DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N = int(lg.get("debug_log_all_strategy_scores_every_n", DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N))

    d = cfg.get("daily_filter", {})
```

(This makes both flags overridable from `strategy_config.json`'s
`"logging"` section without a code change, matching the existing
config-flag pattern. No UI checkbox is required for this to work — it's
safe with just the code default — but a UI toggle can be added later if
wanted.)

### Step 2e — add the persistence + read-back functions

Find:
```python
    try:
        _save_json(SCORES_SNAPSHOT_PATH, snapshot)
    except OSError:
        logger.exception("Failed to write strategy_scores.json snapshot.")


def run_confluence_scan():
```

Replace with:
```python
    try:
        _save_json(SCORES_SNAPSHOT_PATH, snapshot)
    except OSError:
        logger.exception("Failed to write strategy_scores.json snapshot.")


_DEBUG_SCORES_SCAN_COUNT = 0  # in-process counter, used to honor DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N


def _debug_scores_db_connect():
    conn = sqlite3.connect(STRATEGY_SCORES_HISTORY_DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_scores_history (
        scanned_at REAL,
        scanned_at_iso TEXT,
        entry_mode TEXT,
        direction_taken TEXT,
        logic_groups_json TEXT,
        scores_json TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_scores_history_time "
                 "ON strategy_scores_history(scanned_at)")
    return conn


def log_all_strategy_scores_debug(scan_result, direction_taken=None, logic_groups=None):
    """Appends EVERY strategy's score/note for this scan to a local SQLite
    history table (strategy_scores_history.db), independent of which
    strategy (if any) actually wins a group's priority cascade or fires a
    trade. Fixes the gap found during the 2026-06-30 09:05-10:30 reversal
    post-mortem, where `logic_groups` mode's normal logging only showed the
    winning strategy per group, so reversal-specific strategies
    (smart_money_sweep_morning, climax_reversal_sr) couldn't be confirmed
    as having fired during the bounce even though strategies.score_all()
    computes a score for all 33 of them every single scan.

    Gated by DEBUG_LOG_ALL_STRATEGY_SCORES (default True) and throttled by
    DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N (default 1 = every scan). Purely
    additive/read-only — never raises, never affects scoring or entries,
    matches the best-effort pattern used by fib_confluence.py /
    harmonic_patterns.py's own history tables."""
    global _DEBUG_SCORES_SCAN_COUNT
    if not DEBUG_LOG_ALL_STRATEGY_SCORES:
        return
    _DEBUG_SCORES_SCAN_COUNT += 1
    every_n = max(1, DEBUG_LOG_ALL_STRATEGY_SCORES_EVERY_N)
    if (_DEBUG_SCORES_SCAN_COUNT % every_n) != 0:
        return
    try:
        now = datetime.now()
        conn = _debug_scores_db_connect()
        with conn:
            conn.execute(
                "INSERT INTO strategy_scores_history "
                "(scanned_at, scanned_at_iso, entry_mode, direction_taken, logic_groups_json, scores_json) "
                "VALUES (?,?,?,?,?,?)",
                (now.timestamp(), now.isoformat(), ENTRY_MODE, direction_taken,
                 json.dumps(logic_groups, default=str, ensure_ascii=False),
                 json.dumps(scan_result.get("scores"), default=str, ensure_ascii=False)))
        conn.close()
    except Exception:
        logger.exception("log_all_strategy_scores_debug() failed — strategy_scores_history.db not updated this scan (non-fatal).")


def get_strategy_scores_history(limit=500, since_ts=None):
    """Reads back strategy_scores_history rows, oldest -> newest. Never
    raises. `since_ts` (unix timestamp) optionally filters to rows at or
    after that time, for pulling a specific event window."""
    try:
        conn = _debug_scores_db_connect()
        if since_ts is not None:
            cur = conn.execute(
                "SELECT scanned_at_iso, entry_mode, direction_taken, logic_groups_json, scores_json "
                "FROM strategy_scores_history WHERE scanned_at >= ? ORDER BY scanned_at ASC LIMIT ?",
                (since_ts, limit))
        else:
            cur = conn.execute(
                "SELECT scanned_at_iso, entry_mode, direction_taken, logic_groups_json, scores_json "
                "FROM strategy_scores_history ORDER BY scanned_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        out = []
        for scanned_at_iso, entry_mode, direction_taken, logic_groups_json, scores_json in rows:
            out.append({
                "scanned_at_iso": scanned_at_iso,
                "entry_mode": entry_mode,
                "direction_taken": direction_taken,
                "logic_groups": json.loads(logic_groups_json) if logic_groups_json else None,
                "scores": json.loads(scores_json) if scores_json else None,
            })
        if since_ts is None:
            out.reverse()
        return out
    except Exception:
        logger.exception("get_strategy_scores_history() failed.")
        return []


def run_confluence_scan():
```

### Step 2f — call it from `run_confluence_scan()`

Find:
```python
    save_scores_snapshot(result, direction_taken=direction, macro=data.get("macro"))

    if direction is None:
```

Replace with:
```python
    save_scores_snapshot(result, direction_taken=direction, macro=data.get("macro"))
    log_all_strategy_scores_debug(result, direction_taken=direction)

    if direction is None:
```

### Step 2g — call it from `run_logic_groups_scan()`

Find:
```python
    save_scores_snapshot(
        result,
        direction_taken=(signals[0]["direction"] if signals else None),
        macro=data.get("macro"),
        logic_groups=groups_status,
    )
```

Replace with:
```python
    save_scores_snapshot(
        result,
        direction_taken=(signals[0]["direction"] if signals else None),
        macro=data.get("macro"),
        logic_groups=groups_status,
    )
    log_all_strategy_scores_debug(
        result,
        direction_taken=(signals[0]["direction"] if signals else None),
        logic_groups=groups_status,
    )
```

Verify after restart with:
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('strategy_scores_history.db')
print(conn.execute('SELECT COUNT(*) FROM strategy_scores_history').fetchone())
"
```

---

## Fix 3 — `harmonic_patterns` tolerance widened (0.07 → 0.09)

**File**: `harmonic_patterns.py`

Find:
```python
_RATIO_TOL = 0.07          # tolerance band added around each textbook ratio
```

Replace with:
```python
_RATIO_TOL = 0.09          # tolerance band added around each textbook ratio
# Widened from 0.07 -> 0.09 on 2026-06-30 after a live-VPS analysis of the
# 09:05-10:30 reversal found ZERO XABCD matches across 135 consecutive scans
# in that window (see ANALYSIS_REVERSAL_2026-06-30_0905-1030.md) -- 0.07 was
# evidently too tight for XAUUSD's real intraday swing noise. If this still
# produces too few matches, the next step up the report suggested was 0.10;
# if it now produces too MANY low-quality matches, drop back toward 0.07 --
# PRZ convergence (_XD_CONVERGENCE_ATR) and the fib_confluence cross-check
# bonus are the secondary filters that should keep low-quality matches from
# actually voting once entry-trigger (rejection candle at PRZ) is required.
```

---

## Verification checklist

1. Syntax-check all touched files:
   ```bash
   python3 -m py_compile xauusd_mt5_strategy.py fib_confluence.py harmonic_patterns.py
   ```
2. Confirm `SYMBOL` is defined before `get_fib_confluence_safe()` in the file
   (it already is — just don't introduce a NameError by misplacing the new
   calls above where `SYMBOL` is assigned).
3. After restarting the bot, give it a few scan cycles, then check both new
   data sources are actually growing (commands above) — not just that the
   code runs without error.
4. Confirm `strategy_config.json` doesn't need any manual edit — both new
   flags have safe in-code defaults (`True` / `1`); only add a
   `"logging": {"debug_log_all_strategy_scores": ..., "debug_log_all_strategy_scores_every_n": ...}`
   override if you actually want non-default behavior.
5. **Do NOT** change `MIN_STRATEGY_SCORE`, any strategy weight, `ENTRY_MODE`,
   or any risk/lot-sizing parameter as part of this sync — none of that is
   in scope here.
6. **Do NOT restart the live bot without the user's go-ahead** if it's
   currently in a live trading session with open positions — apply the code
   changes, verify they compile, and confirm with the user before bouncing
   the process.
