"""
backup_restore.py — full-state backup/restore for the XAUUSD MT5 EA project.

PURPOSE
-------
The EA, dashboard, league system, and macro/Big-Data fetcher all persist
state as plain files next to the code (JSON snapshots + one SQLite history
DB). None of that lives in MetaTrader itself, so moving to a new VPS by
copying only the .py files would silently reset:

  - strategy_config.json        — all UI settings (weights, sessions, risk,
                                   Telegram bot token/chat id, dashboard
                                   web login)
  - strategy_league.json        — League System: each strategy's running
                                   win/loss record and bench/unbench state
  - macro_data_history.db       — SQLite history of every macro/Big-Data
                                   snapshot ever fetched (DXY, US10Y, Fed
                                   expectation, ETF flow, COT, COMEX) — the
                                   long-run dataset behind "use Big Data as
                                   baseline", not reconstructable after the
                                   fact
  - macro_data_cache.json       — latest macro snapshot + per-source TTL
                                   cache (avoids re-hitting rate-limited
                                   sources right after restart)
  - open_entry_meta.json        — which strategies contributed to each
                                   currently-OPEN position; lost = the
                                   League System can't attribute that
                                   trade's eventual win/loss to anyone
  - strategy_scores.json        — latest confluence scan (dashboard)
  - processed_deals.json        — dedupe state so closed trades aren't
                                   double-counted into the League System
  - news_alert_state.json       — dedupe state for pre/post-news Telegram
                                   alerts
  - logs/                       — rotated EA log files (trade history /
                                   audit trail for analysis)

This module zips all of the above into one timestamped archive and can
restore that archive on a fresh machine, so strategy learning (League
System), macro history, and open-position bookkeeping survive a VPS move
without interruption.

CLI
---
  python backup_restore.py backup                  # one-off backup now
  python backup_restore.py backup --keep 28         # ...and prune old ones
  python backup_restore.py watch --interval-hours 6 --keep 28
                                                     # run forever, backing
                                                     # up on a schedule (use
                                                     # this for continuous
                                                     # protection — same
                                                     # pattern as
                                                     # generate_dashboard.py
                                                     # --watch)
  python backup_restore.py restore backups/foo.zip  # restore onto THIS
                                                     # machine (e.g. the new
                                                     # VPS, after copying the
                                                     # .zip over)
  python backup_restore.py list                     # show backups + sizes

Restoring makes its own safety-backup of whatever is currently on disk
before overwriting anything, so a restore can never destroy data that
wasn't already captured somewhere.
"""

import argparse
import os
import shutil
import sys
import time
import zipfile
from datetime import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BACKUP_DIR = os.path.join(THIS_DIR, "backups")

# Files that matter for continuity of analysis/strategy decision-making.
# Paths are relative to THIS_DIR; missing files are skipped silently (e.g.
# a fresh install before the EA has ever run won't have league/macro state
# yet, and that's fine).
BACKUP_FILES = [
    "strategy_config.json",
    "strategy_league.json",
    "macro_data_history.db",
    "macro_data_cache.json",
    "open_entry_meta.json",
    "strategy_scores.json",
    "processed_deals.json",
    "news_alert_state.json",
]

# Directories backed up recursively (rotated log files).
BACKUP_DIRS = [
    "logs",
]

MANIFEST_NAME = "BACKUP_MANIFEST.txt"


def _existing_files():
    return [f for f in BACKUP_FILES if os.path.exists(os.path.join(THIS_DIR, f))]


def _existing_dir_files():
    out = []
    for d in BACKUP_DIRS:
        abs_d = os.path.join(THIS_DIR, d)
        if not os.path.isdir(abs_d):
            continue
        for root, _dirs, files in os.walk(abs_d):
            for fn in files:
                abs_path = os.path.join(root, fn)
                rel_path = os.path.relpath(abs_path, THIS_DIR)
                out.append(rel_path)
    return out


