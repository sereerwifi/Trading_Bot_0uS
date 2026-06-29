"""Bot launcher — kills any stale bot processes, then opens the config UI.
Use the UI's '▶ Start Bot' button to start the EA and all supporting
processes. The bot will NOT start automatically on launch.

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


if __name__ == "__main__":
    print("Killing stale bot processes...")
    kill_stale()

    print("Opening config UI — use '▶ Start Bot' to start the EA.")
    ui_path = os.path.join(THIS_DIR, "strategy_config_ui.py")
    subprocess.Popen([sys.executable, ui_path], cwd=THIS_DIR)
