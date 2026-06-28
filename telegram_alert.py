"""
Telegram Alert — minimal, dependency-free Telegram Bot API notifier.
=======================================================================
Uses only the standard library (urllib) so no extra `pip install` is needed
beyond what the EA already requires.

Setup (do this yourself on your machine — never share your bot token/chat id
with anyone, including in screenshots):
  1. Message @BotFather on Telegram, run /newbot, copy the bot token it gives you.
  2. Message your new bot once (anything), then open in a browser:
     https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
     and find your numeric "chat":{"id": ...} — that's your chat id.
  3. Open strategy_config_ui.py -> "Telegram" tab -> paste the token and chat
     id there, tick "Enabled", and Save Config. (Or edit strategy_config.json
     directly under the "telegram" section.)

This module degrades silently: if telegram_enabled is False, or the token/
chat id are blank, or the network call fails, send_message() logs a single
debug/warning line and returns False — it never raises, so a Telegram outage
can't take down the trading loop.
"""

import json
import urllib.request
import urllib.error
import urllib.parse
import logging
import symbol_normalize

logger = logging.getLogger("xauusd_ea")

API_URL_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 8


def send_message(bot_token, chat_id, text, enabled=True):
    """Best-effort send. Returns True on success, False otherwise (and never
    raises) so callers can fire-and-forget this from inside the trading
    loop."""
    if not enabled:
        return False
    if not bot_token or not chat_id:
        logger.debug("Telegram alert skipped — bot_token/chat_id not configured yet.")
        return False

    url = API_URL_TEMPLATE.format(token=bot_token)
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning(f"Telegram alert failed to send (will keep trading normally): {exc}")
        return False


def format_order_alert(signal, lot, scores_summary, account_info=None, league_note=None, symbol=None):
    """Builds a readable HTML message for a just-placed order, including the
    contributing strategies/scores and a couple of running stats."""
    direction = signal.get("direction", "?").upper()
    strategy = signal.get("strategy", "confluence13")
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    lines = [
        f"<b>{lbl} EA — New {direction} order</b>",
        f"Strategy: {strategy}",
        f"Entry: {signal.get('entry'):.2f} | SL: {signal.get('sl'):.2f} | TP2: {signal.get('tp2'):.2f}",
        f"Lot: {lot}",
    ]
    if scores_summary:
        lines.append(f"Contributing strategies: {scores_summary}")
    if account_info is not None:
        lines.append(f"Balance: {account_info.balance:.2f} | Equity: {account_info.equity:.2f}")
    if league_note:
        lines.append(league_note)
    return "\n".join(lines)


def format_close_alert(deal, account_info=None, symbol=None):
    """Builds the position-closed message. `deal` accepts the legacy
    minimal {"pnl": ...} shape plus optional richer fields (direction,
    strategy, entry_price, close_price, lot, duration) when available."""
    pnl = deal.get("pnl")
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    lines = [f"<b>🔴 {lbl} EA — Position Closed</b>"]
    if deal.get("direction"):
        lines.append(f"Direction: {str(deal['direction']).upper()}")
    if deal.get("strategy"):
        lines.append(f"Strategy: {deal['strategy']}")
    if deal.get("entry_price") is not None and deal.get("close_price") is not None:
        lines.append(f"Entry: {deal['entry_price']:.2f} -> Close: {deal['close_price']:.2f}")
    elif deal.get("close_price") is not None:
        lines.append(f"Close price: {deal['close_price']:.2f}")
    if deal.get("lot") is not None:
        lines.append(f"Lot: {deal['lot']}")
    lines.append(f"P&L: {pnl:+.2f}" if pnl is not None else "P&L: n/a")
    if deal.get("duration"):
        lines.append(f"Duration: {deal['duration']}")
    if account_info is not None:
        lines.append(f"Balance now: {account_info.balance:.2f}")
    return "\n".join(lines)


def format_startup_alert(config_summary, account_info=None, symbol=None):
    """Builds the bot-startup message: start time + a flattened summary of
    the active strategy_config.json settings, built by the caller
    (xauusd_mt5_strategy.py's send_startup_notification())."""
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    lines = [f"<b>🟢 {lbl} EA — Bot Started</b>"]
    start_time = config_summary.get("start_time")
    if start_time:
        lines.append(f"Start time: {start_time}")
    for k, v in config_summary.items():
        if k == "start_time":
            continue
        lines.append(f"{k}: {v}")
    if account_info is not None:
        lines.append(f"Balance: {account_info.balance:.2f} {account_info.currency} | "
                      f"Equity: {account_info.equity:.2f}")
    return "\n".join(lines)


def format_signal_alert(signal, scores_summary=None, symbol=None):
    """Builds a 'signal found' message — sent the moment a setup qualifies
    (passes confluence/R:R gates), BEFORE any order is actually sent. This is
    distinct from format_order_alert(), which only fires once an order is
    confirmed placed."""
    direction = signal.get("direction", "?").upper()
    strategy = signal.get("strategy", "confluence13")
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    lines = [
        f"<b>🔎 {lbl} EA — Signal Found ({direction})</b>",
        f"Strategy: {strategy}",
        f"Entry: {signal.get('entry'):.2f} | SL: {signal.get('sl'):.2f} | TP2: {signal.get('tp2'):.2f}",
    ]
    if signal.get("combined_score") is not None:
        lines.append(f"Combined score: {signal['combined_score']:.1f}% "
                      f"({signal.get('agreeing', '?')} strategies agreeing)")
    if scores_summary:
        lines.append(f"Contributing: {scores_summary}")
    lines.append("(Signal only — order not yet confirmed/sent)")
    return "\n".join(lines)


