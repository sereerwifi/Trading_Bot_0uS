# Prompt for Claude Code (run ON THE VPS) — sync backtest_sim.py dead-code fix

Paste this into Claude Code **on the VPS**, in the live bot folder
(`C:\Users\Administrator\Desktop\RoBotTrading man 0 V10`).

---

## Context

`AUDIT_REPORT_2026-06-30_v2.md` (finding #12, LOW severity) flagged that
`backtest_sim.py`'s `__main__` block called
`logging.getLogger("xauusd_ea").disabled = True` **twice** — once before
`ea.load_ui_config()` and once after:

```python
    logging.getLogger("xauusd_ea").disabled = True
    ea.load_ui_config()
    logging.getLogger("xauusd_ea").disabled = True
```

The first call is dead code: `load_ui_config()` calls `setup_logging()`
internally, which re-enables the logger, so only the second call (after
`load_ui_config()`) has any effect. 13 of the 14 fixable v2-audit findings
were already synced in commit `4bbc19e`; this was the one remaining item,
now fixed and pushed in commit `f484bef` ("Fix v2 audit finding #12 (dead
duplicate logging.disabled in backtest_sim.py)").

This is a no-op cleanup — it doesn't change what `backtest_sim.py` actually
does at runtime (the effective disable-logging behavior is identical before
and after), just removes a redundant line. `backtest_sim.py` is a standalone
script (run manually for backtesting), not part of the live bot's scan loop,
so **no bot restart is required** for this fix to take effect.

**Before doing anything**: run `git log --oneline -5` and `git status`.
If `f484bef` (or newer) is already at HEAD, this fix is already live — stop here.

---

## Step 0 — Safety preconditions

1. Never print, commit, or show the contents of `strategy_config.json`.
2. Do not change `ENTRY_MODE`, any strategy weight, or any risk/lot parameter
   while applying this.
3. If `backtest_sim.py` happens to be mid-run on the VPS, let it finish
   before pulling — the pull itself is safe either way (it's a script file,
   not something imported by the running live bot process), but there's no
   reason to disturb an in-progress backtest.

---

## Step 1 — Pull the fix from GitHub

```powershell
git pull origin main
```

This should bring in commit `f484bef`, touching only `backtest_sim.py`
(removes the dead pre-`load_ui_config()` disable call and adds a one-line
comment explaining why the remaining call must stay after `load_ui_config()`).
It also adds `VPS_SYNC_AUDIT_V2_2026-06-30_PROMPT.md` as a historical record
(no code in it — just documents the now-closed v2 audit).

If `git pull` reports a conflict on `backtest_sim.py`, **stop and report the
conflict** rather than auto-resolving — the VPS copy may have independent
uncommitted edits that need manual review.

---

## Step 2 — Verify

```powershell
python -m py_compile backtest_sim.py
git log --oneline -3
```

`py_compile` should succeed silently; `git log` should show `f484bef` (or
newer) at or near HEAD.

Optional sanity check — confirm only one `disabled = True` call remains and
it's after `load_ui_config()`:

```powershell
Select-String -Path backtest_sim.py -Pattern "disabled = True"
```
Should show exactly one match, on the line immediately after the
`ea.load_ui_config()` line.

---

## Verification checklist

- [ ] `git log --oneline -3` shows `f484bef` (or newer)
- [ ] `python -m py_compile backtest_sim.py` passes
- [ ] Only one `logging.getLogger("xauusd_ea").disabled = True` remains in
      `backtest_sim.py`, positioned after `ea.load_ui_config()`
- [ ] `strategy_config.json` was never shown, printed, or staged
- [ ] No `ENTRY_MODE`, strategy weight, or risk/lot parameter changed
- [ ] No bot restart needed/performed for this fix (standalone script, not
      part of the live scan loop)
