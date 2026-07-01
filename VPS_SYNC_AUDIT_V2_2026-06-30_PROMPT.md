# Prompt for Claude Code (run ON THE VPS) — sync v2 audit fixes + resolve CRLF + push

Paste this into Claude Code **on the VPS**, in the live bot folder
(`C:\Users\Administrator\Desktop\RoBotTrading man 0 V10`).
Also copy `AUDIT_REPORT_2026-06-30_v2.md` into that folder alongside this prompt.

---

## Context

A second-pass audit (`AUDIT_REPORT_2026-06-30_v2.md`) was run after the
37-finding first-pass audit was committed and pushed. It found 19 new
findings across the same 22 Python files. All 37 first-pass findings had
already been fixed. Of the 19 v2 findings:

- 5 are INFO-only (no fix required — see report, findings #15–#19).
- 13 of the 14 fixable findings were fixed and committed locally in commit
  `4bbc19e` ("Fix 14 audit findings from v2 audit") — but that commit has
  **not yet been pushed to GitHub**. This prompt covers pushing it, applying
  the one remaining fix (#12), and resolving a CRLF line-ending drift that
  would otherwise create noisy diffs going forward.

**Before doing anything**: run `git log --oneline -10` and `git status`.
If your working tree shows the `4bbc19e` commit, 13 of 14 fixes are already
here — skip Step 1. If your HEAD is at `4070bc9` or earlier, follow Step 1.

---

## Step 0 — Safety preconditions

1. Check for open positions before touching anything. These are non-scoring,
   non-risk-parameter changes — but don't restart the bot mid-position.
2. Never print, commit, or show the contents of `strategy_config.json`.
3. Do not change any strategy weight, `MIN_STRATEGY_SCORE`, `ENTRY_MODE`,
   or risk/lot parameter.

---

## Step 1 — Pull the v2 fixes from GitHub (if not already at 4bbc19e)

```powershell
git pull origin main
```

This will pull `4bbc19e` and `54befda` (SYNC_RESULT doc), adding all 13 v2
fixes to this machine. After pulling, confirm with `git log --oneline -3`.

If `git pull` reports merge conflicts on any Python file, **stop and report
which files conflict** rather than auto-resolving — the VPS copy may have
independent uncommitted edits that need manual review.

---

## Step 2 — Apply the one remaining fix (#12)

**File:** `backtest_sim.py`

**Finding #12 (LOW):** The first `logging.getLogger("xauusd_ea").disabled = True`
(immediately after the `import` statements, before `ea.load_ui_config()`)
is dead code. `load_ui_config()` calls `setup_logging()`, which re-enables
the logger, so only the second disable (after `load_ui_config()`) has any
effect.

Find:
```python
    logging.getLogger("xauusd_ea").disabled = True
    ea.load_ui_config()
    logging.getLogger("xauusd_ea").disabled = True
```

Replace with:
```python
    ea.load_ui_config()
    logging.getLogger("xauusd_ea").disabled = True  # must be AFTER load_ui_config (setup_logging re-enables it)
```

Then verify: `python -m py_compile backtest_sim.py` → should print nothing (success).

---

## Step 3 — Fix the CRLF line-ending drift

**Problem:** The local working copy (macOS/Linux sandbox) converted all
Python files from Windows CRLF to Unix LF when they were read and re-saved
by the edit tools. `git diff --stat` shows ~25 files with thousands of
balanced `+N/-N` line counts — this is pure line-ending churn, not real
content changes. If committed as-is, it creates a massive noise commit and
leaves the VPS (Windows) with LF files that some Windows editors may not
handle well.

**Fix:** Add a `.gitattributes` file to lock line endings for this repo
going forward so every checkout normalises to the correct format:

Check if `.gitattributes` already exists:
```powershell
Get-Item .gitattributes
```

If it doesn't exist (or doesn't have a `* text=auto` or `*.py eol=crlf`
line), create/update it:

```powershell
@"
# Normalise line endings on commit; checkout converts to platform default
* text=auto

# Force Python files to CRLF on Windows checkout (VPS is Windows)
*.py text eol=crlf
*.bat text eol=crlf
*.ps1 text eol=crlf
*.md text eol=lf
*.json text eol=lf
"@ | Out-File -Encoding utf8 .gitattributes
```

Then renormalise the tracked files so git stops seeing them as modified:
```powershell
git add --renormalize .
git status --short
```

`git status` should now show only `.gitattributes` (new file) and
`backtest_sim.py` (your Step 2 fix) as staged — not 25 other files. If
it still shows Python files as modified after `--renormalize`, check
`git config core.autocrlf` — it should be `true` on Windows.

---

## Step 4 — Commit

```powershell
git add backtest_sim.py .gitattributes AUDIT_REPORT_2026-06-30_v2.md
git commit -m "Fix v2 audit finding #12 (duplicate logging.disabled); add .gitattributes for CRLF

backtest_sim.py: remove dead logging.getLogger('xauusd_ea').disabled before
load_ui_config() -- setup_logging() re-enables the logger so only the call
after load_ui_config() has effect. Finding #12 (LOW) from AUDIT_REPORT_2026-06-30_v2.md.

.gitattributes: normalise line endings (*.py CRLF on Windows) to prevent
CRLF/LF drift from creating phantom diffs across macOS/Linux editor sessions."
```

Run `git log -1 --format=%h` after the commit and note the real hash —
update `CHANGELOG.md`'s entry for this fix with that hash (not a copied hash
from elsewhere).

