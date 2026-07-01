# Prompt for Claude Code (run ON THE VPS) — sync BOM config-reload fix + minimized console windows

Paste this into Claude Code **on the VPS**, in the live bot folder
(`C:\Users\Administrator\Desktop\RoBotTrading man 0 V10`).

---

## Context

While debugging a "why didn't the bot pick up my config change" report locally, the
log showed a live config-reload crash:

```
2026-07-01 18:41:41 | INFO  | strategy_config.json changed on disk — reloading settings live (no restart needed).
2026-07-01 18:41:41 | ERROR | Live config reload failed — keeping previous settings active.
...
json.decoder.JSONDecodeError: Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)
```

`strategy_config.json` on disk had picked up a UTF-8 byte-order-mark (BOM) —
likely from an external editor or a PowerShell `-Encoding utf8` save (PowerShell
5.1's `utf8` encoding always adds a BOM) — and both places that read the file
were opening it with plain `"utf-8"`, which chokes on a leading BOM. The bot
correctly fell back to keeping its previous in-memory settings rather than
crashing outright, but any edit made right before that timestamp silently
failed to apply until the next successful reload.

Fixed locally in commit `ca7a768` ("Fix: tolerate UTF-8 BOM when loading
strategy_config.json; start bot/dashboard consoles minimized"), pushed to
`origin/main`. This prompt syncs that commit to the VPS.

The same commit also bundles an unrelated, already-tested UI change: the
bot/dashboard/tunnel console windows launched by `strategy_config_ui.py`'s
"Start Bot" button now open minimized and without stealing focus from the
config UI (`SW_SHOWMINNOACTIVE`), instead of popping a full console window
in front of it.

**Before doing anything**: run `git log --oneline -5` and `git status`.
If `ca7a768` (or newer) is already at HEAD, this fix is already live — stop here.

---

## Step 0 — Safety preconditions

1. Check for open positions before restarting anything. This is a reload/UI
   fix, not a risk-parameter change — but don't restart the bot mid-position.
2. Never print, commit, or show the contents of `strategy_config.json`.
3. Do not change `ENTRY_MODE`, any strategy weight, or any risk/lot parameter
   while applying this.

---

## Step 1 — Pull the fix from GitHub

```powershell
git pull origin main
```

This should bring in commit `ca7a768`, touching only:
- `xauusd_mt5_strategy.py` (1 line: `load_ui_config()`'s `open(path, "r", encoding="utf-8")` → `"utf-8-sig"`)
- `strategy_config_ui.py` (`load()`'s reader gets the same `utf-8-sig` fix, plus the new `_win_minimized_startupinfo()` helper wired into `_popen_script()` / `_popen_tunnel()`)

If `git pull` reports a conflict on either file, **stop and report which file
conflicts** rather than auto-resolving — the VPS copy may have independent
uncommitted edits that need manual review.

---

## Step 2 — Verify

```powershell
python -m py_compile xauusd_mt5_strategy.py strategy_config_ui.py
git log --oneline -3
```

Both should succeed silently; `git log` should show `ca7a768` (or newer) at
or near HEAD.

Optional: confirm `strategy_config.json` currently has a BOM and that it no
longer breaks a reload —

```powershell
Get-Content -Encoding Byte -TotalCount 3 strategy_config.json
```
(`239 187 191` = `EF BB BF` = BOM present is fine either way now; the point is
the loader no longer cares.)

---

## Step 3 — Restart to pick up the fix (if the bot is running)

The fix only takes effect on the *next* process start or config-load call —
a currently-running bot process already has the old `load_ui_config()` in
memory. After confirming no open positions:

1. Use the normal UI "Stop Bot" / "Start Bot" sequence in `strategy_config_ui.py`
   — do not run `xauusd_mt5_strategy.py` directly.
2. Watch the new console window: it should now open **minimized** instead of
   stealing focus from the config UI.
3. Tail the log for the next config-reload cycle (or touch
   `strategy_config.json`'s mtime, e.g. re-save from the UI) and confirm no
   `JSONDecodeError` / "Live config reload failed" appears.

---

## Verification checklist

- [ ] `git log --oneline -3` shows `ca7a768` (or newer)
- [ ] `python -m py_compile xauusd_mt5_strategy.py strategy_config_ui.py` passes
- [ ] `strategy_config.json` was never shown, printed, or staged
- [ ] No `ENTRY_MODE`, strategy weight, or risk/lot parameter changed
- [ ] Bot restarted only after confirming no open positions, via the normal
      UI sequence
- [ ] Next config reload (if triggered) shows no `JSONDecodeError` in the log
- [ ] "Start Bot" launches its console window minimized, without stealing
      focus from the config UI

---

## Related, but NOT part of this sync (flag only)

While investigating the above, the local session also found MT5's terminal-wide
**AutoTrading toggle was OFF** (`mt5.terminal_info().trade_allowed == False`),
which separately caused `order_send FAILED (retcode=10027) "AutoTrading disabled
by client"` around the same incident window. That's a live MT5 terminal setting,
not a code change — it isn't fixed by this commit and isn't something `git pull`
can touch. Worth checking the AutoTrading button state on the VPS's MT5 terminal
directly while you're in there, but treat it as a separate, manual check.
