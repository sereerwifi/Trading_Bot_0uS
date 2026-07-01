# Prompt for Claude Code: Multi-Account Switcher + Balance History on Dashboard

Paste this into Claude Code running in this folder (`RoBotTrading man 0 US`).

---

Add a **dashboard-only** feature: a way to switch between multiple trader/user
MT5 accounts and view each account's balance/equity history. This is a
sandbox draft (see this folder's CLAUDE.md — stale secondary copy, will be
ported to `USV9` later). Do not touch trading logic, signal detection, order
placement, lot sizing, or risk parameters — the live bot keeps trading and
detecting via MT5 exactly as it does today, on whichever account it's
currently connected to. This feature is a read-only viewer layered on top.

## Requirements

1. **Config** — add an `"accounts"` section to `strategy_config.json`: a list
   of `{label, mt5_login, mt5_password, mt5_server, enabled}`. Treat
   `mt5_password` with the same secrecy rule as the Telegram `bot_token` and
   Myfxbook `password` — never print, log, or paste it anywhere outside this
   VPS.

2. **History collector** — a new script (follow the `macro_data.py` /
   `fib_confluence.py` pattern: dedicated module, best-effort, never raises).
   For each enabled account, connect via the `MetaTrader5` python package and
   pull `mt5.account_info()` (balance, equity, margin, profit) on a schedule.
   **Investigate first and tell me your plan before implementing**: MT5 only
   allows one active login per terminal connection, so pulling multiple
   accounts' data will require either (a) short-lived connect → read →
   disconnect cycles per account that don't disrupt the live bot's own
   connection, or (b) separate MT5 terminal instances per account. Propose
   which approach fits this VPS setup.

3. **Storage** — new SQLite DB, e.g. `account_balance_history.db`, table
   `balance_snapshots(account_label, mt5_login, timestamp, balance, equity,
   margin, profit)`, append-only, deduplicated, matching the existing
   `_save_to_db()` best-effort pattern used elsewhere in this project.

4. **Dashboard** — update `generate_dashboard.py` and `dashboard.html`: add
   an "Accounts" section with a dropdown/tab to switch between the
   configured accounts (client-side, no page reload needed) and, for the
   selected account, a balance/equity line chart over time plus a current
   balance/equity/profit summary. Match the existing dashboard's visual
   style.

5. **Reset button** — add a "Clear History" button at the bottom of the
   selected account's view. Clicking it deletes only that account's rows
   from `balance_snapshots` (never other accounts', never trade/signal
   data elsewhere in the project) and refreshes the chart to empty. Require
   a confirm step (e.g. `confirm()` dialog or a two-click/type-to-confirm
   pattern) before deleting — this is destructive and irreversible. Wire it
   to a small local endpoint/script the dashboard can call, or document the
   manual command if the dashboard is static-file-only.

## Hard rules (from this project's CLAUDE.md — apply here too)

- Never print, log, or transmit any account password or the existing
  Telegram/Myfxbook secrets beyond this VPS.
- Never place a real trade, modify lot sizing, or change risk parameters.
- Additive, config-flag-driven — don't delete or rewire existing behavior.
- This is the stale sandbox copy; flag at the end what would need to be
  mirrored to `USV9` to go live, don't assume it's synced automatically.

## Before you start

Confirm your plan for problem #2 (multi-account MT5 polling) with me before
writing code — that's the one piece with real architectural trade-offs.
