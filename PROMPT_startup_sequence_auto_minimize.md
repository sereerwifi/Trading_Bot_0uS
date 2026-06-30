# Prompt for Claude Code (run ON THE VPS) — startup sequence + auto-minimize bot windows

Paste into Claude Code **on the VPS** in
`C:\Users\Administrator\Desktop\RoBotTrading man 0 V10`.

---

## What this prompt implements

After pressing **Start Bot** in the config UI, the bot now automatically:

1. Kills all stale bot processes (already done by `launch_bot.py` and the
   UI's `kill_stale_on_start` setting — no change needed here)
2. Opens the config UI (`strategy_config_ui.py`) — no change
3. User clicks **▶ Start Bot (Save + Run All)** → config is saved, bot +
   dashboard + web server + tunnel + backup watcher all start — no change
4. **NEW:** 15 seconds after Start Bot fires, `auto_minimize.py` runs
   silently in the background and minimizes every bot-related console window
   while leaving MT5 (terminal64.exe / terminal.exe) and every other
   unrelated app the user has open completely untouched.
5. MT5 stays visible. Nothing that isn't a bot window is touched.

**Old `auto_minimize.py` behaviour (before this change):** minimized ALL
visible windows except MT5 — that included any browser, File Explorer, or
other app the user had open. Now it only touches windows whose owning
process is a known bot script.

---

## Step 0 — Safety

- Check for open positions before restarting the bot.
- `strategy_config.json` must never be printed, shown, or committed.
- No strategy weights, thresholds, or risk parameters change here.

---

## Step 1 — Pull the latest commits

```powershell
cd "C:\Users\Administrator\Desktop\RoBotTrading man 0 V10"
git pull origin main
```

After pulling, verify with `git log --oneline -3`. The two relevant commits
are:
- `auto_minimize.py` rewrite
- `strategy_config_ui.py` wired launch

If the pull brings those in cleanly, **skip Steps 2 and 3** — go straight
to Step 4 (verify + test).

If there are merge conflicts on either file, resolve them using Steps 2–3
below instead of accepting either side blindly.

---

## Step 2 — Apply the `auto_minimize.py` rewrite

Replace the **entire contents** of `auto_minimize.py` with the following.
The key changes from the old version:
- Waits 15 s (was 60 s)
- Minimizes **only bot-related windows** (was: everything except MT5)
- Accepts `--delay N` argument so `start_bot()` can control timing
- Leaves MT5 AND all other non-bot windows completely untouched

```python
"""Minimize bot-related windows after Start Bot fires, leaving MT5 and all
other non-bot windows untouched.

Launched automatically by strategy_config_ui.py's start_bot() with a short
delay so every bot console window has time to appear before the sweep runs.

Usage (internal — not meant to be run manually):
    python auto_minimize.py [--delay N]   # N seconds to wait, default 15

Old behaviour (< 2026-06-30): minimized EVERY visible window except MT5,
which nuked unrelated apps the user had open. New behaviour: only windows
owned by bot-related Python processes are minimized. MT5 stays. Everything
else is untouched.
"""
import sys
import time
import ctypes
import ctypes.wintypes
import psutil

# Bot-related script filenames — processes whose cmdline contains any of
# these will have their windows minimized.
_BOT_SCRIPT_FRAGMENTS = {
    "xauusd_mt5_strategy.py",
    "generate_dashboard.py",
    "dashboard_server.py",
    "backup_restore.py",
    "strategy_config_ui.py",
    "bot_monitor.py",
    "auto_minimize.py",
    "launch_bot.py",
}

# Native executables to KEEP visible — matched against process .name() (lower).
_KEEP_EXE_NAMES = {"terminal64.exe", "terminal.exe"}   # MT5


def _collect_pids():
    """Return (bot_pids, keep_pids): sets of PIDs to minimize vs. protect."""
    bot_pids = set()
    keep_pids = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmd  = " ".join(proc.info["cmdline"] or [])
            if name in _KEEP_EXE_NAMES:
                keep_pids.add(proc.info["pid"])
            if any(frag in cmd for frag in _BOT_SCRIPT_FRAGMENTS):
                bot_pids.add(proc.info["pid"])
        except Exception:
            pass
    return bot_pids, keep_pids


def minimize_bot_windows():
    """Enumerate all visible windows; minimize those belonging to bot
    processes; leave MT5 and every other window completely alone."""
    user32      = ctypes.windll.user32
    SW_MINIMIZE = 6

    bot_pids, keep_pids = _collect_pids()

    def _callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True                            # invisible — skip
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True                            # no title — skip
        pid = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in keep_pids:
            return True                            # MT5 — always keep visible
        if pid.value in bot_pids:
            user32.ShowWindow(hwnd, SW_MINIMIZE)   # bot window — minimize
        # anything else (browser, editor, etc.) — leave alone
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
    user32.EnumWindows(WNDENUMPROC(_callback), 0)


if __name__ == "__main__":
    delay = 15
    args  = sys.argv[1:]
    if "--delay" in args:
        try:
            delay = int(args[args.index("--delay") + 1])
        except (IndexError, ValueError):
            pass
    elif args:
        # also accept a bare integer: auto_minimize.py 20
        try:
            delay = int(args[0])
        except ValueError:
            pass

    time.sleep(delay)
    minimize_bot_windows()
```

Verify: `python -m py_compile auto_minimize.py` → no output = success.

---

## Step 3 — Wire `auto_minimize.py` into `strategy_config_ui.py`'s `start_bot()`

Open `strategy_config_ui.py`. Find this exact block (it's right after all
the bot processes are spawned, just before `self.btn_start.config(...)`):

```python
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._poll_processes()
        web_note = (
```

Insert the following **before** that block (keep the existing lines, add
the new ones above them):

```python
        # Launch auto_minimize.py in the background — waits 15 s then
        # minimizes every bot-related console window while keeping MT5
        # and any other unrelated app the user has open completely untouched.
        _auto_min = os.path.join(THIS_DIR, "auto_minimize.py")
        if os.path.exists(_auto_min):
            _min_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            subprocess.Popen(
                [sys.executable, _auto_min, "--delay", "15"],
                cwd=THIS_DIR,
                **({"creationflags": _min_flags} if sys.platform == "win32" else {}),
            )

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._poll_processes()
        web_note = (
```

Verify: `python -m py_compile strategy_config_ui.py` → no output = success.

---

## Step 4 — Commit

```powershell
git add auto_minimize.py strategy_config_ui.py
git commit -m "Startup: auto-minimize only bot windows after Start Bot; keep MT5 + other apps untouched

auto_minimize.py:
- Rewrote window sweep: now targets ONLY windows owned by bot-related
  processes (xauusd_mt5_strategy.py, generate_dashboard.py,
  dashboard_server.py, backup_restore.py, strategy_config_ui.py,
  bot_monitor.py). Old code minimized EVERY window except MT5, killing
  unrelated apps.
- MT5 (terminal64.exe / terminal.exe) always kept visible.
- All other windows (browser, editor, File Explorer, etc.) left untouched.
- Wait reduced 60s -> 15s; accepts --delay N argument.

strategy_config_ui.py start_bot():
- Spawns auto_minimize.py (CREATE_NO_WINDOW, no visible console) after all
  bot processes start. Runs automatically on every Start Bot click — no
  manual step needed."
```

After commit: `git log -1 --format=%h` — note the real hash.
Push: `git push origin main`

---

## Step 5 — Test

1. Run `start_bot.bat` (or `python launch_bot.py`) — the config UI opens.
2. Click **▶ Start Bot (Save + Run All)**.
3. Dismiss the startup confirmation dialog.
4. Wait ~15 seconds.
5. Expected result:
   - All bot console windows (EA loop, dashboard watcher, web server, etc.)
     are minimized to the taskbar.
   - MT5 terminal stays visible and in focus.
   - Any other apps you had open (browser, explorer, etc.) are untouched.
   - Taskbar shows the minimized bot windows — click any to restore.

To adjust the delay (e.g. if 15 s isn't enough for all windows to appear on
a slow VPS): change `"15"` to a higher value in the `subprocess.Popen` call
in `strategy_config_ui.py`'s `start_bot()`. No other change needed.

---

## Verification checklist

- [ ] `python -m py_compile auto_minimize.py` passes
- [ ] `python -m py_compile strategy_config_ui.py` passes
- [ ] After clicking Start Bot, bot windows minimize after ~15 s
- [ ] MT5 terminal stays visible throughout
- [ ] Browser / File Explorer / other unrelated windows are NOT minimized
- [ ] `strategy_config.json` was never shown or committed
- [ ] Commit has a real hash from `git log`, not copied from another machine
