"""
generate_dashboard.py
======================
Generates a self-contained, monitoring-friendly HTML dashboard
(dashboard.html) for the XAUUSD MT5 EA defined in xauusd_mt5_strategy.py.

Run this any time you want a fresh snapshot:

    python generate_dashboard.py

Requirements:
  - MetaTrader 5 terminal must be open and logged in (the EA itself doesn't
    need to be running, but the dashboard pulls LIVE account/position data,
    so MT5 does need to be connected).
  - Run from the same folder as xauusd_mt5_strategy.py and strategy_config.json.

What it shows:
  - Account snapshot: balance, equity, floating P&L, margin level.
  - MM / risk-gate status: drawdown breaker, daily loss limit, consecutive-
    loss breaker, trading-session filter, daily trade count — all using the
    EXACT same functions the EA itself checks before allowing a new entry,
    so the dashboard can never drift out of sync with what the EA is really
    enforcing.
  - Open positions opened by this EA (matched by MAGIC_NUMBER).
  - Today's closed trades (win/loss, P&L) and a simple win-rate stat.
  - Recent log activity (signals, MM sizing, order results, warnings,
    errors) parsed straight from logs/xauusd_ea.log, with the strategy name
    and MM values that were tagged onto each order log line.

The script does not modify the EA or place any trades — it only reads.
"""
import json
import os
import re
import sys
from datetime import datetime, date, timedelta

import xauusd_mt5_strategy as ea
import league
import macro_data

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "dashboard.html")
LOG_TAIL_LINES = 1500          # how many lines of the log file to scan
LOG_DISPLAY_LIMIT = 80         # how many recent entries to show in the table

LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?P<level>\w+)\s*\| (?P<msg>.*)$"
)
ORDER_TAG_RE = re.compile(
    r"\[Strategy=(?P<strategy>[^|]+?) \| Lot=(?P<lot>[^|]+?) \| RiskPerTrade=(?P<risk>[^\]]+?)\]"
)


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------
def safe_call(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def collect_mt5_snapshot():
    """Connects to MT5 (read-only) and returns a dict of everything the
    dashboard needs. Falls back gracefully (mt5_connected=False) if MT5
    isn't reachable, so the dashboard can still render log-only data."""
    snapshot = {
        "mt5_connected": False,
        "connect_error": None,
        "info": None,
        "positions": [],
        "today_deals": [],
        "today_trade_count": 0,
        "consecutive_losses": 0,
        "dd_blocked": False, "dd_reason": None,
        "loss_blocked": False, "loss_reason": None,
        "streak_blocked": False, "streak_reason": None,
        "in_session": None, "matched_session": None,
        "session_labels": [],
        "max_daily_trades": ea.MAX_DAILY_TRADES,
        "max_concurrent_trades": ea.MAX_CONCURRENT_TRADES,
        "symbol": ea.SYMBOL,
    }

    # Silence the EA's logger during dashboard reads — it shares the bot's
    # log file, and load_ui_config/connect would otherwise write "Loaded config"
    # and "Connected" entries that look like bot restarts.
    # We use a filter (not disabled/level) because setup_logging() inside
    # load_ui_config() calls logger.handlers.clear() which would re-enable
    # a level-based suppression; filters survive that clear().
    import logging as _logging

    class _Suppress(_logging.Filter):
        def filter(self, record):
            return False

    _ea_logger = _logging.getLogger("xauusd_ea")
    _suppress = _Suppress()
    _ea_logger.addFilter(_suppress)
    try:
        ea.load_ui_config()
    except Exception:
        pass

    try:
        ea.connect()
        snapshot["mt5_connected"] = True
    except Exception as exc:
        snapshot["connect_error"] = str(exc)
        return snapshot
    finally:
        _ea_logger.removeFilter(_suppress)

    try:
        info = mt5.account_info()
        snapshot["info"] = info

        positions = [p for p in (mt5.positions_get(symbol=ea.SYMBOL) or []) if p.magic == ea.MAGIC_NUMBER]
        snapshot["positions"] = positions

        deals = ea.get_today_closed_deals_ordered()
        snapshot["today_deals"] = deals

        snapshot["today_trade_count"] = safe_call(ea.count_today_new_trades, 0)
        snapshot["consecutive_losses"] = safe_call(ea.count_consecutive_losses, 0)

        if info is not None:
            snapshot["dd_blocked"], snapshot["dd_reason"] = ea.check_drawdown_breaker(info)
            snapshot["loss_blocked"], snapshot["loss_reason"] = ea.check_daily_loss_limit(info.balance)

        snapshot["streak_blocked"], snapshot["streak_reason"] = ea.check_consecutive_loss_breaker()
        snapshot["in_session"], snapshot["matched_session"] = ea.is_within_trading_hours()
        snapshot["session_labels"] = [
            ea.TRADING_SESSIONS[k]["label"] for k in sorted(ea.ALLOWED_SESSIONS) if k in ea.TRADING_SESSIONS
        ]
    finally:
        mt5.shutdown()

    return snapshot


def collect_confluence_snapshot():
    """Reads strategy_scores.json (written by run_confluence_scan() in the
    EA every scan, regardless of whether an order fired) and strategy_league.json
    (League System state). Works even if MT5 isn't connected right now, since
    this is just reading the EA's last snapshot off disk."""
    snap = _load_dashboard_json(ea.SCORES_SNAPSHOT_PATH, None)
    # Prefer the league rows already embedded in the latest scan snapshot —
    # those were computed by the EA with its live LEAGUE_MIN_WINRATE_PCT /
    # lookback settings, so they include the "auto_weight" ML adjustment
    # multiplier. Fall back to a plain (no auto_weight) snapshot only if the
    # EA hasn't written one yet (e.g. fresh install, never run).
    if snap and snap.get("league"):
        league_rows = snap["league"]
    else:
        league_state = _load_dashboard_json(league.STATE_PATH, {})
        league_rows = league.status_snapshot(league_state) if league_state else []
    return snap, league_rows


def _load_dashboard_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _pid_is_alive(pid):
    """Returns True if the given PID is a running process on this machine."""
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        pass
    try:
        import os, signal
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _scores_freshness_seconds():
    """Returns how many seconds ago strategy_scores.json was last written,
    or None if unavailable. The bot writes this file every scan loop tick
    so it's a reliable heartbeat even when the log is quiet."""
    scores_path = getattr(ea, "SCORES_SNAPSHOT_PATH",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "strategy_scores.json"))
    try:
        data = _load_dashboard_json(scores_path, {})
        ts_str = data.get("timestamp")
        if not ts_str:
            return None
        ts = datetime.fromisoformat(ts_str)
        return (datetime.now() - ts).total_seconds()
    except Exception:
        return None


