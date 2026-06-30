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
    user32     = ctypes.windll.user32
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
