# Claude Code on the Trading VPS — Setup Checklist

Goal: get Claude Code running directly on the Windows VPS, in the bot's live
folder, so you can ask it to read logs, check the dashboard data, and debug
the EA without copy-pasting screenshots back here.

VPS folder (per your existing logs):
`C:\Users\Administrator\Desktop\RoBotTrading man 0 USV9\`

---

## 1. Connect to the VPS

RDP / remote desktop into the VPS as you normally do. Everything below runs
**on the VPS**, not on your local machine.

## 2. Install Node.js (if not already installed)

Claude Code needs Node.js 18+.

1. On the VPS, open a browser and go to https://nodejs.org/
2. Download the **LTS** Windows installer and run it (default options are fine, no special config needed).
3. Close and reopen PowerShell after install.

Check it worked:
```powershell
node --version
```
Should print `v18.x.x` or higher.

## 3. Run the install script

Copy `install_claude_code.ps1` (in this same folder) onto the VPS — e.g. via
RDP shared clipboard/drive, or download it from wherever you sync this
project. Then in PowerShell:

```powershell
cd "C:\Users\Administrator\Desktop\RoBotTrading man 0 USV9"
powershell -ExecutionPolicy Bypass -File install_claude_code.ps1
```

This installs Claude Code globally via npm and verifies the `claude` command
works. No Administrator rights needed.

(If you'd rather do it by hand: `npm install -g @anthropic-ai/claude-code`)

## 4. First login

```powershell
claude
```

First launch opens a login flow (browser-based) — sign in with your normal
Claude account. After that it stays logged in on this machine.

## 5. Drop in the CLAUDE.md context file

Copy `CLAUDE.md` (also in this folder) into the VPS bot folder, next to
`xauusd_mt5_strategy.py`. Claude Code reads this file automatically every
time it starts in that folder, so it already knows the bot's architecture,
key files, and where logs/state live — you won't need to re-explain it each
session.

## 6. Try it

From inside the bot folder:
```powershell
claude
```
Then ask things like:
- "Read the last 100 lines of xauusd_mt5_strategy.log and tell me if anything looks wrong."
- "Check bot_state.json and strategy_scores.json — is the bot currently running and what's its current bias?"
- "Why didn't the Day Trade group fire on the last few scans?"
- "Open strategy_config.json and tell me what ENTRY_MODE and LOGIC_GROUP_SELECTION are set to."

## What this gives you (and what it doesn't)

- **It gives you**: an on-demand expert that can read the actual files on the
  VPS — log, config, dashboard JSON, code — and explain or fix issues, live,
  without you taking screenshots.
- **It does NOT give you**: a 24/7 autonomous watcher. Claude Code only runs
  when you start it (`claude`) or when you set up a scheduled task that
  invokes it headlessly (e.g. `claude -p "check the log for errors"` on a
  Windows Task Scheduler trigger) — that's a separate, optional step if you
  want periodic automated checks.
- **It will never**: execute real trades, change risk settings, or modify
  the EA's behavior without you reviewing the change first.

## Notes / gotchas

- Run the install as the same Windows user that runs the EA, so file
  permissions on the bot folder line up.
- Don't run PowerShell "as Administrator" for the npm install — it's not
  needed and can cause permission quirks with global npm packages.
- Your `strategy_config.json` contains your Telegram bot token / chat ID.
  Claude Code running locally on the VPS can read that file like any other —
  treat the VPS itself as the security boundary (same as today), and avoid
  pasting that file's contents into any other tool or chat.
