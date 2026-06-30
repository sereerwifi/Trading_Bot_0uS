# Prompt for Claude Code (run ON THE VPS) — sync the 2026-06-30 full audit (37 findings) + fix the changelog/commit problem

Paste this into Claude Code **on the VPS**, in the live bot folder. Bring
three files along with this prompt (copy them into the same folder first):
`AUDIT_REPORT_2026-06-30.md`, `CHANGELOG.md`, and this file. They are the
source of truth for *what* to fix — this prompt is the source of truth for
*how* to apply it safely and honestly.

## Why this prompt exists (read this before doing anything)

The local working copy (`RoBotTrading man 0 V10`) received a full 22-file
audit (37 findings — 5 critical, 5 high, 13 medium, 14 low/info) on
2026-06-30. All findings have been fixed and committed on the local copy
in two commits:

- `02ef72b` — Fix 3 post-mortem bugs from 2026-06-30 reversal analysis
- `53e2e40` — Fix all 37 audit findings across 14 files
- `aba792b` — Add CHANGELOG.md

These commit hashes are **real on the local copy** (`RoBotTrading man 0 V10`).
The `CHANGELOG.md` references them correctly for the local repo.

**The VPS has not received any of these changes.** The VPS still has its
own separate git history that diverged from the local copy — you cannot
`git pull` these commits across. Every fix must be applied by hand on the
VPS, then committed there. The VPS commits will get their own different
hashes. After committing on the VPS, **update `CHANGELOG.md` with the VPS's
actual hashes** (from `git log -1 --format=%h` after each commit) — do not
copy the local copy's hashes into the VPS changelog, since they won't exist
in the VPS's git log.

**Before touching anything**: read the VPS's own `CLAUDE.md`, run
`git log --oneline -20` and `git status`, and compare both against what's
described below and in `AUDIT_REPORT_2026-06-30.md`. The audit was run
against the local copy — if a finding's "buggy" snippet doesn't match what
you find here (line numbers will differ; some bugs may already be
independently fixed, or this copy may have diverged further),
**stop and report the discrepancy instead of guessing**. Don't apply a fix
to code that doesn't match its documented "before" state.

---

## Step 0 — Safety preconditions (do this first, every time)

1. Check whether the bot currently has open positions (MT5 terminal or
   `manual_trades.json` / dashboard). If it does, **do not restart the bot
   process** after applying fixes without the user's explicit go-ahead —
   these are bug fixes, not urgent enough to risk interrupting open risk.
   Applying the file edits and committing is fine; restarting is a separate,
   user-confirmed step.
2. Back up the current state before editing anything:
   `python backup_restore.py` if that's the existing mechanism, or at minimum
   a manual zip of every file you're about to touch.
3. Never print, log, paste, or commit the contents of `strategy_config.json`
   (Telegram bot token/chat ID, Myfxbook email/password) — this applies to
   every step below, including git commit messages and diffs you show the
   user. If a `git diff` output would include that file, exclude it.
4. Do not change `MIN_STRATEGY_SCORE`, any strategy weight, `ENTRY_MODE`,
   `LOGIC_GROUP_SELECTION`, or any risk/lot-sizing parameter as a side effect
   of these fixes. Finding #11 (`macro_bias` weight) is the one exception —
   it's explicitly a bug (wrong default vs. documented intent), not a judgment
   call, and is called out below.

---

## Step 1 — Apply and verify each finding

Work through `AUDIT_REPORT_2026-06-30.md` finding by finding (37 total,
grouped Critical → High → Medium → Low/Info). For each one:

1. `grep -n` for the buggy pattern quoted in the finding, in the file it
   names. Confirm it's actually present here before editing.
2. If present and matches: apply the fix exactly as described in the report.
3. If absent, or the surrounding code looks different from the report's
   description: skip it and note it in your final summary as "already fixed
   on VPS" or "diverged — needs manual review" rather than forcing a change.
4. After each file is fully edited, `python -m py_compile <file>` before
   moving to the next file.

The eight findings below are the ones already hand-verified against the
local copy's actual code (exact text, not just the report's paraphrase) —
use these verbatim. For the remaining 29, `AUDIT_REPORT_2026-06-30.md`'s
own finding text is precise enough (file, line, buggy snippet, fix) — apply
it the same way, with the same verify-first discipline.

### Finding #1 — harmonic_patterns.py: direction inverted

Find: `direction = "bullish" if x_type == "high" else "bearish"`
Replace: `direction = "bullish" if x_type == "low" else "bearish"`

### Finding #2 — harmonic_patterns.py: PRZ `d_from_cd` projects the wrong way

Find: `d_from_cd = C + cd_mid * (C - B)`
Replace: `d_from_cd = C + cd_mid * (B - C)`

### Finding #4 — xauusd_mt5_strategy.py: missing `timedelta` import

Find: `from datetime import datetime, time as dtime`
Replace: `from datetime import datetime, time as dtime, timedelta`

(If the VPS file already imports `timedelta` some other way, e.g. a separate
`import datetime` statement, skip this one — confirm with `grep -n
"timedelta" xauusd_mt5_strategy.py` first.)

### Finding #5 — xauusd_mt5_strategy.py: `account_info()` None guards (3 call sites)