def collect_bot_status(latest_entries):
    """Determines whether the EA process is running using three signals in
    priority order:
      1. PID check — if bot_state.json has a PID that's still alive, RUNNING.
      2. strategy_scores.json freshness — bot writes this every scan tick;
         if it's fresh the bot is alive even when the log is quiet (e.g.
         no signals firing, no open trades to trail).
      3. Log freshness — original fallback; stale log = STALE/STOPPED.
    This prevents false STOPPED reports when the bot is scanning normally
    but has nothing interesting enough to log."""
    status = {"state": "UNKNOWN", "last_log_ts": None, "seconds_since": None,
              "started_at": None, "uptime_seconds": None}

    bot_state = _load_dashboard_json(getattr(ea, "BOT_STATE_PATH", ""), {})
    started_at_str = bot_state.get("started_at")
    pid = bot_state.get("pid")
    if started_at_str:
        status["started_at"] = started_at_str
        try:
            started_at = datetime.fromisoformat(started_at_str)
            status["uptime_seconds"] = (datetime.now() - started_at).total_seconds()
        except ValueError:
            pass

    tick_interval = getattr(ea, "TRAILING_CHECK_SECONDS", 30)
    stale_threshold = max(tick_interval * 4, 120)

    # --- Signal 1: PID alive ---
    if _pid_is_alive(pid):
        status["state"] = "RUNNING"
        # Still populate log-based fields for the dashboard detail line.
        if latest_entries:
            last_ts_str = latest_entries[0]["ts"]
            status["last_log_ts"] = last_ts_str
            try:
                last_ts = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                status["seconds_since"] = (datetime.now() - last_ts).total_seconds()
            except ValueError:
                pass
        return status

    # --- Signal 2: strategy_scores.json freshness ---
    scores_age = _scores_freshness_seconds()
    if scores_age is not None and scores_age <= stale_threshold:
        status["state"] = "RUNNING"
        return status

    # --- Signal 3: log freshness (original logic) ---
    if not latest_entries:
        status["state"] = "STOPPED"
        return status

    last_ts_str = latest_entries[0]["ts"]
    status["last_log_ts"] = last_ts_str
    try:
        last_ts = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return status

    seconds_since = (datetime.now() - last_ts).total_seconds()
    status["seconds_since"] = seconds_since

    if seconds_since <= stale_threshold:
        status["state"] = "RUNNING"
    elif seconds_since <= stale_threshold * 3:
        status["state"] = "STALE"
    else:
        status["state"] = "STOPPED"
    return status


def collect_economic_calendar():
    """Pulls the cached economic calendar (macro_data.py) and returns events
    from the last 24h through the next 7 days, sorted chronologically, so the
    dashboard can show forecast figures ahead of a release and the actual
    figure (with a Bulls/Bears read, reusing the EA's own heuristic) once
    it's out. Never raises — returns [] on any fetch/parse problem."""
    try:
        calendar = macro_data.fetch_economic_calendar()
        if not calendar or not calendar.get("events"):
            return []
        from datetime import timezone, timedelta
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=24)
        window_end = now + timedelta(days=7)
        out = []
        for e in calendar["events"]:
            try:
                dt = datetime.fromisoformat(e["date"])
            except (ValueError, TypeError, KeyError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if not (window_start <= dt <= window_end):
                continue
            impact_note = None
            if e.get("actual") not in (None, ""):
                impact_note = safe_call(lambda ev=e: ea._build_news_impact_note(ev), None)
            out.append({**e, "_dt": dt, "_impact_note": impact_note})
        out.sort(key=lambda x: x["_dt"])
        return out
    except Exception:
        return []


def parse_log_file():
    """Reads the last LOG_TAIL_LINES lines of the EA log and returns
    (entries, order_records, stats). entries is newest-first."""
    log_path = os.path.join(ea.LOG_DIR, ea.LOG_FILE_NAME)
    if not os.path.isfile(log_path):
        return [], [], {"errors": 0, "warnings": 0, "signals": 0, "orders_sent": 0, "orders_failed": 0}

    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-LOG_TAIL_LINES:]

    entries = []
    order_records = []
    stats = {"errors": 0, "warnings": 0, "signals": 0, "orders_sent": 0, "orders_failed": 0}

    for line in lines:
        m = LOG_LINE_RE.match(line.strip())
        if not m:
            continue
        ts, level, msg = m["ts"], m["level"], m["msg"]
        entries.append({"ts": ts, "level": level, "msg": msg})

        if level == "ERROR":
            stats["errors"] += 1
        elif level == "WARNING":
            stats["warnings"] += 1

        if msg.startswith("Signal:"):
            stats["signals"] += 1

        tag_match = ORDER_TAG_RE.search(msg)
        if tag_match and ("order_send result" in msg or "order_send FAILED" in msg):
            failed = "order_send FAILED" in msg
            stats["orders_failed" if failed else "orders_sent"] += 1
            order_records.append({
                "ts": ts,
                "strategy": tag_match["strategy"].strip(),
                "lot": tag_match["lot"].strip(),
                "risk": tag_match["risk"].strip(),
                "failed": failed,
                "raw": msg,
            })

    entries.reverse()
    order_records.reverse()
    return entries[:LOG_DISPLAY_LIMIT], order_records[:LOG_DISPLAY_LIMIT], stats


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def status_pill(ok, ok_text="OK", bad_text="BLOCKED"):
    cls = "pill-ok" if ok else "pill-bad"
    text = ok_text if ok else bad_text
    return f'<span class="pill {cls}">{esc(text)}</span>'


def level_badge(level):
    cls = {"ERROR": "badge-error", "WARNING": "badge-warn", "INFO": "badge-info", "DEBUG": "badge-debug"}.get(level, "badge-info")
    return f'<span class="badge {cls}">{esc(level)}</span>'


