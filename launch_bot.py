"""Bot launcher — kills any stale bot processes, then opens the config UI.

Two modes:
  python launch_bot.py             -- opens UI only; click '▶ Start Bot'
                                      to start the EA + all support processes.
  python launch_bot.py --autostart -- opens UI AND auto-triggers Start Bot
                                      after 1.5 s (same code path as clicking
                                      the button, so dashboard watcher, web
                                      server, Cloudflare tunnel, and backup
                                      watch all start correctly). Use this
                                      after a git push/sync to restart the
                                      bot through the UI sequence rather than
                                      spawning xauusd_mt5_strategy.py directly.
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
    os.path.join(THIS_DIR, "strategy_config_ui.py"),
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
    autostart = "--autostart" in sys.argv

    print("Killing stale bot processes...")
    kill_stale()

    ui_path = os.path.join(THIS_DIR, "strategy_config_ui.py")
    if autostart:
        print("Opening config UI with --autostart (Start Bot will fire automatically)...")
        subprocess.Popen([sys.executable, ui_path, "--autostart"], cwd=THIS_DIR)
    else:
        print("Opening config UI — use '▶ Start Bot' to start the EA.")
        subprocess.Popen([sys.executable, ui_path], cwd=THIS_DIR)