At each of the three call sites in `run_confluence_scan()`,
`run_logic_groups_scan()`, and `run_once()`:

Find (pattern, repeated 3x with different surrounding function names):
```python
    info = mt5.account_info()
```
Replace each with:
```python
    info = mt5.account_info()
    if info is None:
        logger.warning("<function_name>: mt5.account_info() returned None — skipping scan (transient disconnect).")
        return
```
(Use the literal function name in place of `<function_name>` — e.g.
`run_confluence_scan`, `run_logic_groups_scan`, `run_once` — matching the
existing log-message style elsewhere in the file. `run_once()`'s correct
`return`/`continue` behavior depends on its loop structure — read the
surrounding 20 lines before editing so the guard doesn't change control
flow in an unintended way.)

### Finding #7 — xauusd_mt5_strategy.py: legacy short fib zone always False

Find:
```python
    in_fib_zone = fibs["50.0"] <= last["close"] <= fibs["61.8"] if leg_up \
```
Replace:
```python
    in_fib_zone = fibs["61.8"] <= last["close"] <= fibs["50.0"] if leg_up \
```
(Check the line beneath it too — the report's full finding covers both the
up-leg and down-leg branches; make sure the two conditions end up as
mirror images of each other, not both using the same ordering.)

### Finding #9 — bot_monitor.py: fallback process check detects itself

Find: `return "python.exe" in result.stdout` (in the `wmic` fallback path)
Replace: `return "xauusd_mt5_strategy" in result.stdout`

### Finding #11 — strategy_config_ui.py: `macro_bias` weight wrong in defaults

Find: `"macro_bias": {"enabled": True, "weight": 0.6},`
Replace: `"macro_bias": {"enabled": True, "weight": 1.2},`

### Finding #24 — fib_confluence.py: negative extension label formula

Find the `level_label()` branch handling negative ratios. Ensure it computes:
```python
mirrored = (abs(ratio) + 1.0) * 100
```
not `abs(ratio) * 100`. If the VPS copy already has this (it may, if
strategy #32's original sync prompt was applied correctly), skip.

For the remaining findings (#3, #6, #8, #10, #12–#23, #25–#37), follow the
same grep-verify-fix-compile loop using `AUDIT_REPORT_2026-06-30.md` as the
spec. A few worth flagging explicitly because they touch process/safety
rather than pure logic:

- **#8 (zip-slip in `backup_restore.py`)** and **#33/#34
  (`dashboard_server.py` timing-safe compare + 503 on missing file)** are
  security fixes — apply them even if nothing else looks urgent.
- **#30 (`bot_monitor.py restart_bot()` only restarts the EA, not the full
  stack)** depends on `launch_bot.py` existing on this VPS with an
  `--autostart` flag. If `launch_bot.py` isn't here yet, that's a
  prerequisite gap — report it rather than partially wiring the fix.
- **#20/#21 (`backtest_sim.py`)** only matter if backtesting is actually run
  on this VPS; low priority if it's a local-only dev tool here.

---

## Step 2 — Commit for real

Once a logical group of findings is applied and compiling cleanly, commit
it — don't squash all 37 into one opaque commit, and don't invent a message
that claims more than what's actually in the diff:

```
git add <files you actually changed for this group>
git commit -m "Fix N audit findings: <short description>

<one line per finding, referencing AUDIT_REPORT_2026-06-30.md's numbering>"
```

Reasonable grouping: (1) the 5 critical findings as one commit, (2) the 5
high findings as a second, (3) medium + low/info findings, grouped by file
or theme, as however many additional commits make sense. After each commit,
run `git log -1 --format=%h` and use *that* hash — not a hash from
`CHANGELOG.md` — when you update the changelog in step 3.

---

## Step 3 — Update `CHANGELOG.md` with the VPS's real hashes

The `CHANGELOG.md` you copied from the local machine uses local commit
hashes (`53e2e40`, `02ef72b`, etc.) that do not exist in the VPS's git
log. Once you've committed the fixes in Step 2, replace every `[xxxxxxx]`
hash in `CHANGELOG.md` that refers to the audit or post-mortem commits with
the real hashes from `git log -1 --format=%h` on the VPS. If you committed
as multiple commits, list them all or adjust the section headers accordingly.
The local hashes are correct for the local repo — do not carry them over
verbatim.

---

## Verification checklist (don't skip)

- [ ] Every edited file passes `python -m py_compile <file>`.
- [ ] `strategy_config.json` was never printed, logged, or included in any
      commit/diff shown to the user.
- [ ] No strategy weight, `MIN_STRATEGY_SCORE`, `ENTRY_MODE`, or risk
      parameter changed except finding #11's `macro_bias` weight.
- [ ] Bot was **not** restarted without the user's explicit go-ahead,
      especially if positions were open at Step 0.
- [ ] Every commit hash in `CHANGELOG.md` for the audit/post-mortem entries
      is a real hash from this VPS's own `git log`, generated after the
      corresponding commit, not copied from the local copy's changelog
      (local hashes `53e2e40`, `02ef72b`, `aba792b` will not exist here).
- [ ] Findings that didn't match the VPS's actual code were reported as
      "already fixed" or "diverged," not silently skipped or forced through.
