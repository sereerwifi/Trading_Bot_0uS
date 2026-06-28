"""Full bot launch script — kills any stale processes first, then starts
EA, dashboard --watch, web server, cloudflared tunnel, backup watcher,
and auto-minimize (minimizes all windows 60s after launch).

Run via start_bot.bat or: python launch_bot.py
"""
import subprocess
import sys
import os
import time
import psutil

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CF_DIR   = r"C:\Users\Administrator\Desktop"

TARGETS = [
    os.path.join(THIS_DIR, "xauusd_mt5_strategy.py"),
    os.path.join(THIS_DIR, "generate_dashboard.py"),
    os.path.join(THIS_DIR, "dashboard_server.py"),
    os.path.join(THIS_DIR, "backup_restore.py"),
]
CLOUDFLARED_EXE  = os.path.join(CF_DIR, "cloudflared.exe")
TUNNEL_NAME      = "sereewifi-dashboard"
DASHBOARD_PORT   = 8787
BACKUP_DIR       = os.path.join(THIS_DIR, "backups")


def kill_stale():
    my_pid = os.getpid()
    killed = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.info["pid"] == my_pid:
                continue
            cmd = " ".join(proc.info["cmdline"] or [])
            if any(t in cmd for t in TARGETS):
                proc.kill()
                killed.append(proc.info["pid"])
            elif (CLOUDFLARED_EXE.lower() in cmd.lower()
                  and TUNNEL_NAME in cmd):
                proc.kill()
                killed.append(proc.info["pid"])
        except Exception:
            pass
    if killed:
        print(f"Stopped {len(killed)} stale process(es): {killed}")
        time.sleep(2)


def launch(args, cwd=THIS_DIR, console=True):
    flags = subprocess.CREATE_NEW_CONSOLE if console else 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(args, cwd=cwd, creationflags=flags)


if __name__ == "__main__":
    print("Killing stale processes...")
    kill_stale()

    print("Starting bot processes...")
    launch([sys.executable, os.path.join(THIS_DIR, "xauusd_mt5_strategy.py")])
    launch([sys.executable, os.path.join(THIS_DIR, "generate_dashboard.py"), "--watch", "--interval", "60"])
    launch([sys.executable, os.path.join(THIS_DIR, "dashboard_server.py"), "--port", str(DASHBOARD_PORT)])
    launch([CLOUDFLARED_EXE, "tunnel", "run", TUNNEL_NAME], cwd=CF_DIR)
    launch([sys.executable, os.path.join(THIS_DIR, "backup_restore.py"),
            "watch", "--out", BACKUP_DIR, "--interval-hours", "6", "--keep", "28"])
    launch([sys.executable, os.path.join(THIS_DIR, "auto_minimize.py")], console=False)

    print("All processes launched. Windows will minimize in 60 seconds.")