def fmt_money(v):
    try:
        return f"{v:,.2f}"
    except Exception:
        return "-"


def fmt_duration(seconds):
    """Renders a seconds count as e.g. '2d 5h 13m' / '5h 13m' / '13m 4s',
    dropping leading zero units. Used for the bot's running-time display."""
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


# --------------------------------------------------------------------------
# HTML build
# --------------------------------------------------------------------------
def build_html(snap, entries, order_records, log_stats, conf_snap, league_rows, bot_status, calendar_events):
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    info = snap["info"]

    # ---- bot running status ----
    state = bot_status["state"]
    state_pill_cls = {"RUNNING": "pill-ok", "STALE": "pill-warn", "STOPPED": "pill-bad", "UNKNOWN": "pill-bad"}.get(state, "pill-bad")
    if bot_status["seconds_since"] is not None:
        mins = bot_status["seconds_since"] / 60.0
        freshness_detail = f"Last log activity {mins:.1f} min ago ({esc(bot_status['last_log_ts'])})"
    else:
        freshness_detail = "ไม่พบ log file หรือยังไม่มีข้อมูล"

    if bot_status.get("started_at") and bot_status.get("uptime_seconds") is not None:
        uptime_str = fmt_duration(bot_status["uptime_seconds"])
        started_str = esc(bot_status["started_at"][:19])
        uptime_detail = f"Started {started_str}"
    else:
        uptime_str = "-"
        uptime_detail = "ยังไม่พบ bot_state.json — รอให้ EA เริ่มรอบแรกหลัง connect สำเร็จ"
    # If the process looks STOPPED/STALE, the recorded started_at is from a
    # run that's no longer alive — say so instead of showing a misleading
    # still-counting uptime.
    if state in ("STOPPED", "UNKNOWN") and uptime_str != "-":
        uptime_detail += " (process ไม่ตอบสนองแล้ว — เวลานี้คือรอบที่แล้ว)"

    bot_status_card = f"""
    <div class="card"><div class="card-label">EA Process</div><div class="card-value"><span class="pill {state_pill_cls}">{esc(state)}</span></div><div class="msg" style="font-size:12px;margin-top:6px">{freshness_detail}</div></div>
    <div class="card"><div class="card-label">Bot Running Time</div><div class="card-value" style="font-size:20px">{esc(uptime_str)}</div><div class="msg" style="font-size:12px;margin-top:6px">{uptime_detail}</div></div>
    <div class="card"><div class="card-label">MT5 Connection</div><div class="card-value"><span class="pill {'pill-ok' if snap['mt5_connected'] else 'pill-bad'}">{'CONNECTED' if snap['mt5_connected'] else 'DISCONNECTED'}</span></div></div>
    <div class="card"><div class="card-label">Entry Mode</div><div class="card-value" style="font-size:17px">{esc(getattr(ea, 'ENTRY_MODE', '-'))}</div></div>
    <div class="card"><div class="card-label">Auto Trade</div><div class="card-value"><span class="pill {'pill-ok' if getattr(ea, 'AUTO_TRADE', False) else 'pill-warn'}">{'ON' if getattr(ea, 'AUTO_TRADE', False) else 'OFF (signal-only)'}</span></div></div>
    """

    # ---- economic calendar / forecast ----
    if calendar_events:
        cal_rows = ""
        for e in calendar_events:
            dt_local = e["_dt"].astimezone()
            time_str = dt_local.strftime("%Y-%m-%d %H:%M")
            impact = e.get("impact") or "-"
            impact_cls = {"High": "badge-error", "Medium": "badge-warn", "Low": "badge-debug"}.get(impact, "badge-info")
            actual = e.get("actual")
            if actual not in (None, ""):
                actual_disp = f"<b>{esc(actual)}</b>"
                note = esc(e.get("_impact_note") or "-")
            else:
                actual_disp = '<span style="color:var(--text-dim)">รอประกาศ</span>'
                note = "-"
            cal_rows += (f"<tr><td class='ts'>{esc(time_str)}</td>"
                         f"<td><span class='badge {impact_cls}'>{esc(impact)}</span></td>"
                         f"<td>{esc(e.get('title', '-'))}</td><td>{esc(e.get('country', '-'))}</td>"
                         f"<td>{esc(e.get('forecast') or '-')}</td><td>{esc(e.get('previous') or '-')}</td>"
                         f"<td>{actual_disp}</td><td class='msg'>{note}</td></tr>")
    else:
        cal_rows = "<tr><td colspan='8' class='empty'>ยังไม่มีข้อมูล economic calendar (macro_data.py) — ตรวจสอบการเชื่อมต่ออินเทอร์เน็ต/ไฟร์วอลล์</td></tr>"

    # ---- confluence multi-strategy (25) scores ----
    if conf_snap:
        direction_taken = conf_snap.get("direction_taken")
        conf_cards = f"""
        <div class="card"><div class="card-label">Long combined score</div><div class="card-value {'pos' if direction_taken == 'long' else ''}">{conf_snap.get('long_combined', 0):.1f}% <span style="font-size:13px;color:var(--text-dim)">({conf_snap.get('long_agreeing', 0)} agreeing)</span></div></div>
        <div class="card"><div class="card-label">Short combined score</div><div class="card-value {'neg' if direction_taken == 'short' else ''}">{conf_snap.get('short_combined', 0):.1f}% <span style="font-size:13px;color:var(--text-dim)">({conf_snap.get('short_agreeing', 0)} agreeing)</span></div></div>
        <div class="card"><div class="card-label">Threshold (gate)</div><div class="card-value">{conf_snap.get('min_strategy_score', 0):.0f}% &middot; {conf_snap.get('min_agreeing_strategies', 0)} agreeing</div></div>
        <div class="card"><div class="card-label">Last scan</div><div class="card-value" style="font-size:15px">{esc(conf_snap.get('timestamp', '-')[:19])}</div></div>
        """
        scores = conf_snap.get("scores", {})
        score_rows = ""
        for key, v in sorted(scores.items(), key=lambda kv: -max(kv[1]["long"], kv[1]["short"])):
            bench_pill = '<span class="pill pill-bad">BENCHED</span>' if v.get("benched") else '<span class="pill pill-ok">ACTIVE</span>'
            score_rows += (f"<tr><td>{esc(v.get('display', key))}</td>"
                           f"<td class='pos'>{v['long']:.0f}%</td><td class='neg'>{v['short']:.0f}%</td>"
                           f"<td>{v.get('weight', 1.0):.1f}</td><td>{bench_pill}</td>"
                           f"<td class='msg'>{esc(v.get('note', ''))}</td></tr>")
        conf_banner = ""
    else:
        conf_cards = '<div class="card card-wide"><div class="card-label">Confluence Scan</div><div class="card-value neg">ยังไม่มีข้อมูล (strategy_scores.json)</div></div>'
        score_rows = "<tr><td colspan='6' class='empty'>ยังไม่มีข้อมูล — ต้องให้ EA รันโหมด confluence13 อย่างน้อย 1 รอบสแกนก่อน</td></tr>"
        conf_banner = '<div class="banner banner-warn">ยังไม่พบ strategy_scores.json — เปิด EA ให้รัน ENTRY_MODE=confluence13 อย่างน้อยหนึ่งรอบ (ทุก 30 วินาที) แล้วรันแดชบอร์ดนี้ใหม่</div>'

    # ---- logic groups (Day Trade / Scalping) — only when ENTRY_MODE=logic_groups ----
    logic_groups = conf_snap.get("logic_groups") if conf_snap else None
    if logic_groups:
        lg_cards = ""
        for g in logic_groups:
            bias = g.get("bias", "neutral")
            bias_cls = "pos" if bias == "long" else ("neg" if bias == "short" else "")
            bias_pill_cls = "pill-ok" if bias != "neutral" else "pill-bad"
            cand = g.get("candidate")
            cand_disp = f"{esc(cand)} ({g.get('candidate_score', 0):.0f}%)" if cand else '<span style="color:var(--text-dim)">no candidate fired</span>'
            df_note = ("Daily Filter (D1) also applied on top" if g.get("apply_daily_filter")
                        else "Daily Filter (D1) bypassed for this group — see LOGIC_GROUPS_APPLY_DAILY_FILTER")
            lg_cards += f"""
            <div class="card">
              <div class="card-label">{esc(g.get('group', '-'))} &mdash; bias</div>
              <div class="card-value {bias_cls}"><span class="pill {bias_pill_cls}">{esc(bias.upper())}</span></div>
              <div class="msg" style="font-size:12px;margin-top:6px">Would trade: {cand_disp}</div>
              <div class="msg" style="font-size:11px;color:var(--text-dim)">{esc(df_note)}</div>
            </div>"""
        lg_section = f"""
<section>
  <h2>Logic Groups (Day Trade / Scalping) &mdash; Live Status</h2>
  <div class="grid-cards">{lg_cards}</div>
  <div class="msg" style="margin-top:8px;font-size:12.5px;color:var(--text-dim)">Bias มาจาก Step-1 trend-filter cascade ของแต่ละกลุ่ม (ไม่ใช่ confluence score ด้านบน) &middot; &ldquo;Would trade&rdquo; คือกลยุทธ์ที่ League System จะเลือกถ้ากลุ่มนี้ยิงสัญญาณ &middot; ถ้าทั้งสองกลุ่มยิงพร้อมกัน บอทจะเลือกกลุ่มที่ League standing ดีกว่า</div>
</section>"""
    else:
        lg_section = ""

    # ---- league system ----
    if league_rows:
        league_table_rows = ""
        for r in sorted(league_rows, key=lambda x: (not x["benched"], x["key"])):
            wr = f"{r['winrate']:.0f}%" if r["winrate"] is not None else "-"
            bench_pill = '<span class="pill pill-bad">BENCHED</span>' if r["benched"] else '<span class="pill pill-ok">ACTIVE</span>'
            until = esc(r["benched_until"][:19]) if r.get("benched_until") and r["benched"] else "-"
            aw = r.get("auto_weight")
            aw_cell = f"{aw:.0%}" if aw is not None else "-"
            aw_class = "neg" if (aw is not None and aw < 1.0) else "pos"
            league_table_rows += (f"<tr><td>{esc(r['key'])}</td><td>{r['trades']}</td><td>{wr}</td>"
                                  f"<td>{r['consecutive_losses']}</td><td>{bench_pill}</td>"
                                  f"<td class='{aw_class}'>{aw_cell}</td>"
                                  f"<td>{until}</td><td class='msg'>{esc(r.get('bench_reason') or '-')}</td></tr>")
    else:
        league_table_rows = "<tr><td colspan='8' class='empty'>ยังไม่มีข้อมูล League System (ยังไม่มี trade ที่ปิดด้วยโหมด confluence13)</td></tr>"

    # ---- macro bias (Big Data) — weighted Gold Decision Matrix ----
    macro = conf_snap.get("macro") if conf_snap else None
    if macro:
        def chk(label, weight, ok, detail):
            wtag = f' <span style="color:var(--text-dim);font-weight:400">({weight}%)</span>'
            if ok is None:
                return f'<div class="card"><div class="card-label">{esc(label)}{wtag}</div><div class="card-value" style="font-size:14px;color:var(--text-dim)">N/A</div><div class="msg" style="font-size:12px">{esc(detail)}</div></div>'
            pill = '<span class="pill pill-ok">BULLISH</span>' if ok else '<span class="pill pill-bad">BEARISH</span>'
            return f'<div class="card"><div class="card-label">{esc(label)}{wtag}</div><div class="card-value" style="font-size:15px">{pill}</div><div class="msg" style="font-size:12px">{esc(detail)}</div></div>'

        dxy = macro.get("dxy")
        dxy_ok = (dxy["change"] < 0) if dxy and dxy.get("change") is not None else None
        dxy_detail = f"chg {dxy['change']:+.3f} ({dxy.get('source', '-')})" if dxy and dxy.get("change") is not None else "ไม่มีข้อมูล"

        yld = macro.get("yield10y")
        yld_ok = (yld["change"] < 0) if yld and yld.get("change") is not None else None
        yld_detail = f"US10Y {yld['latest']:.2f}% chg {yld['change']:+.3f}" if yld and yld.get("change") is not None else "ไม่มีข้อมูล"

        fed = macro.get("fed_expectation")
        fed_ok = (fed["change"] < 0) if fed and fed.get("change") is not None else None
        fed_detail = (f"US2Y (proxy) {fed['latest']:.2f}% chg {fed['change']:+.3f} — ไม่ใช่ CME FedWatch จริง"
                       if fed and fed.get("change") is not None else "ไม่มีข้อมูล")

        etf = macro.get("etf_flow")
        etf_ok = (etf["change_tonnes"] > 0) if etf and etf.get("change_tonnes") is not None else None
        etf_detail = f"chg {etf['change_tonnes']:+.2f}t" if etf and etf.get("change_tonnes") is not None else "ไม่มีข้อมูล (เว็บมีระบบกันบอท)"

        cot = macro.get("cot")
        cot_ok = (cot["managed_money_net_long_change"] > 0) if cot and cot.get("managed_money_net_long_change") is not None else None
        cot_detail = (f"Net Long {cot['managed_money_net_long']:,.0f} chg {cot['managed_money_net_long_change']:+,.0f} ({cot.get('report_date', '-')})"
                       if cot and cot.get("managed_money_net_long_change") is not None else "ไม่มีข้อมูล")

        comex = macro.get("comex")
        comex_ok = None
        comex_detail = "ไม่มีข้อมูล"
        if comex and comex.get("registered_oz") is not None and comex.get("eligible_oz"):
            ratio = comex["registered_oz"] / max(comex["eligible_oz"], 1.0)
            comex_ok = ratio < 0.5
            comex_detail = f"Registered {comex['registered_oz']:,.0f} oz / Eligible {comex['eligible_oz']:,.0f} oz (ratio {ratio:.2f})"

        macro_cards = (
            chk("DXY", 30, dxy_ok, dxy_detail)
            + chk("US 10Y Yield", 25, yld_ok, yld_detail)
            + chk("Fed Expectation", 20, fed_ok, fed_detail)
            + chk("ETF Flow (GLD)", 10, etf_ok, etf_detail)
            + chk("COT Net Long", 10, cot_ok, cot_detail)
            + chk("COMEX Registered", 5, comex_ok, comex_detail)
        )

        macro_strategy = (conf_snap.get("scores") or {}).get("macro_bias")
        bull_score_line = ""
        if macro_strategy:
            bull_score_line = (f'<div class="msg" style="margin-top:4px;font-size:13px">'
                                f'<b>{esc(macro_strategy.get("note", "-"))}</b></div>')

        macro_note = (bull_score_line +
                      '<div class="msg" style="margin-top:8px;font-size:12.5px;color:var(--text-dim)">'
                      'ข้อมูลนี้อัปเดตไม่บ่อยเท่ากลยุทธ์ราคา (COT รายสัปดาห์ / ที่เหลือทุก 3-6 ชม.) — ดู macro_data.py. '
                      'ETF Flow มักไม่มีข้อมูลเพราะเว็บต้นทาง (SPDR/iShares) มีระบบกันบอท ถือเป็นข้อจำกัดที่ทราบอยู่แล้ว ไม่ใช่ bug. '
                      'Fed Expectation ใช้ US 2Y Yield เป็นตัวแทน ไม่ใช่ CME FedWatch จริง (ไม่มี API ฟรี).</div>')
    else:
        macro_cards = '<div class="card card-wide"><div class="card-label">Macro Bias (Big Data)</div><div class="card-value neg">ยังไม่มีข้อมูล</div></div>'
        macro_note = '<div class="msg" style="margin-top:8px;font-size:12.5px;color:var(--text-dim)">รอ EA ดึงข้อมูลรอบแรก (macro_data.py) แล้วรันแดชบอร์ดนี้ใหม่</div>'

    # ---- account cards ----
    if snap["mt5_connected"] and info is not None:
        floating_pnl = sum(p.profit for p in snap["positions"])
        margin_level = f"{info.margin_level:,.0f}%" if getattr(info, "margin_level", 0) else "-"
        account_cards = f"""
        <div class="card"><div class="card-label">Balance</div><div class="card-value">{fmt_money(info.balance)} {esc(info.currency)}</div></div>
        <div class="card"><div class="card-label">Equity</div><div class="card-value">{fmt_money(info.equity)} {esc(info.currency)}</div></div>
        <div class="card"><div class="card-label">Floating P&amp;L</div><div class="card-value {'pos' if floating_pnl >= 0 else 'neg'}">{fmt_money(floating_pnl)}</div></div>
        <div class="card"><div class="card-label">Margin Level</div><div class="card-value">{margin_level}</div></div>
        """
        conn_banner = ""
    else:
        account_cards = '<div class="card card-wide"><div class="card-label">MT5 Connection</div><div class="card-value neg">Not connected</div></div>'
        conn_banner = (f'<div class="banner banner-warn">ไม่สามารถเชื่อมต่อ MT5 ได้'
                        f'{": " + esc(snap["connect_error"]) if snap["connect_error"] else ""} '
                        f'— แดชบอร์ดนี้แสดงได้แค่ข้อมูลจาก log file (เปิด MT5 terminal แล้วรันสคริปต์นี้ใหม่เพื่อดูข้อมูล live)</div>')

    # ---- risk / MM gate status ----
    session_label = ", ".join(snap["session_labels"]) if snap["session_labels"] else "ไม่ได้เลือก session"
    risk_rows = f"""
    <tr><td>Drawdown breaker</td><td>{status_pill(not snap['dd_blocked'])}</td><td>{esc(snap['dd_reason'] or '-')}</td></tr>
    <tr><td>Daily loss limit</td><td>{status_pill(not snap['loss_blocked'])}</td><td>{esc(snap['loss_reason'] or '-')}</td></tr>
    <tr><td>Anti-Martingale (consecutive losses)</td><td>{status_pill(not snap['streak_blocked'])}</td><td>{snap['consecutive_losses']} loss streak today{(' — ' + esc(snap['streak_reason'])) if snap['streak_reason'] else ''}</td></tr>
    <tr><td>Trading session filter</td><td>{status_pill(bool(snap['in_session']), 'IN SESSION', 'OUTSIDE SESSION')}</td><td>Allowed: {esc(session_label)}</td></tr>
    <tr><td>Daily trade count</td><td>{status_pill(snap['max_daily_trades'] is None or snap['today_trade_count'] < snap['max_daily_trades'])}</td><td>{snap['today_trade_count']} / {snap['max_daily_trades'] if snap['max_daily_trades'] is not None else '∞'} trades today</td></tr>
    <tr><td>Concurrent positions</td><td>{status_pill(len(snap['positions']) < snap['max_concurrent_trades'])}</td><td>{len(snap['positions'])} / {snap['max_concurrent_trades']} open</td></tr>
    """

    # ---- open positions ----
    if snap["positions"]:
        pos_rows = ""
        for p in snap["positions"]:
            direction = "LONG" if p.type == 0 else "SHORT"
            pnl_cls = "pos" if p.profit >= 0 else "neg"
            pos_rows += (f"<tr><td>{p.ticket}</td><td>{esc(direction)}</td><td>{p.volume}</td>"
                         f"<td>{p.price_open:.2f}</td><td>{p.sl:.2f}</td><td>{p.tp:.2f}</td>"
                         f"<td class='{pnl_cls}'>{fmt_money(p.profit)}</td></tr>")
    else:
        pos_rows = "<tr><td colspan='7' class='empty'>ไม่มี position เปิดอยู่</td></tr>"

    # ---- today's closed trades ----
    if snap["today_deals"]:
        wins = sum(1 for d in snap["today_deals"] if (d.profit + d.swap + d.commission) > 0)
        total_closed = len(snap["today_deals"])
        win_rate = (wins / total_closed * 100.0) if total_closed else 0.0
        deal_rows = ""
        for d in snap["today_deals"][-30:][::-1]:
            pnl = d.profit + d.swap + d.commission
            pnl_cls = "pos" if pnl >= 0 else "neg"
            t = datetime.fromtimestamp(d.time).strftime("%H:%M:%S")
            deal_rows += f"<tr><td>{t}</td><td>{d.volume}</td><td class='{pnl_cls}'>{fmt_money(pnl)}</td></tr>"
        trades_summary = f"{total_closed} trades closed today &middot; win rate {win_rate:.0f}%"
    else:
        deal_rows = "<tr><td colspan='3' class='empty'>ยังไม่มี trade ปิดวันนี้</td></tr>"
        trades_summary = "ยังไม่มี trade ปิดวันนี้"

    # ---- manual trades (user-placed, magic != EA's magic) ----
    manual_state = _load_dashboard_json(ea.MANUAL_TRADES_PATH, {"open": {}, "closed": []})
    manual_open   = list(manual_state.get("open", {}).values())
    manual_closed = manual_state.get("closed", [])
    manual_float  = sum(t.get("floating_pnl", 0.0) for t in manual_open)
    manual_real   = sum(t.get("profit", 0.0) for t in manual_closed)
    manual_wins   = sum(1 for t in manual_closed if t.get("profit", 0.0) > 0)
    manual_losses = sum(1 for t in manual_closed if t.get("profit", 0.0) <= 0)

    if manual_open:
        manual_open_rows = ""
        for t in manual_open:
            pnl = t.get("floating_pnl", 0.0)
            cls = "pos" if pnl >= 0 else "neg"
            manual_open_rows += (
                f"<tr><td>{t['ticket']}</td><td>{esc(t['type'])}</td><td>{t['volume']}</td>"
                f"<td>{t['entry']:.2f}</td><td>{t.get('sl', 0):.2f}</td><td>{t.get('tp', 0):.2f}</td>"
                f"<td class='{cls}'>{fmt_money(pnl)}</td></tr>"
            )
    else:
        manual_open_rows = "<tr><td colspan='7' class='empty'>No open manual positions</td></tr>"

    if manual_closed:
        manual_closed_rows = ""
        for t in list(reversed(manual_closed))[:20]:
            pnl = t.get("profit", 0.0)
            cls = "pos" if pnl >= 0 else "neg"
            ts  = t.get("closed_at", "")[:19].replace("T", " ")
            manual_closed_rows += (
                f"<tr><td>{ts}</td><td>{esc(t['type'])}</td><td>{t['volume']}</td>"
                f"<td>{t['entry']:.2f}</td><td>{t.get('close_price', 0):.2f}</td>"
                f"<td class='{cls}'>{fmt_money(pnl)}</td></tr>"
            )
    else:
        manual_closed_rows = "<tr><td colspan='6' class='empty'>No closed manual trades yet</td></tr>"

    # ---- recent order records (strategy + MM tag) ----
    if order_records:
        order_rows = ""
        for o in order_records:
            status_cls = "badge-error" if o["failed"] else "badge-ok"
            status_text = "FAILED" if o["failed"] else "SENT"
            order_rows += (f"<tr><td>{esc(o['ts'])}</td>"
                            f"<td><span class='badge badge-strategy'>{esc(o['strategy'])}</span></td>"
                            f"<td>{esc(o['lot'])}</td><td>{esc(o['risk'])}</td>"
                            f"<td><span class='badge {status_cls}'>{status_text}</span></td></tr>")
    else:
        order_rows = "<tr><td colspan='5' class='empty'>ยังไม่มีออเดอร์ใน log</td></tr>"

    # ---- log stats cards ----
    log_cards = f"""
    <div class="card"><div class="card-label">Signals (in log window)</div><div class="card-value">{log_stats['signals']}</div></div>
    <div class="card"><div class="card-label">Orders sent</div><div class="card-value pos">{log_stats['orders_sent']}</div></div>
    <div class="card"><div class="card-label">Orders failed</div><div class="card-value {'neg' if log_stats['orders_failed'] else ''}">{log_stats['orders_failed']}</div></div>
    <div class="card"><div class="card-label">Warnings / Errors</div><div class="card-value {'neg' if log_stats['errors'] else ''}">{log_stats['warnings']} / {log_stats['errors']}</div></div>
    """

    # ---- recent log entries table ----
    if entries:
        log_rows = ""
        for e in entries:
            log_rows += f"<tr><td class='ts'>{esc(e['ts'])}</td><td>{level_badge(e['level'])}</td><td class='msg'>{esc(e['msg'])}</td></tr>"
    else:
        log_rows = "<tr><td colspan='3' class='empty'>ไม่พบไฟล์ log หรือยังไม่มีข้อมูล (logs/xauusd_ea.log)</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XAUUSD EA Dashboard</title>
