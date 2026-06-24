"""
bot_monitor.py — Watchdog that alerts via Telegram when the trading bot stops.
==============================================================================
Run this as a separate process (e.g. keep a CMD window open, or add to Task
Scheduler). It checks every POLL_SECONDS whether:
  1. The bot Python process (xauusd_mt5_strategy.py) is running, AND
  2. The log file has been updated within LOG_STALE_SECONDS

If either check fails it sends a Telegram alert. It also sends a recovery
alert when the bot comes back up.

Usage:
    python bot_monitor.py              # runs forever, checks every 60s
    python bot_monitor.py --once       # single check, exit 0=ok / 1=problem
    python bot_monitor.py --restart    # alert + auto-restart the bot on failure

Config is read from strategy_config.json (same token/chat_id the bot uses).
Never modify strategy_config.json directly — use strategy_config_ui.py.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(THIS_DIR, "strategy_config.json")
LOG_PATH    = os.path.join(THIS_DIR, "logs", "xauusd_ea.log")
BOT_SCRIPT  = os.path.join(THIS_DIR, "xauusd_mt5_strategy.py")

POLL_SECONDS     = 60      # how often to check
LOG_STALE_SECONDS = 180    # alert if log hasn't been written for this long (3 min)
ALERT_COOLDOWN   = 300     # don't repeat the same alert more often than this (5 min)


def load_telegram_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        tg = cfg.get("telegram", {})
        return tg.get("bot_token") or None, str(tg.get("chat_id") or "")
    except Exception:
        return None, None


def send_telegram(token, chat_id, text):
    import urllib.request, urllib.parse
    if not token or not chat_id:
        print(f"[monitor] Telegram not configured — message not sent: {text[:80]}")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        return True
    except Exception as exc:
        print(f"[monitor] Telegram send failed: {exc}")
        return False


def is_bot_running():
    """Returns True if xauusd_mt5_strategy.py is in the running Python processes."""
    try:
        import subprocess
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=10
        )
        return "xauusd_mt5_strategy" in result.stdout
    except Exception:
        # Fallback: check if any python process is running (less precise)
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe"],
                capture_output=True, text=True, timeout=10
            )
            return "python.exe" in result.stdout
        except Exception:
            return False


def log_is_fresh():
    """Returns (is_fresh, age_seconds). is_fresh=False if log missing or stale."""
    try:
        mtime = os.path.getmtime(LOG_PATH)
        age = time.time() - mtime
        return age < LOG_STALE_SECONDS, age
    except FileNotFoundError:
        return False, float("inf")


def restart_bot():
    """Launches the bot in a new window."""
    try:
        subprocess.Popen(
            [sys.executable, BOT_SCRIPT],
            cwd=THIS_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        return True
    except Exception as exc:
        print(f"[monitor] Auto-restart failed: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="XAUUSD bot watchdog")
    parser.add_argument("--once",    action="store_true", help="Single check then exit")
    parser.add_argument("--restart", action="store_true", help="Auto-restart bot on failure")
    args = parser.parse_args()

    print(f"[monitor] Bot watchdog started  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"[monitor] Poll interval: {POLL_SECONDS}s | Log stale threshold: {LOG_STALE_SECONDS}s")
    if args.restart:
        print("[monitor] Auto-restart: ON")

    token, chat_id = load_telegram_config()
    if not token:
        print("[monitor] WARNING: Telegram not configured — alerts will only print to console.")

    last_alert_time  = 0.0
    last_alert_msg   = ""
    was_down         = False

    while True:
        now = datetime.now()
        process_ok = is_bot_running()
        log_fresh, log_age = log_is_fresh()
        log_age_str = f"{log_age:.0f}s" if log_age < float("inf") else "missing"

        # Determine status
        if not process_ok:
            status = "STOPPED"
            problem = f"Bot process (xauusd_mt5_strategy.py) is NOT running."
        elif not log_fresh:
            status = "STALE"
            problem = f"Bot process running but log not updated for {log_age_str}."
        else:
            status = "OK"
            problem = None

        print(f"[monitor] {now:%H:%M:%S}  process={'RUN' if process_ok else 'STOPPED'}  "
              f"log_age={log_age_str}  -> {status}")

        if problem:
            # Bot is down
            alert_msg = (
                f"⛔ <b>XAUUSD Bot {status}</b>\n"
                f"{problem}\n"
                f"Time: {now:%Y-%m-%d %H:%M:%S}\n"
                f"VPS: {os.environ.get('COMPUTERNAME', 'unknown')}"
            )
            cooldown_ok = (time.time() - last_alert_time) > ALERT_COOLDOWN
            if cooldown_ok or alert_msg != last_alert_msg:
                sent = send_telegram(token, chat_id, alert_msg)
                print(f"[monitor] Alert sent: {sent}")
                last_alert_time = time.time()
                last_alert_msg  = alert_msg

            if args.restart and not process_ok:
                print("[monitor] Attempting auto-restart...")
                time.sleep(3)
                ok = restart_bot()
                if ok:
                    time.sleep(8)
                    if is_bot_running():
                        restart_msg = (
                            f"✅ <b>XAUUSD Bot Restarted</b>\n"
                            f"Auto-restart succeeded.\n"
                            f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}"
                        )
                        send_telegram(token, chat_id, restart_msg)
                        print("[monitor] Restart confirmed — bot is running.")
                    else:
                        send_telegram(token, chat_id,
                            "⚠️ <b>Auto-restart FAILED</b> — bot still not running. "
                            "Manual intervention required.")
            was_down = True

        else:
            # Bot is up
            if was_down:
                recovery_msg = (
                    f"✅ <b>XAUUSD Bot Recovered</b>\n"
                    f"Bot is running and log is fresh.\n"
                    f"Time: {now:%Y-%m-%d %H:%M:%S}"
                )
                send_telegram(token, chat_id, recovery_msg)
                print("[monitor] Recovery alert sent.")
                was_down = False
                last_alert_msg = ""

        if args.once:
            sys.exit(0 if status == "OK" else 1)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
