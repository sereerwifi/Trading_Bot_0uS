# Prompt for Claude Code — full codebase + log audit

Paste this into Claude Code, running inside this folder. Read `CLAUDE.md`
first if you haven't already — it explains the bot's architecture, the
entry modes, and which copy (`RoBotTrading man 0 US` vs
`RoBotTrading man 0 USV9`) is primary. This is a read-only audit: find and
report problems, don't fix anything yet unless explicitly told to.

## Goal

Audit every Python file in this folder plus the running log, and verify
that every strategy/scoring function is wired correctly end-to-end and is
internally correct. Produce a written report, not silent fixes.

## Step 1 — Inventory

1. List every `.py` file in this folder (`ls *.py`).
2. In `strategies.py`, list every function matching `def score_*(` and
   compare that list against the keys in `STRATEGY_REGISTRY` at the bottom
   of the file. Flag:
   - Any `score_*` function that exists but has **no** registry entry
     (dead code, or a wiring step that was missed).
   - Any registry entry whose function reference doesn't resolve (typo,
     function renamed/removed).
   - Any registry key that doesn't appear in `xauusd_mt5_strategy.py`'s
     `_RECOMMENDED_STRATEGY_WEIGHTS` dict (missing weight — would silently
     fall back to the default weight of `1.0`, which may not be intended).
   - Any registry key that doesn't appear in `strategy_config_ui.py`'s
     `DEFAULT_CONFIG["confluence"]["strategies"]` and `STRATEGY13_LABELS`
     (UI won't show it / it won't be individually toggleable).
   - Whether the strategy count stated in `CLAUDE.md`'s
     `## Strategies (N total)` heading matches `len(STRATEGY_REGISTRY)`.

2. Confirm `score_all()` / `run_confluence_scan()` / `run_logic_groups_scan()`
   in their respective files actually call every registered strategy with
   the right argument shape — most take `(data)`, but check the registry for
   any `lambda data: score_fn(data, extra_kw=...)` wrappers (used for
   strategies registered more than once with different parameters, e.g. the
   smart-money-sweep morning/night pair) and confirm those wrapped calls are
   passing sane, distinct arguments rather than accidentally identical ones.

## Step 2 — Per-function correctness check

For every `score_*` function in `strategies.py`, check:

- **Return shape**: always returns a dict with exactly `"long"`, `"short"`,
  `"note"` keys, and `"long"`/`"short"` are always floats (not `None`,
  not numpy types that might not JSON-serialize cleanly into
  `strategy_scores.json`).
- **Guard clauses**: every function that reads `data["m1"]`, `data["m15"]`,
  `data["h1"]`, `data["h4"]`, `data["dom"]`, or `data["macro"][...]` has an
  early-return guard for that key being `None`/missing/too-short, rather
  than risking a `KeyError`/`AttributeError`/`IndexError` if that data
  source is ever briefly unavailable mid-scan.
- **ATR safety**: any division by an ATR value guards against zero/NaN
  (look for the `max(atr_now, 1e-6)` pattern used elsewhere — flag any
  function dividing by ATR without an equivalent floor).
- **Score bounds**: long/short scores are clipped into `[0, 100]` (via
  `_clip()` or equivalent) before being returned — flag anything that could
  produce a negative score or a score above 100.
- **Internal consistency**: re-derive the function's own documented logic
  from its docstring and confirm the code actually implements what the
  docstring claims (this caught the `LOGIC_GROUPS_APPLY_DAILY_FILTER`
  double-gating bug previously — re-check that fix is still intact while
  you're in there).
- **Session/time-window strategies specifically** (`scalp_london_sweep`,
  `scalp_ny_orb`, `smart_money_sweep_morning`, `smart_money_sweep_night`,
  and any others gated on `data["now"]`): confirm what clock `data["now"]`
  actually is (`build_market_data()`'s `datetime.now()` call, and whatever
  this machine's OS timezone actually is right now — check with `date` in
  the shell, don't just trust a docstring) versus what each function's
  docstring *claims* `session_start`/`session_end` are denominated in
  (broker time vs Bangkok time). Report any mismatch explicitly — there is
  already one known, intentionally-unfixed mismatch documented in
  `CLAUDE.md` (the `scalp_*` strategies' "broker UTC+3" docstrings vs the
  actual Thai-time clock) — confirm whether that's still the only one, or
  whether the audit finds others.

## Step 3 — Cross-file wiring consistency

- `strategy_config.json` (read structure only — **do not print Telegram
  `bot_token`/`chat_id` or Myfxbook `email`/`password` values**, just
  confirm which top-level keys exist): does every key in
  `STRATEGY_REGISTRY` have a corresponding entry under
  `confluence.strategies`? Anything present in code but missing from this
  live config file will get backfilled by `_deep_merge` on next UI
  save/load — confirm that backfill path still exists and works, rather
  than assuming it from memory.
- `generate_dashboard.py`: confirm the hardcoded strategy-count text
  (search for "Multi-Strategy Confluence" and similar) matches the actual
  current count, and that any per-strategy display logic (icons, labels)
  doesn't silently skip newly added strategies.
- `ENTRY_MODE` paths: confirm `confluence13` and `logic_groups` modes both
  still function against the *current* `STRATEGY_REGISTRY` — i.e.
  `logic_groups`' two pools (Day Trade / Scalping Trade) include every
  strategy intended for them, and nothing newly added was silently left out
  of both pools (which would mean it's registered and weighted but never
  actually eligible to be picked as a candidate in `logic_groups` mode).

## Step 4 — Log audit

1. Read the tail of `xauusd_mt5_strategy.log` (last ~500-1000 lines, or
   further back if the file is small) and summarize: any Python
   tracebacks/exceptions, any repeated "veto"/"neutral"/"Daily filter"
   messages that look like a stuck pattern, any MT5 connection-loss/retry
   sequences, and the timestamp of the most recent line (compare against
   `bot_state.json`'s `started_at` and the current time — large gaps mean
   the process likely died silently).
2. If `bot_monitor.py` exists in this folder, check whether it's
   relevant to corroborating "is the bot actually alive" independent of the
   log file.
3. Read `strategy_scores.json` and sanity-check its shape against what
   `generate_dashboard.py` expects to read (key names, nested structure for
   `logic_groups` if present) — a silent schema drift here would make the
   dashboard show stale/wrong data without erroring.

## Step 5 — Report

Write findings as a plain list grouped by severity, not prose paragraphs:

- **Broken** (would cause a crash, a strategy silently never firing despite
  being enabled, or visibly wrong scores) — needs a fix.
- **Inconsistent** (works but contradicts its own docstring, or one file's
  wiring disagrees with another's, e.g. a weight present in one file but
  missing in another) — needs a decision from the user on intended
  behavior before fixing.
- **Cosmetic/doc-only** (CLAUDE.md count out of date, a stale comment) —
  low priority, fine to batch-fix.

For each finding: file + line reference, what's wrong, and what the fix
would look like — but **do not apply any fix without the user's
go-ahead**, per this project's hard rule against changing behavior
(including subtle scoring/weight changes) without explicit confirmation.
Never print secrets (Telegram token/chat ID, Myfxbook email/password) in
the report even if you have to read `strategy_config.json` to check key
presence.
