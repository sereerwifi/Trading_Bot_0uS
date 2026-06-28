"""Waits 60 seconds then minimizes all visible windows EXCEPT MT5 terminal.
Launched automatically alongside the bot by launch_bot.py."""
import time
import ctypes
import ctypes.wintypes
import psutil

time.sleep(60)

user32  = ctypes.windll.user32
SW_MINIMIZE = 6

# Collect PIDs of MT5 terminal processes to exclude
MT5_EXE_NAMES = {"terminal64.exe", "terminal.exe"}
mt5_pids = set()
for proc in psutil.process_iter(["pid", "name"]):
    try:
        if proc.info["name"] and proc.info["name"].lower() in MT5_EXE_NAMES:
            mt5_pids.add(proc.info["pid"])
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
    if pid.value in mt5_pids:
        return True  # skip MT5 windows
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
user32.EnumWindows(WNDENUMPROC(_minimize_callback), 0)
