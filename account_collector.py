"""
account_collector.py
====================
Polls balance/equity snapshots for multiple MT5 accounts configured in
strategy_config.json -> "accounts": {"list": [...]}.

Integration model (Option B — single-process, in-loop):
  Runs inside the EA's main loop, between scans, at most once every
  COLLECT_INTERVAL_SEC (default 900 s / 15 min). For each secondary account
  the collector does: mt5.login(secondary) -> read -> mt5.login(primary)
  inside a try/finally so the primary is always restored even on error.
  Primary account data is read without any login switch (already connected).

DB: account_balance_history.db
Table: balance_snapshots(id, account_label, mt5_login, timestamp,
       balance, equity, margin, profit, currency)
"""
import logging
import os
import sqlite3
import time

logger = logging.getLogger("xauusd_ea")

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE   = os.path.join(_THIS_DIR, "account_balance_history.db")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS balance_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        account_label TEXT    NOT NULL,
        mt5_login     INTEGER NOT NULL,
        timestamp     REAL    NOT NULL,
        balance       REAL,
        equity        REAL,
        margin        REAL,
        profit        REAL,
        currency      TEXT
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_label_ts "
        "ON balance_snapshots(account_label, timestamp)"
    )
    conn.commit()
    return conn


def _save_snapshot(label, login, info):
    """Append one row. Best-effort — never raises."""
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT INTO balance_snapshots "
            "(account_label, mt5_login, timestamp, balance, equity, margin, profit, currency) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                label, int(login), time.time(),
                getattr(info, "balance",  None),
                getattr(info, "equity",   None),
                getattr(info, "margin",   None),
                getattr(info, "profit",   None),
                getattr(info, "currency", None),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception(f"[account_collector] DB write failed for '{label}'")


# ---------------------------------------------------------------------------
# Core collector
# ---------------------------------------------------------------------------

def collect_accounts(accounts_cfg, primary_login):
    """Iterate accounts_cfg and save a balance snapshot for each enabled entry.

    accounts_cfg  — list of dicts from strategy_config.json "accounts.list"
    primary_login — int login of the account the bot is currently logged in as
                    (stored after connect() in xauusd_mt5_strategy.main())

    For primary account: reads mt5.account_info() directly, no login switch.
    For secondaries:     mt5.login(secondary) -> read -> mt5.login(primary)
                         in a try/finally; logs ERROR if restore fails.
    Accounts without a password configured are skipped (warning logged).
    """
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return

    if not accounts_cfg:
        return

    # Find primary entry so we can restore after each secondary switch.
    primary_entry = next(
        (a for a in accounts_cfg
         if a.get("enabled", True) and int(a.get("mt5_login") or 0) == int(primary_login or 0)),
        None,
    )
    primary_password = (primary_entry or {}).get("mt5_password", "")
    primary_server   = (primary_entry or {}).get("mt5_server",   "")

    for acct in accounts_cfg:
        if not acct.get("enabled", True):
            continue
        label    = acct.get("label") or str(acct.get("mt5_login", "unknown"))
        login    = acct.get("mt5_login")
        password = acct.get("mt5_password", "")
        server   = acct.get("mt5_server",   "")

        if not login:
            continue

        try:
            if int(login) == int(primary_login or 0):
                # Primary — already connected, no switch needed.
                info = mt5.account_info()
                if info is not None:
                    _save_snapshot(label, login, info)
                    logger.debug(f"[account_collector] Saved primary '{label}' balance={info.balance}")
            else:
                # Secondary — switch, read, restore.
                if not password:
                    logger.warning(
                        f"[account_collector] Skipping '{label}' (login {login}): "
                        f"no password configured in accounts.list."
                    )
                    continue
                switched = False
                try:
                    ok = mt5.login(int(login), password=password, server=server)
                    if ok:
                        switched = True
                        info = mt5.account_info()
                        if info is not None:
                            _save_snapshot(label, login, info)
                            logger.debug(f"[account_collector] Saved '{label}' balance={info.balance}")
                        else:
                            logger.warning(f"[account_collector] account_info() returned None for '{label}'")
                    else:
                        logger.warning(
                            f"[account_collector] Login failed for '{label}' "
                            f"(login {login}): {mt5.last_error()}"
                        )
                finally:
                    if switched:
                        if primary_login and primary_password:
                            restored = mt5.login(
                                int(primary_login),
                                password=primary_password,
                                server=primary_server,
                            )
                            if not restored:
                                logger.error(
                                    f"[account_collector] CRITICAL: failed to restore primary "
                                    f"account {primary_login} after collecting '{label}'. "
                                    f"MT5 error: {mt5.last_error()}. "
                                    f"Bot will reconnect on next connect() call."
                                )
                        else:
                            logger.error(
                                f"[account_collector] Cannot restore primary account: "
                                f"primary_login or primary_password missing from accounts.list. "
                                f"Add the primary account entry with its password so the bot "
                                f"can switch back after reading secondary accounts."
                            )
        except Exception:
            logger.exception(f"[account_collector] Unexpected error collecting '{label}'")


# ---------------------------------------------------------------------------
# Read helpers (used by generate_dashboard.py)
# ---------------------------------------------------------------------------

def get_account_history(label, limit=500):
    """Return up to `limit` snapshots for `label`, oldest-first."""
    try:
        conn  = _db_connect()
        rows  = conn.execute(
            "SELECT timestamp, balance, equity, margin, profit, currency, mt5_login "
            "FROM balance_snapshots WHERE account_label=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (label, limit),
        ).fetchall()
        conn.close()
        return [
            {
                "timestamp": r[0], "balance": r[1], "equity": r[2],
                "margin": r[3],    "profit":  r[4], "currency": r[5],
                "login": r[6],
            }
            for r in reversed(rows)
        ]
    except Exception:
        return []


def get_all_labels():
    """Sorted list of distinct account_label values in the DB."""
    try:
        conn  = _db_connect()
        rows  = conn.execute(
            "SELECT DISTINCT account_label FROM balance_snapshots ORDER BY account_label"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


def get_latest_snapshot(label):
    """Most-recent row for `label`, or None."""
    try:
        conn = _db_connect()
        row  = conn.execute(
            "SELECT timestamp, balance, equity, margin, profit, currency, mt5_login "
            "FROM balance_snapshots WHERE account_label=? "
            "ORDER BY timestamp DESC LIMIT 1",
            (label,),
        ).fetchone()
        conn.close()
        if row:
            return {
                "timestamp": row[0], "balance": row[1], "equity": row[2],
                "margin": row[3],    "profit":  row[4], "currency": row[5],
                "login": row[6],
            }
    except Exception:
        pass
    return None


def clear_account_history(label):
    """Delete all rows for `label`. Returns True on success."""
    try:
        conn = _db_connect()
        conn.execute("DELETE FROM balance_snapshots WHERE account_label=?", (label,))
        conn.commit()
        conn.close()
        logger.info(f"[account_collector] Cleared history for '{label}'")
        return True
    except Exception:
        logger.exception(f"[account_collector] Failed to clear history for '{label}'")
        return False