def create_backup(out_dir=None, label=None):
    """Zips every state file/dir listed above into one timestamped archive.
    Returns the absolute path to the created .zip. Safe to call while the
    EA is running — files are read individually, so at worst a single JSON
    snapshot could be caught mid-write (it will simply be picked up fresh
    on the *next* backup; nothing is corrupted on disk since the EA writes
    via the standard tmp-file-then-os.replace pattern)."""
    out_dir = out_dir or DEFAULT_BACKUP_DIR
    os.makedirs(out_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"backup_{ts}" + (f"_{label}" if label else "") + ".zip"
    zip_path = os.path.join(out_dir, name)

    files = _existing_files()
    dir_files = _existing_dir_files()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files + dir_files:
            zf.write(os.path.join(THIS_DIR, rel), arcname=rel)
        manifest = (
            f"XAUUSD MT5 EA — backup snapshot\n"
            f"Created: {datetime.now().isoformat()}\n"
            f"Source machine dir: {THIS_DIR}\n"
            f"Files included ({len(files) + len(dir_files)}):\n"
            + "\n".join(f"  - {f}" for f in files + dir_files)
            + "\n\nTo restore on a new VPS:\n"
              "  1. Copy this .zip onto the new machine, into the project folder.\n"
              "  2. Run: python backup_restore.py restore <this_file>.zip\n"
              "  3. Start the bot normally (strategy_config_ui.py -> Start Bot).\n"
        )
        zf.writestr(MANIFEST_NAME, manifest)

    return zip_path


def prune_old_backups(out_dir=None, keep=28):
    """Keeps only the `keep` most recent backup_*.zip files in out_dir,
    deleting older ones. keep<=0 disables pruning (keep everything)."""
    out_dir = out_dir or DEFAULT_BACKUP_DIR
    if keep is None or keep <= 0 or not os.path.isdir(out_dir):
        return []
    zips = sorted(
        (os.path.join(out_dir, f) for f in os.listdir(out_dir)
         if f.startswith("backup_") and f.endswith(".zip")),
        key=os.path.getmtime,
        reverse=True,
    )
    removed = []
    for path in zips[keep:]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def list_backups(out_dir=None):
    out_dir = out_dir or DEFAULT_BACKUP_DIR
    if not os.path.isdir(out_dir):
        return []
    rows = []
    for f in sorted(os.listdir(out_dir)):
        if f.startswith("backup_") and f.endswith(".zip"):
            path = os.path.join(out_dir, f)
            size_kb = os.path.getsize(path) / 1024
            mtime = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
            rows.append((f, size_kb, mtime, path))
    return rows


def restore_backup(zip_path, target_dir=None, make_safety_backup=True):
    """Extracts a backup archive on top of target_dir (defaults to this
    project's own directory — i.e. run this ON the new VPS after copying
    the .zip there). Before overwriting anything, takes a safety backup of
    whatever currently exists at target_dir under <target_dir>/backups/
    (suffixed "_pre_restore"), so restoring can never silently destroy
    data that hadn't been captured anywhere else yet."""
    target_dir = target_dir or THIS_DIR
    if not os.path.exists(zip_path):
        raise FileNotFoundError(zip_path)

    safety_path = None
    if make_safety_backup:
        try:
            safety_path = create_backup(
                out_dir=os.path.join(target_dir, "backups"), label="pre_restore"
            )
        except Exception:
            safety_path = None  # don't block a restore over a failed safety backup

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if n != MANIFEST_NAME]
        zf.extractall(target_dir, members=names)

    return {"restored_files": names, "safety_backup": safety_path}


def _fmt_size(kb):
    return f"{kb:,.1f} KB" if kb < 1024 else f"{kb / 1024:,.1f} MB"


def main():
    parser = argparse.ArgumentParser(description="Backup/restore all EA state for VPS migration.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup", help="Create one backup now.")
    p_backup.add_argument("--out", default=None, help="Backup output directory (default: ./backups)")
    p_backup.add_argument("--keep", type=int, default=0, help="Prune to keep only N most recent backups (0 = keep all)")
    p_backup.add_argument("--label", default=None, help="Optional label appended to the filename")

    p_watch = sub.add_parser("watch", help="Back up on a recurring schedule, forever.")
    p_watch.add_argument("--out", default=None)
    p_watch.add_argument("--interval-hours", type=float, default=6.0)
    p_watch.add_argument("--keep", type=int, default=28)

    p_restore = sub.add_parser("restore", help="Restore a backup onto this machine.")
    p_restore.add_argument("zip_path")
    p_restore.add_argument("--target", default=None, help="Target dir (default: this script's own dir)")
    p_restore.add_argument("--no-safety-backup", action="store_true")

    p_list = sub.add_parser("list", help="List existing backups.")
    p_list.add_argument("--out", default=None)

    args = parser.parse_args()

    if args.cmd == "backup":
        path = create_backup(out_dir=args.out, label=args.label)
        print(f"Backup created: {path}")
        if args.keep > 0:
            removed = prune_old_backups(out_dir=args.out, keep=args.keep)
            for r in removed:
                print(f"Pruned old backup: {r}")

    elif args.cmd == "watch":
        print(f"Backing up every {args.interval_hours}h, keeping last {args.keep}. Ctrl+C to stop.")
        while True:
            try:
                path = create_backup(out_dir=args.out)
                print(f"[{datetime.now().isoformat(timespec='seconds')}] Backup created: {path}")
                removed = prune_old_backups(out_dir=args.out, keep=args.keep)
                for r in removed:
                    print(f"  pruned: {r}")
            except Exception as e:
                print(f"Backup failed: {e}", file=sys.stderr)
            time.sleep(max(args.interval_hours, 0.05) * 3600)

    elif args.cmd == "restore":
        result = restore_backup(
            args.zip_path, target_dir=args.target,
            make_safety_backup=not args.no_safety_backup,
        )
        print(f"Restored {len(result['restored_files'])} files into "
              f"{args.target or THIS_DIR}:")
        for f in result["restored_files"]:
            print(f"  - {f}")
        if result["safety_backup"]:
            print(f"Safety backup of pre-existing state saved to: {result['safety_backup']}")

    elif args.cmd == "list":
        rows = list_backups(args.out)
        if not rows:
            print("No backups found.")
        for name, size_kb, mtime, _path in rows:
            print(f"{mtime}  {_fmt_size(size_kb):>10}  {name}")


if __name__ == "__main__":
    main()