def format_macro_update_alert(bias_result, macro_raw=None, symbol=None):
    """Builds the periodic Big Data / Macro update message — the weighted
    Gold Decision Matrix note from strategies.score_macro_bias() plus the
    raw figures behind it (forecast/changes), so the Bulls/Bears call is
    backed by visible numbers, not just a label."""
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    lines = [f"<b>📊 {lbl} EA — Big Data / Macro Update</b>",
              bias_result.get("note", "n/a")]
    if macro_raw:
        dxy = macro_raw.get("dxy")
        if dxy and dxy.get("latest") is not None:
            lines.append(f"DXY: {dxy.get('latest'):.3f} (chg {dxy.get('change'):+.3f})")
        yld = macro_raw.get("yield10y")
        if yld and yld.get("latest") is not None:
            lines.append(f"US10Y Yield: {yld.get('latest'):.3f}% (chg {yld.get('change'):+.3f})")
        fed = macro_raw.get("fed_expectation")
        if fed and fed.get("latest") is not None:
            lines.append(f"Fed Expectation (2Y proxy): {fed.get('latest'):.3f}% (chg {fed.get('change'):+.3f})")
        cot = macro_raw.get("cot")
        if cot and cot.get("managed_money_net_long_change") is not None:
            lines.append(f"COT Managed Money Net Long chg: {cot.get('managed_money_net_long_change'):+.0f}")
        etf = macro_raw.get("etf_flow")
        if etf and etf.get("change_tonnes") is not None:
            lines.append(f"ETF Flow (GLD) chg: {etf.get('change_tonnes'):+.2f} tonnes")
        comex = macro_raw.get("comex")
        if comex and comex.get("registered_oz") is not None:
            lines.append(f"COMEX Registered: {comex.get('registered_oz'):,.0f} oz")
    return "\n".join(lines)


def format_pre_news_alert(event):
    """Builds the 'news coming up' message, sent ~1 hour before a scheduled
    High-impact USD release (see macro_data.upcoming_high_impact_events())."""
    lines = [
        "<b>⏰ Upcoming US Economic News (~1 hour)</b>",
        f"{event.get('title')} ({event.get('country')})",
        f"Time: {event.get('date')}",
        f"Forecast: {event.get('forecast') or 'n/a'} | Previous: {event.get('previous') or 'n/a'}",
        "Caution: Gold can whip around this release — consider standing aside "
        "or tightening risk until it settles.",
    ]
    return "\n".join(lines)


def format_post_news_alert(event, impact_note=None):
    """Builds the 'news just released' message — actual vs. forecast plus a
    best-effort Bulls/Bears note built by the caller
    (xauusd_mt5_strategy.py's _build_news_impact_note())."""
    lines = [
        "<b>📰 US Economic News Released</b>",
        f"{event.get('title')} ({event.get('country')})",
        f"Actual: {event.get('actual') or 'n/a'} | Forecast: {event.get('forecast') or 'n/a'} | "
        f"Previous: {event.get('previous') or 'n/a'}",
    ]
    if impact_note:
        lines.append(impact_note)
    return "\n".join(lines)


def format_proxy_staleness_alert(data_key, hours_on_proxy, proxy_source_name, symbol=None):
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    return (f"⚠️ <b>{lbl} — Macro data on proxy for {hours_on_proxy:.0f}h</b>\n"
            f"<b>{data_key}</b> has been using fallback source "
            f"<code>{proxy_source_name}</code> instead of its primary feed.\n"
            f"macro_bias (weight 1.2, highest-weighted strategy) is scoring "
            f"off degraded data. Primary source may be blocked (403/bot-wall).\n"
            f"No action needed — bot keeps running. This alert fires once per episode.")


def format_market_closed_alert(symbol, reason):
    lbl = symbol_normalize.display_label(symbol)
    return (f"⏸️ <b>{lbl} — Market Closed</b>\n"
            f"New-entry scanning is paused.\n"
            f"Reason: {reason}\n"
            f"Open positions (if any) keep being managed (trailing/basket-close) as normal.")


def format_market_reopened_alert(symbol, reason):
    lbl = symbol_normalize.display_label(symbol)
    return (f"▶️ <b>{lbl} — Market Reopened</b>\n"
            f"New-entry scanning has resumed.\n"
            f"Status: {reason}")


def format_daily_status_alert(account_info, open_positions_count, today_pnl=None, today_trades=None, symbol=None):
    """Builds the once-daily (default 08:00) bot-status heartbeat message."""
    lbl = symbol_normalize.display_label(symbol or "XAUUSD")
    lines = [f"<b>📋 {lbl} EA — Daily Status</b>"]
    if account_info is not None:
        lines.append(f"Balance: {account_info.balance:.2f} {account_info.currency} | "
                      f"Equity: {account_info.equity:.2f}")
    lines.append(f"Open positions: {open_positions_count}")
    if today_pnl is not None:
        lines.append(f"Today's P&L: {today_pnl:+.2f}")
    if today_trades is not None:
        lines.append(f"Today's trades: {today_trades}")
    return "\n".join(lines)
