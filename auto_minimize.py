"""Waits 60 seconds then minimizes all visible windows EXCEPT MT5 terminal.
Launched automatically alongside the bot by launch_bot.py."""
import time
import ctypes
import ctypes.wintypes
import psutil

time.sleep(60)

user32  = ctypes.windll.user32
SW_MINIMIZE = 6

# Collect PIDs to exclude from minimization: MT5 terminal + config UI
EXCLUDE_EXE_NAMES = {"terminal64.exe", "terminal.exe"}
EXCLUDE_SCRIPT_FRAGMENTS = {"strategy_config_ui.py"}
exclude_pids = set()
for proc in psutil.process_iter(["pid", "name", "cmdline"]):
    try:
        name = (proc.info["name"] or "").lower()
        if name in EXCLUDE_EXE_NAMES:
            exclude_pids.add(proc.info["pid"])
        cmd = " ".join(proc.info["cmdline"] or [])
        if any(frag in cmd for frag in EXCLUDE_SCRIPT_FRAGMENTS):
            exclude_pids.add(proc.info["pid"])
    except Exception:
        pass

def _minimize_callback(hwnd, _):
    if not user32.IsWindowVisible(hwnd):
        return True
    if user32.GetWindowTextLengthW(hwnd) == 0:
        return True
    # Get the PID that owns this window
    pid = ctypes.wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in exclude_pids:
        return True  # skip MT5 and config UI windows
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
user32.EnumWindows(WNDENUMPROC(_minimize_callback), 0)