---

## Step 5 — Push to GitHub

```powershell
git push origin main
```

Confirm the push succeeded with `git log --oneline origin/main -5`.

---

## Verification checklist

- [ ] `git log --oneline -5` shows `4bbc19e` (or newer) and your new commit
- [ ] `python -m py_compile backtest_sim.py` passes
- [ ] `git diff --stat` shows no unexpected modified files (should be clean)
- [ ] `AUDIT_REPORT_2026-06-30_v2.md` is in the folder
- [ ] `strategy_config.json` was never shown, printed, or staged
- [ ] No strategy weights, risk params, or `ENTRY_MODE` changed
- [ ] Bot restart (if needed) only after confirming no open positions, via
      the normal UI sequence — not by running the strategy script directly

---

## Summary of v2 audit status (for the record)

| # | Severity | File | Finding | Status |
|---|---|---|---|---|
| 1 | HIGH | xauusd_mt5_strategy.py | `_debug_scores_db_connect` missing WAL | ✅ Fixed in 4bbc19e |
| 2 | HIGH | macro_data.py | `_db_connect` missing WAL | ✅ Fixed in 4bbc19e |
| 3 | MEDIUM | xauusd_mt5_strategy.py | `get_strategy_scores_history` conn leak | ✅ Fixed in 4bbc19e |
| 4 | MEDIUM | harmonic_patterns.py | `cd_ratio_implied` inflates n_confirm | ✅ Fixed in 4bbc19e |
| 5 | MEDIUM | macro_data.py | Myfxbook password in exception | ✅ Fixed in 4bbc19e |
| 6 | MEDIUM | backtest_sim.py | `LOOKBACK` inside `__main__` | ✅ Fixed in 4bbc19e |
| 7 | MEDIUM | backtest_sim.py | MT5/EA imported at module level | ✅ Fixed in 4bbc19e |
| 8 | LOW | strategy_simulator.py | `save_state` missing PermissionError retry | ✅ Fixed in 4bbc19e |
| 9 | LOW | league.py | `winrate` variable shadows function | ✅ Fixed in 4bbc19e |
| 10 | LOW | symbol_normalize.py | Dead entries in `_GOLD_ALIASES` | ✅ Fixed in 4bbc19e |
| 11 | LOW | verify_data_sources.py | Module-level `results` accumulates | ✅ Fixed in 4bbc19e |
| 12 | LOW | backtest_sim.py | Duplicate `logging.disabled = True` | ✅ Fixed in this prompt (Step 2) |
| 13 | LOW | telegram_alert.py | Docstring claims dependency-free | ✅ Fixed in 4bbc19e |
| 14 | LOW | analyze_candlestick_patterns.py | Dead `df.rename()` no-op | ✅ Fixed in 4bbc19e |
| 15–19 | INFO | various | No fix required | — |