<style>
  :root {{
    --bg: #0f1420;
    --panel: #161d2e;
    --panel-border: #232c42;
    --text: #e7ecf7;
    --text-dim: #8b96b3;
    --accent: #4f8cff;
    --good: #34d399;
    --bad: #f87171;
    --warn: #fbbf24;
    --radius: 12px;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, "Segoe UI", Roboto, "Noto Sans Thai", sans-serif;
    background: linear-gradient(160deg, #0b0f1a, #11172a 60%, #0f1420);
    color: var(--text);
    padding: 28px 32px 60px;
  }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 22px;
  }}
  header h1 {{ font-size: 22px; margin: 0; font-weight: 650; letter-spacing: .2px; }}
  header .sub {{ color: var(--text-dim); font-size: 13px; }}
  .symbol-tag {{
    background: rgba(79,140,255,.15); color: var(--accent);
    border: 1px solid rgba(79,140,255,.35);
    padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
  }}
  .banner {{ padding: 12px 16px; border-radius: var(--radius); margin-bottom: 18px; font-size: 13px; }}
  .banner-warn {{ background: rgba(251,191,36,.12); border: 1px solid rgba(251,191,36,.35); color: #fde68a; }}
  section {{ margin-bottom: 28px; }}
  section h2 {{
    font-size: 14px; text-transform: uppercase; letter-spacing: .08em;
    color: var(--text-dim); margin: 0 0 12px; font-weight: 650;
  }}
  .grid-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; }}
  .card {{
    background: var(--panel); border: 1px solid var(--panel-border);
    border-radius: var(--radius); padding: 16px 18px;
  }}
  .card-wide {{ grid-column: 1 / -1; }}
  .card-label {{ font-size: 12px; color: var(--text-dim); margin-bottom: 6px; }}
  .card-value {{ font-size: 22px; font-weight: 650; }}
  .pos {{ color: var(--good); }}
  .neg {{ color: var(--bad); }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel); border-radius: var(--radius); overflow: hidden; }}
  thead th {{
    text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
    color: var(--text-dim); padding: 10px 14px; border-bottom: 1px solid var(--panel-border);
  }}
  tbody td {{ padding: 10px 14px; font-size: 13px; border-bottom: 1px solid var(--panel-border); }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: rgba(255,255,255,.02); }}
  .empty {{ text-align: center; color: var(--text-dim); padding: 18px !important; }}
  .panel-table {{ border: 1px solid var(--panel-border); border-radius: var(--radius); overflow: hidden; }}
  .pill {{ padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 650; letter-spacing: .03em; }}
  .pill-ok {{ background: rgba(52,211,153,.15); color: var(--good); border: 1px solid rgba(52,211,153,.35); }}
  .pill-bad {{ background: rgba(248,113,113,.15); color: var(--bad); border: 1px solid rgba(248,113,113,.35); }}
  .pill-warn {{ background: rgba(251,191,36,.15); color: var(--warn); border: 1px solid rgba(251,191,36,.35); }}
  .badge {{ padding: 2px 9px; border-radius: 6px; font-size: 11px; font-weight: 650; }}
  .badge-info {{ background: rgba(79,140,255,.15); color: var(--accent); }}
  .badge-warn {{ background: rgba(251,191,36,.15); color: var(--warn); }}
  .badge-error {{ background: rgba(248,113,113,.15); color: var(--bad); }}
  .badge-debug {{ background: rgba(139,150,179,.15); color: var(--text-dim); }}
  .badge-ok {{ background: rgba(52,211,153,.15); color: var(--good); }}
  .badge-strategy {{ background: rgba(167,139,250,.18); color: #c4b5fd; }}
  .msg {{ color: #c9d2eb; }}
  .ts {{ color: var(--text-dim); white-space: nowrap; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; align-items: start; }}
  @media (max-width: 880px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
  .table-scroll {{ max-height: 480px; overflow-y: auto; }}
  footer {{ color: var(--text-dim); font-size: 12px; margin-top: 36px; text-align: center; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>XAUUSD MT5 EA — Monitoring Dashboard <span class="symbol-tag">{esc(snap['symbol'])}</span></h1>
    <div class="sub">Generated {generated_at} &middot; ข้อมูล live จาก MT5 ขณะรันสคริปต์นี้ — รันใหม่เพื่ออัปเดต</div>
  </div>
</header>

{conn_banner}
{conf_banner}

<section>
  <h2>Bot Running Status</h2>
  <div class="grid-cards">{bot_status_card}</div>
</section>

<section>
  <h2>Account Snapshot</h2>
  <div class="grid-cards">{account_cards}</div>
</section>

<section>
  <h2>Multi-Strategy Confluence (25) — Live Scores</h2>
  <div class="grid-cards">{conf_cards}</div>
  <div class="panel-table" style="margin-top:14px">
    <table>
      <thead><tr><th>Strategy</th><th>Long%</th><th>Short%</th><th>Weight</th><th>Status</th><th>Note</th></tr></thead>
      <tbody>{score_rows}</tbody>
    </table>
  </div>
</section>
{lg_section}
<section>
  <h2>Macro Bias (Big Data) — Gold Decision Matrix (น้ำหนักถ่วง 6 ปัจจัย)</h2>
  <div class="grid-cards">{macro_cards}</div>
  {macro_note}
</section>

<section>
  <h2>Economic Calendar — Forecast / Actual / Impact (24h ago &ndash; 7 days ahead)</h2>
  <div class="panel-table table-scroll">
    <table>
      <thead><tr><th>Time (local)</th><th>Impact</th><th>Event</th><th>Country</th><th>Forecast</th><th>Previous</th><th>Actual</th><th>Bulls/Bears note</th></tr></thead>
      <tbody>{cal_rows}</tbody>
    </table>
  </div>
  <div class="msg" style="margin-top:8px;font-size:12.5px;color:var(--text-dim)">ที่มา: ForexFactory public feed (macro_data.py) &middot; Bulls/Bears note เป็น heuristic เทียบ actual vs forecast แบบทั่วไป ไม่ใช่การวิเคราะห์เฉพาะตัวชี้วัด ใช้ร่วมกับ price action เสมอ</div>
</section>

<section>
  <h2>League System — Win/Loss Bench Status</h2>
  <div class="panel-table">
    <table>
      <thead><tr><th>Strategy</th><th>Trades</th><th>Win-rate</th><th>Losses in a row</th><th>Status</th><th>Auto-Weight</th><th>Benched until</th><th>Reason</th></tr></thead>
      <tbody>{league_table_rows}</tbody>
    </table>
  </div>
  <div class="msg" style="margin-top:8px;font-size:12.5px;color:var(--text-dim)">Auto-Weight = ML decision layer: ตัวคูณน้ำหนักแบบต่อเนื่อง (0%-100%) คำนวณจากผลรวมไม้จริง+ไม้จำลอง (shadow simulation) ทุกรอบสแกน — ต่ำกว่า 100% แปลว่า win-rate ของกลยุทธ์นั้นต่ำกว่าเกณฑ์อยู่ และจะกลับมา 100% ทันทีที่ผลงานฟื้นตัว ไม่ต้องรอครบเวลาพัก</div>
</section>

<section>
  <h2>MM / Risk Gate Status</h2>
  <div class="panel-table">
    <table>
      <thead><tr><th>Gate</th><th>Status</th><th>Detail</th></tr></thead>
      <tbody>{risk_rows}</tbody>
    </table>
  </div>
</section>

<div class="two-col">
  <section>
    <h2>Open Positions ({len(snap['positions'])})</h2>
    <div class="panel-table">
      <table>
        <thead><tr><th>Ticket</th><th>Dir</th><th>Lot</th><th>Open</th><th>SL</th><th>TP</th><th>P&amp;L</th></tr></thead>
        <tbody>{pos_rows}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Today's Closed Trades — {trades_summary}</h2>
    <div class="panel-table table-scroll">
      <table>
        <thead><tr><th>Time</th><th>Lot</th><th>P&amp;L</th></tr></thead>
        <tbody>{deal_rows}</tbody>
      </table>
    </div>
  </section>
</div>

<section>
  <h2>Manual Trades (User-Placed) &mdash; Combined P&amp;L</h2>
  <div class="grid-cards">
    <div class="card"><div class="card-label">Open manual positions</div><div class="card-value">{len(manual_open)}</div></div>
    <div class="card"><div class="card-label">Floating P&amp;L (manual)</div><div class="card-value {'pos' if manual_float >= 0 else 'neg'}">{fmt_money(manual_float)}</div></div>
    <div class="card"><div class="card-label">Closed manual trades</div><div class="card-value">{len(manual_closed)}</div></div>
    <div class="card"><div class="card-label">Realized P&amp;L (manual)</div><div class="card-value {'pos' if manual_real >= 0 else 'neg'}">{fmt_money(manual_real)}</div></div>
    <div class="card"><div class="card-label">Manual W / L</div><div class="card-value">{manual_wins}W / {manual_losses}L</div></div>
  </div>
  <div class="two-col" style="margin-top:12px">
    <div>
      <h3 style="margin:0 0 6px">Open Manual Positions</h3>
      <div class="panel-table"><table>
        <thead><tr><th>Ticket</th><th>Dir</th><th>Lot</th><th>Entry</th><th>SL</th><th>TP</th><th>Float P&amp;L</th></tr></thead>
        <tbody>{manual_open_rows}</tbody>
      </table></div>
    </div>
    <div>
      <h3 style="margin:0 0 6px">Closed Manual Trades</h3>
      <div class="panel-table table-scroll"><table>
        <thead><tr><th>Time</th><th>Dir</th><th>Lot</th><th>Entry</th><th>Close</th><th>P&amp;L</th></tr></thead>
        <tbody>{manual_closed_rows}</tbody>
      </table></div>
    </div>
  </div>
</section>

<section>
  <h2>Log Activity Summary</h2>
  <div class="grid-cards">{log_cards}</div>
</section>

<section>
  <h2>Recent Orders — Strategy &amp; MM Used</h2>
  <div class="panel-table table-scroll">
    <table>
      <thead><tr><th>Time</th><th>Strategy</th><th>Lot</th><th>Risk per trade</th><th>Result</th></tr></thead>
      <tbody>{order_rows}</tbody>
    </table>
  </div>
</section>

<section>
  <h2>Recent Log Entries</h2>
  <div class="panel-table table-scroll">
    <table>
      <thead><tr><th style="width:140px">Time</th><th style="width:90px">Level</th><th>Message</th></tr></thead>
      <tbody>{log_rows}</tbody>
    </table>
  </div>
</section>

<footer>xauusd_mt5_strategy.py &middot; generate_dashboard.py &middot; เปิดไฟล์นี้ใหม่ในเบราว์เซอร์ทุกครั้งหลังรันสคริปต์เพื่อดูข้อมูลล่าสุด</footer>
</body>
</html>
"""
    return html


def main():
    snap = collect_mt5_snapshot()
    entries, order_records, log_stats = parse_log_file()
    conf_snap, league_rows = collect_confluence_snapshot()
    bot_status = collect_bot_status(entries)
    calendar_events = collect_economic_calendar()
    html = build_html(snap, entries, order_records, log_stats, conf_snap, league_rows, bot_status, calendar_events)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Dashboard generated: {OUTPUT_PATH}")
    if not snap["mt5_connected"]:
        print(f"Note: MT5 not connected ({snap['connect_error']}) — dashboard shows log-only data.")


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Generate the EA monitoring dashboard.")
    parser.add_argument("--watch", action="store_true",
                         help="Keep running and regenerate dashboard.html on a timer "
                              "instead of writing it once and exiting. Useful when "
                              "launched alongside the bot so the dashboard always "
                              "reflects the latest state without re-running manually.")
    parser.add_argument("--interval", type=int, default=60,
                         help="Seconds between regenerations in --watch mode (default 60).")
    args = parser.parse_args()

    if args.watch:
        print(f"Dashboard watch mode: regenerating every {args.interval}s. Ctrl+C to stop.")
        while True:
            try:
                main()
            except Exception as exc:
                print(f"Dashboard generation failed this cycle (will retry next interval): {exc}")
            time.sleep(args.interval)
    else:
        main()
