"""
XAUUSD Trend-Pullback Strategy for MetaTrader 5
=================================================
Strategy: EMA trend filter (H4) + Fibonacci retracement zone (H1)
          + RSI(14) timing filter + MACD(12,26,9) trigger.

Requirements:
    pip install MetaTrader5 pandas numpy

IMPORTANT — read before running:
  - This script is a STRATEGY SKELETON / DECISION ENGINE. It will fetch data,
    compute the signal, and print/log it.
  - Order placement (`send_order`) is gated behind AUTO_TRADE=False by default.
    Flip it to True only after you have backtested and forward-tested on a
    DEMO account, and you understand exactly what it does. You are responsible
    for any live orders this script places once AUTO_TRADE=True.
  - MT5 terminal must be open and logged into your broker account on this
    machine for the `MetaTrader5` package to connect.
  - Trailing stop (see TRAILING_* config and manage_trailing_stops()) only
    runs while THIS script's main loop is executing, which only works while
    the MT5 desktop terminal is open and running on this machine. If you
    close the terminal or stop the script, the trailing stop stops updating
    — any SL already written to your position stays in place on the broker
    server, but it will no longer move further as price advances.
  - Trailing supports 4 selectable methods (TRAILING_METHOD): "ATR" (best
    for volatile instruments — distance adapts to current volatility),
    "EMA" (rides a moving average line — best for sustained trends),
    "FIXED_POINTS", and "PERCENT". Only one runs at a time.
  - Optional basket close (BASKET_CLOSE_ENABLED, off by default) closes ALL
    of this EA's open positions together once combined floating P&L crosses
    a $ or % threshold you set.
  - Daily trend filter (DAILY_FILTER_ENABLED, on by default) is a hard veto:
    it reads the Day chart's 10/20/50/100/200 EMA stack + RSI(14) and blocks
    ANY entry whose direction fights the dominant Day trend (or blocks both
    directions if the Day chart is neutral/choppy). This exists specifically
    to stop wrong-side entries relative to the Day timeframe.
  - Money management (MIN_LOT/MAX_LOT/ENFORCE_MIN_LOT/MAX_DAILY_TRADES/
    MAX_DRAWDOWN_PCT/DAILY_LOSS_LIMIT_R/MIN_RISK_REWARD_RATIO/
    MAX_CONSECUTIVE_LOSSES): calc_lot_size() still sizes the trade off
    RISK_PER_TRADE first (Fixed Fractional — % of CLOSED balance, never
    equity, so it can never "double up" to chase a loss), then clamps the
    result between MIN_LOT and MAX_LOT. run_once() additionally refuses ALL
    new entries (existing open positions keep being trailing-managed
    regardless) once: today's realized+floating P&L breaches
    DAILY_LOSS_LIMIT_R, today's new-trade count hits MAX_DAILY_TRADES,
    equity drawdown from this session's peak balance hits MAX_DRAWDOWN_PCT,
    or today's closing trades have hit MAX_CONSECUTIVE_LOSSES losses in a
    row (Anti-Martingale circuit breaker — stop and reassess instead of
    revenge-trading). Separately, check_entry_signal() now rejects any
    setup outright if its TP2:SL reward-to-risk ratio falls below
    MIN_RISK_REWARD_RATIO (the system never proposes a trade worse than the
    configured R:R floor, default 1:1.5).
  - Trading-hours filter (TRADING_HOURS_FILTER_ENABLED/ALLOWED_SESSIONS):
    a hard gate on NEW entries based on the time of day, tuned for gold's
    three classic Thai-time sessions (Asia / London open / London-NY
    overlap — see TRADING_SESSIONS below). Uses this machine's local clock
    (datetime.now()), so the computer/server must be set to Thailand time
    (UTC+7) for the windows below to line up correctly. Selectable from the
    "ช่วงเวลาเทรด" tab in strategy_config_ui.py — pick one or more sessions;
    outside all selected windows, run_once() returns before even computing
    a signal. Existing open positions keep being trailing-managed
    regardless of session.
  - Logging (LOG_DIR/LOG_LEVEL/LOG_TO_CONSOLE/LOG_FILE_MAX_BYTES/
    LOG_BACKUP_COUNT): all status, gate-rejection, order, and error messages
    go through Python's logging module instead of print(). Writes a
    rotating log file under LOG_DIR (default "logs/xauusd_ea.log", auto
    rotates once it hits LOG_FILE_MAX_BYTES, keeping LOG_BACKUP_COUNT old
    files) and, if LOG_TO_CONSOLE is True, also echoes to the console.
    Unexpected exceptions in the main loop are caught and logged with a
    full traceback (logger.exception) instead of crashing silently.
"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
import sys
import time
import json
import os
import logging
import logging.handlers

import strategies
import league
import strategy_simulator
import telegram_alert
import macro_data
import symbol_normalize
import fib_confluence
import harmonic_patterns

# ----------------------------- CONFIG ---------------------------------
SYMBOL = "GOLD"                # broker symbol name for gold spot — XM uses "GOLD",
                                # not "XAUUSD". If your broker uses a different name
                                # (e.g. "XAUUSDm", "GOLD#"), check Market Watch and set
                                # it either here or via the "Symbol" field in the
                                # Risk & Basket Close tab of strategy_config_ui.py.
TF_TREND = mt5.TIMEFRAME_H4
TF_ENTRY = mt5.TIMEFRAME_H1
TF_DAY = mt5.TIMEFRAME_D1

# --- Daily trend filter (hard veto against wrong-side entries) ---
# Uses the 10/20/50/100/200 EMA stack + RSI(14) on the DAY chart. This runs
# independently of the H4/H1 signal logic and acts as a gate: if the Day
# chart's dominant trend disagrees with the H4/H1 signal direction (or is
# neutral/choppy), the trade is blocked outright — this is what stops the
# EA from opening longs into a Day downtrend or shorts into a Day uptrend.
DAILY_FILTER_ENABLED = True
DAILY_EMA_PERIODS = (10, 20, 50, 100, 200)
DAILY_RSI_PERIOD = 14
DAILY_RSI_OVERBOUGHT = 70   # block longs once Day RSI is at/above this
DAILY_RSI_OVERSOLD = 30     # block shorts once Day RSI is at/below this
DAILY_REQUIRE_FULL_STACK = False  # strict mode: require 10>20>50>100>200
                                    # (or fully reversed for short) — off by
                                    # default since gold often pulls back
                                    # through the faster EMAs inside a trend
RISK_PER_TRADE = 0.01          # 1% of account balance
LOT_STEP = 0.01
VALUE_PER_POINT_PER_LOT = 100  # $ per $1 move per 1.0 lot (check your broker's contract spec)
SWING_LOOKBACK = 50            # bars to scan for swing high/low
ATR_PERIOD = 14
ATR_SL_BUFFER_MULT = 0.5
MAX_CONCURRENT_TRADES = 2
DAILY_LOSS_LIMIT_R = 3

# --- Money management ---
# calc_lot_size() still computes a risk-based lot from RISK_PER_TRADE first;
# MIN_LOT/MAX_LOT only clamp the *result* — they don't replace the risk calc.
MIN_LOT = 0.01                 # broker's minimum tradable volume for SYMBOL
MAX_LOT = 5.0                  # hard ceiling regardless of how big the risk-based calc gets
ENFORCE_MIN_LOT = True         # if the risk-based calc rounds below MIN_LOT:
                                #   True  -> open at MIN_LOT anyway (risk % will run a bit
                                #            higher than RISK_PER_TRADE for that trade)
                                #   False -> skip the trade entirely (lot = 0)
MAX_DAILY_TRADES = 5           # max new entries per calendar day; None = unlimited
MIN_TRADE_INTERVAL_MINUTES = 20          # minimum minutes between consecutive new orders (Day Trade / confluence13)
MIN_TRADE_INTERVAL_MINUTES_SCALPING = 5  # separate, shorter cooldown for Scalping Trade group only;
                                          # defaults to 5 min but is independent of the Day Trade cooldown
                                          # so a scalping fill no longer blocks day-trade entries and vice versa.
                                          # Change via risk.min_trade_interval_minutes_scalping in config/UI.

# Per-group last-order time so Day Trade and Scalping Trade cooldowns don't
# cross-block each other. Keys match _group_key() return values below.
_LAST_ORDER_TIME_BY_GROUP: "dict[str, datetime | None]" = {
    "confluence13": None,
    "day_trade":    None,
    "scalping_trade": None,
    "legacy":       None,
}
MAX_DRAWDOWN_PCT = 10.0        # stop opening NEW trades once equity drawdown from the
                                # session's peak balance reaches this %; None = disabled.
                                # Existing open positions keep being trailing-managed.

# --- Fixed Fractional sizing basis (Anti-Martingale by construction) ---
# calc_lot_size() is always called with the account's CLOSED-trade balance
# (mt5.account_info().balance), never equity. Balance only moves when a
# trade actually closes, so lot size can only grow after a realized win and
# can only shrink after a realized loss — it is mathematically impossible
# for this script to raise the lot size to "chase" a loss (the Martingale
# trap). RISK_PER_TRADE is the fixed fraction (1-2% is the standard rule);
# do not raise it above ~0.02 (2%) for an instrument as volatile as XAUUSD.
MIN_RISK_REWARD_RATIO = 1.5     # reject a signal outright if its TP:SL reward
                                 # ratio is below this (1.5 = R:R 1:1.5, the
                                 # floor recommended for gold; use 2.0 for a
                                 # stricter 1:2 rule). Checked against TP2.
MAX_CONSECUTIVE_LOSSES = 3      # Anti-Martingale circuit breaker: stop ALL
                                 # new entries for the rest of the calendar
                                 # day once this many losing trades in a row
                                 # have closed. None = disabled.

# --- Trading hours filter (Thai time, 3 gold sessions) ---
# Times are LOCAL machine time (datetime.now()) — set this PC/server's clock
# to Thailand time (UTC+7) for these windows to match the descriptions below.
TRADING_SESSIONS = {
    "asia": {
        "label": "07:00-12:00 ตลาดเอเชีย (เงียบ/Sideways)",
        "start": (7, 0), "end": (12, 0),
    },
    "london": {
        "label": "14:00-17:00 ลอนดอนเปิด (คึกคัก/Breakout)",
        "start": (14, 0), "end": (17, 0),
    },
    "overlap": {
        "label": "19:00-23:00 London-NY Overlap (Golden Period — วิ่งแรงที่สุด)",
        "start": (19, 0), "end": (23, 0),
    },
    "all_day": {
        "label": "00:00-23:59 All Day (ไม่จำกัดช่วงเวลา — เทรดได้ตลอด 24 ชม.)",
        "start": (0, 0), "end": (23, 59),
    },
}
TRADING_HOURS_FILTER_ENABLED = True
ALLOWED_SESSIONS = {"all_day"}  # default: trade around the clock; switch to
                                 # "overlap" / "london" / "asia" to narrow it

# --- Real market-open/closed detection (broker ground truth) -----------------
# TRADING_HOURS_FILTER_ENABLED / ALLOWED_SESSIONS answer "do I WANT to trade
# right now"; the settings below answer "CAN I trade right now at all" --
# weekends, broker holidays, or a stalled/disconnected price feed. Both gates
# are checked independently; either one can block a new entry.
MARKET_HOURS_CHECK_ENABLED = True
MARKET_CLOSED_MAX_TICK_AGE_SEC = 180   # last tick older than this = treat as stale/closed
MARKET_CLOSED_NOTIFY = True            # one Telegram alert per open<->closed transition
MARKET_PRICE_SANITY_CHECK_ENABLED = False   # off by default (extra HTTP call per tick)
MARKET_PRICE_SANITY_TOLERANCE_PCT = 1.0

AUTO_TRADE = False             # safety gate — see docstring above
POLL_SECONDS = 60 * 15         # check every 15 minutes
MAGIC_NUMBER = 20260618

# --- Trailing stop ---
# Pick ONE method. ATR and EMA are the two methods with real evidence behind
# them for trend/breakout trades; FIXED/PERCENT are simpler fallbacks.
#   "ATR"          -> trail distance = ATR(H1) * TRAILING_ATR_MULT (best for
#                      volatile instruments like gold — tightens automatically
#                      when volatility drops, widens when it spikes)
#   "EMA"          -> SL trails just behind a moving average line; exit when
#                      price closes back through it (best for riding trends)
#   "FIXED_POINTS" -> constant distance in price points, MT5's native-style
#                      fixed trailing stop
#   "PERCENT"      -> distance as a % of current price
TRAILING_ENABLED = True
TRAILING_METHOD = "ATR"

TRAILING_ATR_MULT = 1.5        # 1.5-2.0 for short-term trades, 2.5-3.0 for swing trades
TRAILING_FIXED_POINTS = 5.0    # used only if TRAILING_METHOD == "FIXED_POINTS"
TRAILING_PERCENT = 0.3         # % of price, used only if TRAILING_METHOD == "PERCENT"
TRAILING_EMA_PERIOD = 20       # used only if TRAILING_METHOD == "EMA"
TRAILING_EMA_BUFFER_POINTS = 7.0  # SL sits this far beyond the EMA line

TRAILING_ACTIVATION_R = 1.0    # for ATR/FIXED/PERCENT: only start trailing once
                                # price has moved this many multiples of the
                                # trail distance into profit. EMA method ignores
                                # this and starts as soon as price is in profit.
TRAILING_REMOVE_TP_ON_ACTIVATE = False  # if True, clears the original TP once
                                # trailing kicks in, so the trade is only ever
                                # closed by the trailing SL ("let profits run")
TRAILING_CHECK_SECONDS = 30    # how often to re-check trailing stops (independent
                                # of the slower signal-scan POLL_SECONDS)

# --- Basket close (close ALL of this EA's open positions at once) ---
# Off by default — this closes live positions outright, not just adjusts SL.
BASKET_CLOSE_ENABLED = False
BASKET_TARGET_PROFIT_USD = None   # e.g. 200  -> close all when floating profit >= $200
BASKET_MAX_LOSS_USD = None        # e.g. 100  -> close all when floating loss <= -$100
BASKET_TARGET_PROFIT_PCT = None   # e.g. 2.0  -> close all when profit >= 2% of balance
BASKET_MAX_LOSS_PCT = None        # e.g. 1.5  -> close all when loss >= 1.5% of balance

# Set of entry-strategy keys enabled via strategy_config_ui.py. Only
# "fib_confluence" is currently wired into check_entry_signal(); the rest
# are recorded here for future implementation.
ENABLED_STRATEGIES = {"fib_confluence"}

# --- Multi-Strategy (31) Confluence Engine -----------------------------------
# ENTRY_MODE selects which entry logic the main loop runs:
#   "confluence13" (default, recommended) -> run_confluence_scan(): all 31
#       strategies in strategies.py score the market every SCAN_INTERVAL_SECONDS,
#       and an entry only fires when MULTIPLE strategies agree (confluence) —
#       see MIN_STRATEGY_SCORE / MIN_AGREEING_STRATEGIES below.
#   "legacy" -> the original single-strategy check_entry_signal() (fib
#       retracement + MACD/RSI only), run on the slower POLL_SECONDS cycle.
#   "logic_groups" -> run_logic_groups_scan(): splits the strategy set into
#       two purpose-built groups ("Day Trade" and "Scalping Trade"), each
#       with its OWN priority-cascaded trend filter and its OWN entry pool —
#       see LOGIC_GROUP_SELECTION and the DAY_TRADE_*/SCALP_* lists below.
ENTRY_MODE = "confluence13"
SCAN_INTERVAL_SECONDS = 30      # how often the multi-strategy confluence scan runs

# --- Logic Groups entry engine (alternative to confluence13) ---------------
# Selects which group(s) run when ENTRY_MODE == "logic_groups". Configurable
# in strategy_config_ui.py -> "Logic Groups" tab. One of:
#   "day_trade"  -> only the Day Trade group is evaluated
#   "scalping"   -> only the Scalping Trade group is evaluated
#   "both"       -> both groups are evaluated every scan; if both fire in the
#                   same cycle (possibly opposite directions), the one whose
#                   chosen strategy has the stronger League standing wins.
LOGIC_GROUP_SELECTION = "both"

# Each Logic Group already runs its OWN trend filter (Step 1, the
# DAY_TRADE_BIAS_PRIORITY / SCALP_BIAS_PRIORITY cascade) before any entry is
# allowed — that filter exists for exactly the same reason as the global
# Daily Filter below (DAILY_FILTER_ENABLED, the D1 EMA-stack veto): to block
# trades against the dominant trend. Stacking BOTH filters means a trade
# needs the D1 daily trend AND the group's own H1-based bias to agree and
# both be non-neutral at once — in practice this very rarely happens and
# tends to silently veto almost everything (observed: bot stays at 0 trades
# for hours with "Day trend is neutral/choppy" in the log even when the
# group's own bias was a clean, decisive short). Default OFF so the group's
# own filter is the only trend gate in logic_groups mode; flip to True if
# you want the extra (stricter) D1 confirmation on top.
LOGIC_GROUPS_APPLY_DAILY_FILTER = False

# Group 1: "Day Trade". Step 1 (trend filter) cascades through this priority
# list in order — the first strategy that gives a decisive long/short read
# sets the group's bias; everything below it is only consulted as a
# tie-breaker. If none of them are decisive, the group is "neutral" this
# scan and takes no trade in either direction (mirrors the existing Daily
# Filter's "neutral blocks both sides" design, but using H1 signals instead
# of the D1 EMA stack).
DAY_TRADE_BIAS_PRIORITY = ["multi_tf_align", "macro_bias", "news_fade", "order_flow_dom", "supply_demand"]
# Step 2 (entry pool): once the group has a bias, these are the strategies
# allowed to actually trigger an entry in that direction. If more than one
# fires the same scan, the League System's standing (auto_weight, then
# recent win-rate, then raw score) picks the single one that trades.
DAY_TRADE_STRATEGIES = [
    "bb_breakout", "macd_cross", "opening_range_breakout", "price_action",
    "vwap_rejection", "rsi_divergence", "atr_donchian_breakout", "fair_value_gap",
    "fibonacci", "multi_tf_align", "news_fade",
    "ema_cross", "liquidity_sweep", "bos_choch", "order_block",
    "climax_reversal_sr", "zone_mw_reversal", "mtr_trend_regime",
    # mtr_range_regime excluded: returns symmetric long=short scores so its
    # direction is 100% inherited from the bias filter, not from price-action
    # analysis — caused late entries at price extremes. Still scores every scan
    # and contributes to confluence, but never owns the entry.
]

# Group 2: "Scalping Trade" — same two-step design, smaller/faster bias
# chain and a dedicated scalp-only entry pool.
SCALP_BIAS_PRIORITY = ["macro_bias", "order_flow_dom", "supply_demand", "sr_breakout_retest"]
SCALP_STRATEGIES = [
    "scalp_ema_pullback", "london_breakout", "scalp_combo_sweep",
    "scalp_ny_orb", "scalp_london_sweep",
    "smart_money_sweep_morning", "smart_money_sweep_night",
]
TF_M15 = mt5.TIMEFRAME_M15
TF_M5 = mt5.TIMEFRAME_M5        # used by the 4 scalping strategies (#21-24)
TF_M1 = mt5.TIMEFRAME_M1        # used by the EMA Pullback scalp (#22)

# Minimum COMBINED confluence score (weighted average across all *voting*
# non-benched strategies on one side) required before an entry fires.
# Configurable in strategy_config_ui.py -> "20 กลยุทธ์ (Confluence)" tab.
# Recommended starting point: ~70.
MIN_STRATEGY_SCORE = 70.0
# Minimum number of strategies that must independently vote the same
# direction (score >= strategies.DEFAULT_VOTE_THRESHOLD) before that side
# is even considered — this is what enforces "must be confluence, not a
# single strategy crossing the threshold alone".
MIN_AGREEING_STRATEGIES = 3
# Per-strategy weight in the combined score (1.0 = normal influence). Keys
# match strategies.STRATEGY_REGISTRY. Editable per-strategy in the UI
# (strategy_config_ui.py's DEFAULT_CONFIG mirrors these same numbers so a
# fresh strategy_config.json shows the same starting point in the UI).
# Recommended defaults: 1.2-1.4 for high-conviction structural/SMC concepts
# and the institutional Big Data check, 1.0-1.1 for solid dependable
# classics, 0.7-0.9 for useful-but-lag/false-signal-prone confirmation —
# nothing is disabled, only down-weighted, so all 31 strategies still vote.
_RECOMMENDED_STRATEGY_WEIGHTS = {
    "order_block": 1.3, "supply_demand": 1.1, "ema_cross": 0.8, "rsi_divergence": 0.8,
    "london_breakout": 1.1, "fibonacci": 0.8, "vwap_rejection": 1.0, "news_fade": 0.7,
    "multi_tf_align": 1.3, "bos_choch": 1.2, "liquidity_sweep": 1.3, "fair_value_gap": 1.1,
    "opening_range_breakout": 1.0,
    "macd_cross": 0.7, "bb_breakout": 0.8, "sr_breakout_retest": 1.1, "price_action": 1.0,
    "atr_donchian_breakout": 0.9,
    "order_flow_dom": 1.0, "macro_bias": 1.2,
    "scalp_london_sweep": 1.0, "scalp_ema_pullback": 1.1, "scalp_ny_orb": 0.8,
    "scalp_combo_sweep": 1.4,  # the user's "most recommended" 4-layer combo setup
    "myfxbook_sentiment": 0.8,  # must stay below macro_bias's 1.2 (Big Data) per user's rule
    "climax_reversal_sr": 1.0,  # 26th -- extreme/exhausted move + S/R + rejection candle
    "zone_mw_reversal": 1.1,  # 29th -- multi-touch H4 zone + M15 double top/bottom + neckline break
    "smart_money_sweep_morning": 1.0,  # 30th -- M1 sweep+reclaim / DOM delta / spike-wick, Asia 07-10 BKK
    "smart_money_sweep_night": 1.0,  # 31st -- same logic, US-close window 02-04 BKK
    "fib_confluence_sr": 1.2,       # 32nd -- Fibonacci Confluence S/R (Major+Minor Swing)
    "harmonic_patterns": 1.3,       # 33rd -- XABCD harmonic pattern PRZ + Fib-confluence cross-check + rejection candle
    "mtr_range_regime": 0.9,  # 27th -- MTR quantitative range-regime detector
    "mtr_trend_regime": 0.8,  # 28th -- MTR quantitative trend-regime detector
}
STRATEGY_WEIGHTS = {k: _RECOMMENDED_STRATEGY_WEIGHTS.get(k, 1.0) for k in strategies.STRATEGY_REGISTRY}
# Which of the (now 33) confluence strategies are turned on at all (a disabled strategy is
# excluded entirely — not scored, not displayed as voting).
CONFLUENCE_ENABLED_STRATEGIES = set(strategies.STRATEGY_REGISTRY.keys())
# Generic SL/TP construction for a confluence-triggered entry (it isn't tied
# to one strategy's structure level, so SL is ATR-based and TP follows the
# existing MIN_RISK_REWARD_RATIO floor via passes_risk_reward()).
CONFLUENCE_SL_ATR_MULT = 1.5
CONFLUENCE_TP_RR = 2.0

# --- League System: per-strategy auto-bench -------------------------------
# A strategy is benched (its score is shown but contributes 0 to the
# combined score) for LEAGUE_BENCH_HOURS once EITHER rule trips:
#   - LEAGUE_MAX_CONSECUTIVE_LOSSES losses in a row, OR
#   - rolling win-rate over its last LEAGUE_WINRATE_LOOKBACK_TRADES trades
#     falls below LEAGUE_MIN_WINRATE_PCT
# All four numbers are configurable in the UI's "League System" tab.
LEAGUE_ENABLED = True
LEAGUE_MAX_CONSECUTIVE_LOSSES = 3
LEAGUE_MIN_WINRATE_PCT = 45.0
LEAGUE_WINRATE_LOOKBACK_TRADES = 10
LEAGUE_BENCH_HOURS = 24

# --- ML decision layer: continuous shadow simulation + auto-weight --------
# On top of the time-based bench rules above, every scan also runs a
# zero-risk shadow/paper trade per strategy (strategy_simulator.py) so the
# League System always has fresh win/loss data — even for a strategy that's
# currently losing and not winning any real-trade slots. league.auto_weight()
# reads that combined (real + shadow) result history every scan and scales
# each strategy's weight down smoothly as its rolling win-rate falls below
# LEAGUE_MIN_WINRATE_PCT, and restores it the instant the win-rate recovers
# — no fixed cooldown, recovery is purely performance-driven.
SHADOW_SIMULATION_ENABLED = True
LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT = 5  # don't judge a strategy on too little data yet
LEAGUE_AUTO_DISABLE_WEIGHT_FLOOR = 0.05  # auto_weight at/below this -> treat as fully benched

# --- Order SL/TP options ------------------------------------------------------
# Defaults reproduce current behavior exactly — nothing changes until the user
# opens the UI "Order Options" panel and explicitly saves a different value.
ORDER_OPTIONS = {
    "use_sl":       True,       # False = send sl=0.0 (NO SL AT BROKER — unlimited risk)
    "use_tp":       True,       # False = send tp=0.0 (no TP, let trailing/basket manage exit)
    "tp_mode":      "strategy", # "strategy" = ATR/R:R TP as now; "fixed_usd" = calc from target $
    "tp_fixed_usd": 50.0,       # USD profit target per order (only used when tp_mode=fixed_usd)
}

# --- Breakeven SL ------------------------------------------------------------
# Once a position's floating profit reaches BREAKEVEN_TRIGGER_R multiples of
# its original SL distance, move SL to entry + BREAKEVEN_BUFFER_POINTS (in
# the profit direction) so the trade can no longer turn into a loss. Runs
# independently of (and before) the regular trailing-stop method.
BREAKEVEN_ENABLED = True
BREAKEVEN_TRIGGER_R = 1.0
BREAKEVEN_BUFFER_POINTS = 2.0

# --- Telegram Alert ----------------------------------------------------------
# Fill these in yourself via strategy_config_ui.py -> "Telegram" tab (or
# directly in strategy_config.json under "telegram") — this script never
# asks for or transmits these anywhere except straight to api.telegram.org.
TELEGRAM_ENABLED = False
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# --- Myfxbook Sentiment (25th strategy) ---------------------------------------
# Fill these in via strategy_config_ui.py -> "Myfxbook Sentiment" tab (or
# directly in strategy_config.json under "myfxbook"). Only the session token,
# never the password, is cached to disk by macro_data.py.
MYFXBOOK_ENABLED = False
MYFXBOOK_EMAIL = ""
MYFXBOOK_PASSWORD = ""
MYFXBOOK_CONTRARIAN = True   # True = fade the crowd (default); False = follow crowd

# --- Stop on Error -----------------------------------------------------------
# Fill these in via strategy_config_ui.py -> "Log / Debug" tab (or directly in
# strategy_config.json under "error_handling"). Default is OFF: a single bad
# loop iteration (transient network/MT5 hiccup) just gets logged and the loop
# keeps running so open positions still get trailing-stop/basket-close care.
# Turn this ON if you'd rather the bot stop completely (and alert you via
# Telegram) the moment something unexpected happens, instead of risking it
# running blind on bad data.
STOP_ON_ERROR = False
MAX_ERRORS_BEFORE_STOP = 1

# --- Notifications (Telegram) -------------------------------------------------
# Fill these in via strategy_config.json under "notifications" (no UI tab yet
# — edit the JSON directly if you want to change any of these). All default
# to True so every event class fires once Telegram is enabled:
#   startup        -> once, right after MT5 connects (this run's full config)
#   signal         -> every time a setup qualifies (before any order is sent)
#   order_open     -> every time an order is actually placed
#   order_close    -> every time a position closes
#   macro_update   -> periodic Big Data / Gold Decision Matrix summary
#   pre_news       -> ~1h before a High-impact USD economic release
#   post_news      -> right after that release prints (actual vs forecast)
#   daily_status   -> once/day heartbeat at DAILY_STATUS_HOUR local time
NOTIFY_STARTUP = True
NOTIFY_SIGNAL = True
NOTIFY_ORDER_OPEN = True
NOTIFY_ORDER_CLOSE = True
NOTIFY_MACRO_UPDATE = True
NOTIFY_PRE_NEWS = True
NOTIFY_POST_NEWS = True
NOTIFY_DAILY_STATUS = True
MACRO_UPDATE_INTERVAL_HOURS = 6.0        # how often to push the Big Data summary
PROXY_STALENESS_ALERT_ENABLED = True     # Telegram alert when macro data on proxy > threshold
PROXY_STALENESS_ALERT_HOURS   = 24.0    # hours on proxy before first alert fires
DAILY_STATUS_HOUR = 8               # local-time hour for the daily heartbeat
PRE_NEWS_MINUTES = 60                # how far ahead to warn before a release
POST_NEWS_WINDOW_MINUTES = 30        # how long after a release to keep trying
                                      # to pick up its "actual" figure

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY_META_PATH = os.path.join(_THIS_DIR, "open_entry_meta.json")          # ticket -> contributing strategies
SCORES_SNAPSHOT_PATH = os.path.join(_THIS_DIR, "strategy_scores.json")     # latest scan, for the dashboard
MARKET_STATE_PATH    = os.path.join(_THIS_DIR, "market_state.json")         # market open/closed status, updated every tick
PROCESSED_DEALS_PATH = os.path.join(_THIS_DIR, "processed_deals.json")    # avoids double-counting league results
NEWS_ALERT_STATE_PATH = os.path.join(_THIS_DIR, "news_alert_state.json")  # dedupes pre/post-news alerts
BOT_STATE_PATH = os.path.join(_THIS_DIR, "bot_state.json")                # records this process's start time, for dashboard uptime
MANUAL_TRADES_PATH = os.path.join(_THIS_DIR, "manual_trades.json")        # captures manually opened/closed trades for combined P&L
_LEAGUE_STATE = league.load_state()
_SHADOW_STATE = strategy_simulator.load_state()

CONFIG_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_config.json")
_LAST_CONFIG_MTIME = None   # used by maybe_reload_config() for live-reload
_CONFIG_LOADED_ONCE = False  # guards the SYMBOL hot-reload safety check below

# --- Logging ---
# All runtime messages go through the "xauusd_ea" logger instead of print().
# Writes a rotating file under LOG_DIR (created automatically) and, if
# LOG_TO_CONSOLE is True, also echoes to the console/terminal.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_FILE_NAME = "xauusd_ea.log"
LOG_LEVEL = "INFO"           # DEBUG / INFO / WARNING / ERROR / CRITICAL
LOG_TO_CONSOLE = True
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024   # rotate after 5 MB
LOG_BACKUP_COUNT = 5                    # keep this many old rotated files

logger = logging.getLogger("xauusd_ea")


def setup_logging(log_dir=None, level=None, to_console=None, max_bytes=None, backup_count=None):
    """Configures the module-level `logger` with a rotating file handler
    (and optional console handler). Safe to call more than once — clears
    any handlers already attached so config reloads don't duplicate log
    lines."""
    log_dir = log_dir if log_dir is not None else LOG_DIR
    level = level if level is not None else LOG_LEVEL
    to_console = LOG_TO_CONSOLE if to_console is None else to_console
    max_bytes = max_bytes if max_bytes is not None else LOG_FILE_MAX_BYTES
    backup_count = backup_count if backup_count is not None else LOG_BACKUP_COUNT

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, LOG_FILE_NAME)

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()  # avoid duplicate lines if setup_logging() reruns

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if to_console:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.propagate = False
    return logger


# Attach default handlers immediately on import so any logger.* call made
# before load_ui_config() runs (or if strategy_config.json is missing/has
# no "logging" section) still gets written somewhere, not silently dropped.
setup_logging()


def load_ui_config(path=CONFIG_JSON_PATH):
    """Loads strategy_config.json (written by strategy_config_ui.py) and
    overrides the matching module-level globals. Safe no-op if the file
    doesn't exist or a field is missing — defaults above are kept."""
    global SYMBOL, RISK_PER_TRADE, LOT_STEP, VALUE_PER_POINT_PER_LOT, MAX_CONCURRENT_TRADES, AUTO_TRADE
    global MIN_TRADE_INTERVAL_MINUTES, MIN_TRADE_INTERVAL_MINUTES_SCALPING
    global TRAILING_ENABLED, TRAILING_METHOD, TRAILING_ATR_MULT, TRAILING_FIXED_POINTS
    global TRAILING_PERCENT, TRAILING_EMA_PERIOD, TRAILING_EMA_BUFFER_POINTS
    global TRAILING_ACTIVATION_R, TRAILING_REMOVE_TP_ON_ACTIVATE, TRAILING_CHECK_SECONDS
    global BASKET_CLOSE_ENABLED, BASKET_TARGET_PROFIT_USD, BASKET_MAX_LOSS_USD
    global BASKET_TARGET_PROFIT_PCT, BASKET_MAX_LOSS_PCT, ENABLED_STRATEGIES
    global DAILY_FILTER_ENABLED, DAILY_RSI_OVERBOUGHT, DAILY_RSI_OVERSOLD, DAILY_REQUIRE_FULL_STACK
    global MIN_LOT, MAX_LOT, ENFORCE_MIN_LOT, MAX_DAILY_TRADES, MAX_DRAWDOWN_PCT, DAILY_LOSS_LIMIT_R
    global MIN_RISK_REWARD_RATIO, MAX_CONSECUTIVE_LOSSES
    global TRADING_HOURS_FILTER_ENABLED, ALLOWED_SESSIONS
    global MARKET_HOURS_CHECK_ENABLED, MARKET_CLOSED_MAX_TICK_AGE_SEC, MARKET_CLOSED_NOTIFY
    global MARKET_PRICE_SANITY_CHECK_ENABLED, MARKET_PRICE_SANITY_TOLERANCE_PCT
    global LOG_DIR, LOG_LEVEL, LOG_TO_CONSOLE, LOG_FILE_MAX_BYTES, LOG_BACKUP_COUNT
    global ENTRY_MODE, SCAN_INTERVAL_SECONDS, MIN_STRATEGY_SCORE, MIN_AGREEING_STRATEGIES
    global LOGIC_GROUP_SELECTION, LOGIC_GROUPS_APPLY_DAILY_FILTER
    global STRATEGY_WEIGHTS, CONFLUENCE_ENABLED_STRATEGIES, CONFLUENCE_SL_ATR_MULT, CONFLUENCE_TP_RR
    global LEAGUE_ENABLED, LEAGUE_MAX_CONSECUTIVE_LOSSES, LEAGUE_MIN_WINRATE_PCT
    global LEAGUE_WINRATE_LOOKBACK_TRADES, LEAGUE_BENCH_HOURS
    global SHADOW_SIMULATION_ENABLED, LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT
    global ORDER_OPTIONS
    global BREAKEVEN_ENABLED, BREAKEVEN_TRIGGER_R, BREAKEVEN_BUFFER_POINTS
    global TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    global MYFXBOOK_ENABLED, MYFXBOOK_EMAIL, MYFXBOOK_PASSWORD, MYFXBOOK_CONTRARIAN
    global STOP_ON_ERROR, MAX_ERRORS_BEFORE_STOP
    global NOTIFY_STARTUP, NOTIFY_SIGNAL, NOTIFY_ORDER_OPEN, NOTIFY_ORDER_CLOSE
    global NOTIFY_MACRO_UPDATE, NOTIFY_PRE_NEWS, NOTIFY_POST_NEWS, NOTIFY_DAILY_STATUS
    global MACRO_UPDATE_INTERVAL_HOURS, DAILY_STATUS_HOUR, PRE_NEWS_MINUTES, POST_NEWS_WINDOW_MINUTES
    global PROXY_STALENESS_ALERT_ENABLED, PROXY_STALENESS_ALERT_HOURS
    global _CONFIG_LOADED_ONCE

    if not os.path.exists(path):
        logger.warning(f"No strategy_config.json found at {path} — using script defaults.")
        return

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    risk = cfg.get("risk", {})
    _previous_symbol = SYMBOL
    SYMBOL = risk.get("symbol", SYMBOL)
    if _CONFIG_LOADED_ONCE and SYMBOL != _previous_symbol:
        # Changing the trading symbol while the bot (and any open positions
        # tracked under the old symbol) is already running is not safe to
        # hot-apply — revert it and ask for a restart instead. Every other
        # setting below IS safe to hot-apply and proceeds normally.
        logger.warning(
            f"Live config reload: symbol change '{_previous_symbol}' -> '{SYMBOL}' "
            f"requires a bot restart and was NOT applied live. Keeping '{_previous_symbol}' "
            f"for this session; restart the bot to switch symbols."
        )
        SYMBOL = _previous_symbol
    RISK_PER_TRADE = float(risk.get("risk_per_trade_pct", RISK_PER_TRADE * 100)) / 100.0
    LOT_STEP = float(risk.get("lot_step", LOT_STEP))
    VALUE_PER_POINT_PER_LOT = float(risk.get("value_per_point_per_lot", VALUE_PER_POINT_PER_LOT))
    MAX_CONCURRENT_TRADES = int(risk.get("max_concurrent_trades", MAX_CONCURRENT_TRADES))
    MIN_TRADE_INTERVAL_MINUTES = float(risk.get("min_trade_interval_minutes", MIN_TRADE_INTERVAL_MINUTES))
    MIN_TRADE_INTERVAL_MINUTES_SCALPING = float(risk.get("min_trade_interval_minutes_scalping", MIN_TRADE_INTERVAL_MINUTES_SCALPING))
    AUTO_TRADE = bool(risk.get("auto_trade", AUTO_TRADE))

    mm = cfg.get("money_management", {})
    MIN_LOT = float(mm.get("min_lot", MIN_LOT))
    MAX_LOT = float(mm.get("max_lot", MAX_LOT))
    ENFORCE_MIN_LOT = bool(mm.get("enforce_min_lot", ENFORCE_MIN_LOT))
    max_daily = mm.get("max_daily_trades", MAX_DAILY_TRADES)
    MAX_DAILY_TRADES = int(max_daily) if max_daily not in (None, "") else None
    max_dd = mm.get("max_drawdown_pct", MAX_DRAWDOWN_PCT)
    MAX_DRAWDOWN_PCT = float(max_dd) if max_dd not in (None, "") else None
    daily_loss_r = mm.get("daily_loss_limit_r", DAILY_LOSS_LIMIT_R)
    DAILY_LOSS_LIMIT_R = float(daily_loss_r) if daily_loss_r not in (None, "") else None
    min_rr = mm.get("min_risk_reward_ratio", MIN_RISK_REWARD_RATIO)
    MIN_RISK_REWARD_RATIO = float(min_rr) if min_rr not in (None, "") else None
    max_streak = mm.get("max_consecutive_losses", MAX_CONSECUTIVE_LOSSES)
    MAX_CONSECUTIVE_LOSSES = int(max_streak) if max_streak not in (None, "") else None

    t = cfg.get("trailing_stop", {})
    TRAILING_ENABLED = bool(t.get("enabled", TRAILING_ENABLED))
    TRAILING_METHOD = t.get("method", TRAILING_METHOD)
    TRAILING_ATR_MULT = float(t.get("atr_mult", TRAILING_ATR_MULT))
    TRAILING_FIXED_POINTS = float(t.get("fixed_points", TRAILING_FIXED_POINTS))
    TRAILING_PERCENT = float(t.get("percent", TRAILING_PERCENT))
    TRAILING_EMA_PERIOD = int(t.get("ema_period", TRAILING_EMA_PERIOD))
    TRAILING_EMA_BUFFER_POINTS = float(t.get("ema_buffer_points", TRAILING_EMA_BUFFER_POINTS))
    TRAILING_ACTIVATION_R = float(t.get("activation_r", TRAILING_ACTIVATION_R))
    TRAILING_REMOVE_TP_ON_ACTIVATE = bool(t.get("remove_tp_on_activate", TRAILING_REMOVE_TP_ON_ACTIVATE))
    TRAILING_CHECK_SECONDS = int(t.get("check_seconds", TRAILING_CHECK_SECONDS))

    def _flt(v):
        try: return float(v) if v not in (None, "", "None") else None
        except (ValueError, TypeError): return None

    b = cfg.get("basket_close", {})
    BASKET_CLOSE_ENABLED = bool(b.get("enabled", BASKET_CLOSE_ENABLED))
    BASKET_TARGET_PROFIT_USD = _flt(b.get("target_profit_usd"))
    BASKET_MAX_LOSS_USD      = _flt(b.get("max_loss_usd"))
    BASKET_TARGET_PROFIT_PCT = _flt(b.get("target_profit_pct"))
    BASKET_MAX_LOSS_PCT      = _flt(b.get("max_loss_pct"))

    th = cfg.get("trading_hours", {})
    TRADING_HOURS_FILTER_ENABLED = bool(th.get("enabled", TRADING_HOURS_FILTER_ENABLED))
    sessions_cfg = th.get("sessions", {})
    if sessions_cfg:
        ALLOWED_SESSIONS = {k for k, v in sessions_cfg.items() if v}

    mh = cfg.get("market_hours", {})
    MARKET_HOURS_CHECK_ENABLED = bool(mh.get("enabled", MARKET_HOURS_CHECK_ENABLED))
    MARKET_CLOSED_MAX_TICK_AGE_SEC = float(mh.get("max_tick_age_sec", MARKET_CLOSED_MAX_TICK_AGE_SEC))
    MARKET_CLOSED_NOTIFY = bool(mh.get("notify", MARKET_CLOSED_NOTIFY))
    MARKET_PRICE_SANITY_CHECK_ENABLED = bool(mh.get("price_sanity_enabled", MARKET_PRICE_SANITY_CHECK_ENABLED))
    MARKET_PRICE_SANITY_TOLERANCE_PCT = float(mh.get("price_sanity_tolerance_pct", MARKET_PRICE_SANITY_TOLERANCE_PCT))

    lg = cfg.get("logging", {})
    LOG_DIR = lg.get("log_dir", LOG_DIR)
    LOG_LEVEL = lg.get("level", LOG_LEVEL)
    LOG_TO_CONSOLE = bool(lg.get("log_to_console", LOG_TO_CONSOLE))
    LOG_FILE_MAX_BYTES = int(lg.get("max_bytes", LOG_FILE_MAX_BYTES))
    LOG_BACKUP_COUNT = int(lg.get("backup_count", LOG_BACKUP_COUNT))
    setup_logging(LOG_DIR, LOG_LEVEL, LOG_TO_CONSOLE, LOG_FILE_MAX_BYTES, LOG_BACKUP_COUNT)

    d = cfg.get("daily_filter", {})
    DAILY_FILTER_ENABLED = bool(d.get("enabled", DAILY_FILTER_ENABLED))
    DAILY_RSI_OVERBOUGHT = float(d.get("rsi_overbought", DAILY_RSI_OVERBOUGHT))
    DAILY_RSI_OVERSOLD = float(d.get("rsi_oversold", DAILY_RSI_OVERSOLD))
    DAILY_REQUIRE_FULL_STACK = bool(d.get("require_full_stack", DAILY_REQUIRE_FULL_STACK))

    legacy_strategies_cfg = cfg.get("strategies", {})
    ENABLED_STRATEGIES = {k for k, v in legacy_strategies_cfg.items() if v.get("enabled")}

    conf = cfg.get("confluence", {})
    ENTRY_MODE = conf.get("entry_mode", ENTRY_MODE)
    LOGIC_GROUP_SELECTION = conf.get("logic_group_selection", LOGIC_GROUP_SELECTION)
    LOGIC_GROUPS_APPLY_DAILY_FILTER = bool(conf.get("logic_groups_apply_daily_filter", LOGIC_GROUPS_APPLY_DAILY_FILTER))
    SCAN_INTERVAL_SECONDS = int(conf.get("scan_interval_seconds", SCAN_INTERVAL_SECONDS))
    MIN_STRATEGY_SCORE = float(conf.get("min_strategy_score", MIN_STRATEGY_SCORE))
    MIN_AGREEING_STRATEGIES = int(conf.get("min_agreeing_strategies", MIN_AGREEING_STRATEGIES))
    CONFLUENCE_SL_ATR_MULT = float(conf.get("sl_atr_mult", CONFLUENCE_SL_ATR_MULT))
    CONFLUENCE_TP_RR = float(conf.get("tp_rr", CONFLUENCE_TP_RR))
    strat_cfg = conf.get("strategies", {})
    if strat_cfg:
        CONFLUENCE_ENABLED_STRATEGIES = {k for k, v in strat_cfg.items() if v.get("enabled", True)}
        STRATEGY_WEIGHTS = {k: float(v.get("weight", 1.0)) for k, v in strat_cfg.items()}
        # any strategy not mentioned in config keeps its previous default
        for k in strategies.STRATEGY_REGISTRY:
            if k not in strat_cfg:
                CONFLUENCE_ENABLED_STRATEGIES.add(k)
                STRATEGY_WEIGHTS.setdefault(k, 1.0)

    lg_cfg = cfg.get("league", {})
    LEAGUE_ENABLED = bool(lg_cfg.get("enabled", LEAGUE_ENABLED))
    LEAGUE_MAX_CONSECUTIVE_LOSSES = int(lg_cfg.get("max_consecutive_losses", LEAGUE_MAX_CONSECUTIVE_LOSSES))
    LEAGUE_MIN_WINRATE_PCT = float(lg_cfg.get("min_winrate_pct", LEAGUE_MIN_WINRATE_PCT))
    LEAGUE_WINRATE_LOOKBACK_TRADES = int(lg_cfg.get("winrate_lookback_trades", LEAGUE_WINRATE_LOOKBACK_TRADES))
    LEAGUE_BENCH_HOURS = float(lg_cfg.get("bench_hours", LEAGUE_BENCH_HOURS))
    SHADOW_SIMULATION_ENABLED = bool(lg_cfg.get("shadow_simulation_enabled", SHADOW_SIMULATION_ENABLED))
    LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT = int(
        lg_cfg.get("min_samples_for_adjustment", LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT)
    )

    oo = cfg.get("order_options", {})
    ORDER_OPTIONS["use_sl"]       = bool(oo.get("use_sl",       ORDER_OPTIONS["use_sl"]))
    ORDER_OPTIONS["use_tp"]       = bool(oo.get("use_tp",       ORDER_OPTIONS["use_tp"]))
    ORDER_OPTIONS["tp_mode"]      = oo.get("tp_mode",           ORDER_OPTIONS["tp_mode"])
    ORDER_OPTIONS["tp_fixed_usd"] = float(oo.get("tp_fixed_usd", ORDER_OPTIONS["tp_fixed_usd"]))

    be_cfg = cfg.get("breakeven", {})
    BREAKEVEN_ENABLED = bool(be_cfg.get("enabled", BREAKEVEN_ENABLED))
    BREAKEVEN_TRIGGER_R = float(be_cfg.get("trigger_r", BREAKEVEN_TRIGGER_R))
    BREAKEVEN_BUFFER_POINTS = float(be_cfg.get("buffer_points", BREAKEVEN_BUFFER_POINTS))

    tg_cfg = cfg.get("telegram", {})
    TELEGRAM_ENABLED = bool(tg_cfg.get("enabled", TELEGRAM_ENABLED))
    TELEGRAM_BOT_TOKEN = tg_cfg.get("bot_token", TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = tg_cfg.get("chat_id", TELEGRAM_CHAT_ID)

    mfb_cfg = cfg.get("myfxbook", {})
    MYFXBOOK_ENABLED = bool(mfb_cfg.get("enabled", MYFXBOOK_ENABLED))
    MYFXBOOK_EMAIL = mfb_cfg.get("email", MYFXBOOK_EMAIL)
    MYFXBOOK_PASSWORD = mfb_cfg.get("password", MYFXBOOK_PASSWORD)
    MYFXBOOK_CONTRARIAN = bool(mfb_cfg.get("contrarian", MYFXBOOK_CONTRARIAN))

    err_cfg = cfg.get("error_handling", {})
    STOP_ON_ERROR = bool(err_cfg.get("stop_on_error", STOP_ON_ERROR))
    MAX_ERRORS_BEFORE_STOP = int(err_cfg.get("max_errors_before_stop", MAX_ERRORS_BEFORE_STOP))

    notif = cfg.get("notifications", {})
    NOTIFY_STARTUP = bool(notif.get("startup", NOTIFY_STARTUP))
    NOTIFY_SIGNAL = bool(notif.get("signal", NOTIFY_SIGNAL))
    NOTIFY_ORDER_OPEN = bool(notif.get("order_open", NOTIFY_ORDER_OPEN))
    NOTIFY_ORDER_CLOSE = bool(notif.get("order_close", NOTIFY_ORDER_CLOSE))
    NOTIFY_MACRO_UPDATE = bool(notif.get("macro_update", NOTIFY_MACRO_UPDATE))
    NOTIFY_PRE_NEWS = bool(notif.get("pre_news", NOTIFY_PRE_NEWS))
    NOTIFY_POST_NEWS = bool(notif.get("post_news", NOTIFY_POST_NEWS))
    NOTIFY_DAILY_STATUS = bool(notif.get("daily_status", NOTIFY_DAILY_STATUS))
    MACRO_UPDATE_INTERVAL_HOURS = float(notif.get("macro_update_interval_hours", MACRO_UPDATE_INTERVAL_HOURS))

    md_cfg = cfg.get("macro_data", {})
    PROXY_STALENESS_ALERT_ENABLED = bool(md_cfg.get("proxy_staleness_alert_enabled", PROXY_STALENESS_ALERT_ENABLED))
    PROXY_STALENESS_ALERT_HOURS   = float(md_cfg.get("proxy_staleness_alert_hours", PROXY_STALENESS_ALERT_HOURS))
    DAILY_STATUS_HOUR = int(notif.get("daily_status_hour", DAILY_STATUS_HOUR))
    PRE_NEWS_MINUTES = int(notif.get("pre_news_minutes", PRE_NEWS_MINUTES))
    POST_NEWS_WINDOW_MINUTES = int(notif.get("post_news_window_minutes", POST_NEWS_WINDOW_MINUTES))

    logger.info(f"Loaded config from {path}. Enabled strategies: {sorted(CONFLUENCE_ENABLED_STRATEGIES)}")
    logger.info(f"Entry mode: {ENTRY_MODE}"
                + (f" (logic_group_selection={LOGIC_GROUP_SELECTION})" if ENTRY_MODE == "logic_groups" else "")
                + f" | Confluence: min_score={MIN_STRATEGY_SCORE} "
                f"min_agreeing={MIN_AGREEING_STRATEGIES} scan_every={SCAN_INTERVAL_SECONDS}s | "
                f"League: enabled={LEAGUE_ENABLED} | Telegram: enabled={TELEGRAM_ENABLED}")
    _CONFIG_LOADED_ONCE = True


def maybe_reload_config(path=CONFIG_JSON_PATH):
    """Live-configuration hook — call this once per main-loop tick.

    If strategy_config.json has changed on disk since the last check (e.g.
    saved from strategy_config_ui.py while the bot is already running, or
    edited directly and saved), every setting is re-applied immediately via
    load_ui_config() — no restart required. Because this runs every loop
    tick, a change saved from the UI typically takes effect within
    TRAILING_CHECK_SECONDS.

    Uses the file's mtime (not a content hash) to detect changes, which is
    cheap (a single stat() call) and sufficient here since the UI always
    rewrites the whole file on Save. The one setting that genuinely isn't
    safe to hot-apply (the trading SYMBOL, since open positions are tracked
    against it) is reverted by load_ui_config() itself, with a warning
    asking for a restart — every other setting (risk %, lot sizing, league
    thresholds, strategy weights/enables, sessions, notifications, etc.)
    takes effect live.

    Returns True if a reload happened this call, False otherwise."""
    global _LAST_CONFIG_MTIME
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False  # file briefly missing/being replaced — try again next tick

    if _LAST_CONFIG_MTIME is not None and mtime <= _LAST_CONFIG_MTIME:
        return False

    _LAST_CONFIG_MTIME = mtime
    if not _CONFIG_LOADED_ONCE:
        return False  # startup's own load_ui_config() call already covers this

    logger.info("strategy_config.json changed on disk — reloading settings live (no restart needed).")
    try:
        load_ui_config(path)
    except Exception:
        logger.exception("Live config reload failed — keeping previous settings active.")
        return False

    send_telegram(
        "🔄 Config reloaded live — your latest strategy_config_ui.py settings are now active "
        "(no bot restart needed)."
    )
    return True


# ----------------------------- NOTIFICATIONS (Telegram) ---------------------
def send_telegram(text):
    """One-line wrapper used by every notification call site below — keeps
    the TELEGRAM_ENABLED check and exception-swallowing in one place instead
    of repeated at each call site."""
    if not TELEGRAM_ENABLED:
        return False
    try:
        return telegram_alert.send_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text, enabled=TELEGRAM_ENABLED)
    except Exception:
        logger.exception("Failed to send a Telegram notification.")
        return False


def send_startup_notification(info):
    """(1) Startup notification — fires once, right after MT5 connects,
    with the start time and a flattened summary of every config section
    that matters for this run (so you can confirm from your phone that the
    config you just saved is actually what's running on the VPS)."""
    if not (TELEGRAM_ENABLED and NOTIFY_STARTUP):
        return
    summary = {
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Symbol": SYMBOL,
        "Entry mode": ENTRY_MODE if ENTRY_MODE != "logic_groups" else f"logic_groups ({LOGIC_GROUP_SELECTION})",
        "Auto trade": AUTO_TRADE,
        "Risk per trade": f"{RISK_PER_TRADE * 100:.2f}%",
        "Max concurrent trades": MAX_CONCURRENT_TRADES,
        "Min lot / Max lot": f"{MIN_LOT} / {MAX_LOT}",
        "Min R:R": MIN_RISK_REWARD_RATIO,
        "Max daily trades": MAX_DAILY_TRADES,
        "Daily loss limit (R)": DAILY_LOSS_LIMIT_R,
        "Max drawdown %": MAX_DRAWDOWN_PCT,
        "Max consecutive losses": MAX_CONSECUTIVE_LOSSES,
        "Daily trend filter": DAILY_FILTER_ENABLED,
        "Trading hours filter": f"{TRADING_HOURS_FILTER_ENABLED} ({sorted(ALLOWED_SESSIONS)})",
        "Trailing stop": f"{TRAILING_ENABLED} ({TRAILING_METHOD})",
        "Breakeven": BREAKEVEN_ENABLED,
        "Basket close": BASKET_CLOSE_ENABLED,
        "Confluence min score / agreeing": f"{MIN_STRATEGY_SCORE} / {MIN_AGREEING_STRATEGIES}",
        "Strategies enabled": len(CONFLUENCE_ENABLED_STRATEGIES),
        "League system": LEAGUE_ENABLED,
        "Stop on error": f"{STOP_ON_ERROR} (max {MAX_ERRORS_BEFORE_STOP})",
    }
    send_telegram(telegram_alert.format_startup_alert(summary, account_info=info, symbol=SYMBOL))


def _news_event_key(e):
    return f"{e.get('title')}|{e.get('date')}"


def _build_news_impact_note(e):
    """(6/7) Best-effort, GENERIC Bulls/Bears note comparing actual vs.
    forecast for a just-released economic figure. This is a simplified
    heuristic (actual beating forecast generally firms up USD -> headwind
    for Gold, and vice versa) — real per-indicator economics are more
    nuanced (e.g. a hot CPI print can sometimes be read as Gold-bullish
    via inflation-hedge demand even though it's USD-hawkish). Treat this as
    a rough guide to sanity-check against price action, not a certainty."""
    try:
        actual = float(str(e.get("actual")).replace("%", "").replace(",", ""))
        forecast = float(str(e.get("forecast")).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return "Actual vs Forecast comparison unavailable (non-numeric figures)."
    if actual > forecast:
        return ("Actual > Forecast — generally USD-supportive -> potential headwind "
                "for Gold (Bearish-leaning), but confirm with price action.")
    if actual < forecast:
        return ("Actual < Forecast — generally USD-negative -> potential tailwind "
                "for Gold (Bullish-leaning), but confirm with price action.")
    return "Actual matched Forecast — limited surprise, muted directional impact expected."


_LAST_MACRO_NOTIFY_TS = 0.0


def check_macro_update_notify():
    """(5) Periodic Big Data / Macro update — pushes the weighted Gold
    Decision Matrix (strategies.score_macro_bias()) plus the raw figures
    behind it, every MACRO_UPDATE_INTERVAL_HOURS. Independent of the
    confluence scan's own (possibly stale) macro snapshot — this always
    reads fresh-or-cached via macro_data.get_macro_snapshot()."""
    global _LAST_MACRO_NOTIFY_TS
    if not (TELEGRAM_ENABLED and NOTIFY_MACRO_UPDATE):
        return
    interval_seconds = max(MACRO_UPDATE_INTERVAL_HOURS, 0.1) * 3600
    now = time.time()
    if now - _LAST_MACRO_NOTIFY_TS < interval_seconds:
        return
    macro = get_macro_snapshot_safe()
    if not macro:
        return
    try:
        bias = strategies.score_macro_bias({"macro": macro})
    except Exception:
        logger.exception("score_macro_bias() failed during macro update notification.")
        return
    send_telegram(telegram_alert.format_macro_update_alert(bias, macro, symbol=SYMBOL))
    _LAST_MACRO_NOTIFY_TS = now


_LAST_PROXY_ALERT_SENT: "dict[str, str]" = {}  # data_key -> "since" ISO str of the episode already alerted


def check_proxy_staleness_notify():
    """Fires a one-time Telegram alert per data key when macro data has been
    on a proxy/fallback source for >= PROXY_STALENESS_ALERT_HOURS. Resets
    when that key recovers to a primary source so a future episode can alert
    again. Follows the same one-per-transition pattern as check_macro_update_notify()."""
    if not (TELEGRAM_ENABLED and PROXY_STALENESS_ALERT_ENABLED):
        return
    try:
        report = macro_data.get_proxy_staleness_report()
    except Exception:
        return
    current_keys = set()
    for entry in report:
        key = entry["data_key"]
        current_keys.add(key)
        if entry["hours"] < PROXY_STALENESS_ALERT_HOURS:
            continue
        if _LAST_PROXY_ALERT_SENT.get(key) == entry["since"]:
            continue  # already alerted for this episode
        try:
            send_telegram(telegram_alert.format_proxy_staleness_alert(
                key, entry["hours"], entry["proxy_source"], symbol=SYMBOL))
            _LAST_PROXY_ALERT_SENT[key] = entry["since"]
        except Exception:
            logger.exception(f"Failed to send proxy staleness alert for {key}.")
    # Clear sent-alert record for any key that recovered to primary
    for key in list(_LAST_PROXY_ALERT_SENT):
        if key not in current_keys:
            del _LAST_PROXY_ALERT_SENT[key]


def check_pre_news_notify():
    """(6) Warns PRE_NEWS_MINUTES (default 60) before a scheduled High-impact
    USD economic release, with its forecast/previous figures. Dedupes per
    event via NEWS_ALERT_STATE_PATH so the same release isn't re-alerted
    every loop tick while it's inside the warning window."""
    if not (TELEGRAM_ENABLED and NOTIFY_PRE_NEWS):
        return
    try:
        soon = macro_data.upcoming_high_impact_events(within_minutes=PRE_NEWS_MINUTES)
    except Exception:
        logger.exception("upcoming_high_impact_events() failed during pre-news check.")
        return
    if not soon:
        return

    state = _load_json(NEWS_ALERT_STATE_PATH, {"pre": [], "post": []})
    alerted = set(state.get("pre", []))
    changed = False
    for e in soon:
        key = _news_event_key(e)
        if key in alerted:
            continue
        send_telegram(telegram_alert.format_pre_news_alert(e))
        alerted.add(key)
        changed = True
    if changed:
        state["pre"] = sorted(alerted)[-500:]
        _save_json(NEWS_ALERT_STATE_PATH, state)


_LAST_FORCE_CALENDAR_FETCH_TS = 0.0


def check_post_news_notify():
    """(7) After a High-impact USD release's scheduled time has passed (within
    POST_NEWS_WINDOW_MINUTES), tries to pick up its "actual" figure and sends
    actual-vs-forecast plus a best-effort Bulls/Bears note. Force-refreshes
    the calendar feed (bypassing its normal ~6h cache) but throttled to once
    every 2 minutes so the post-news window doesn't hammer the feed."""
    global _LAST_FORCE_CALENDAR_FETCH_TS
    if not (TELEGRAM_ENABLED and NOTIFY_POST_NEWS):
        return
    try:
        from datetime import timezone
        calendar = macro_data.fetch_economic_calendar()
        if not calendar or not calendar.get("events"):
            return
        now = datetime.now(timezone.utc)
        recent = []
        for e in calendar["events"]:
            if e.get("impact") != "High" or e.get("country") != "USD":
                continue
            try:
                dt = datetime.fromisoformat(e["date"])
            except (ValueError, TypeError, KeyError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_minutes = (now - dt).total_seconds() / 60.0
            if 0 <= age_minutes <= POST_NEWS_WINDOW_MINUTES:
                recent.append(e)
        if not recent:
            return

        state = _load_json(NEWS_ALERT_STATE_PATH, {"pre": [], "post": []})
        alerted = set(state.get("post", []))
        pending = [e for e in recent if _news_event_key(e) not in alerted]
        if not pending:
            return

        if time.time() - _LAST_FORCE_CALENDAR_FETCH_TS > 120:
            fresh = macro_data.fetch_economic_calendar(force=True)
            _LAST_FORCE_CALENDAR_FETCH_TS = time.time()
            if fresh and fresh.get("events"):
                fresh_by_key = {_news_event_key(fe): fe for fe in fresh["events"]}
                pending = [fresh_by_key.get(_news_event_key(e), e) for e in pending]

        changed = False
        for e in pending:
            if e.get("actual") in (None, ""):
                continue  # actual not published yet — retry next tick until the window closes
            note = _build_news_impact_note(e)
            send_telegram(telegram_alert.format_post_news_alert(e, note))
            alerted.add(_news_event_key(e))
            changed = True

        if changed:
            state["post"] = sorted(alerted)[-500:]
            _save_json(NEWS_ALERT_STATE_PATH, state)
    except Exception:
        logger.exception("check_post_news_notify() failed.")


_LAST_DAILY_STATUS_DATE = None


def check_daily_status_notify():
    """(8) Once-per-day bot-status heartbeat at DAILY_STATUS_HOUR (default
    08:00) local time — balance/equity, open positions, today's P&L and
    trade count. Sends at most once per calendar day."""
    global _LAST_DAILY_STATUS_DATE
    if not (TELEGRAM_ENABLED and NOTIFY_DAILY_STATUS):
        return
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_STATUS_HOUR or _LAST_DAILY_STATUS_DATE == today_str:
        return
    info = mt5.account_info()
    positions = mt5.positions_get(symbol=SYMBOL) or []
    open_count = sum(1 for p in positions if p.magic == MAGIC_NUMBER)
    try:
        today_pnl = get_today_realized_pnl() + get_floating_pnl()
    except Exception:
        today_pnl = None
    try:
        today_trades = count_today_new_trades()
    except Exception:
        today_trades = None
    send_telegram(telegram_alert.format_daily_status_alert(info, open_count, today_pnl, today_trades, symbol=SYMBOL))
    _LAST_DAILY_STATUS_DATE = today_str


# ----------------------------- CONNECTION -------------------------------
def connect():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("Could not read account info — check MT5 login.")
    logger.info(f"Connected: {info.login} | Balance: {info.balance} {info.currency}")
    return info


def record_bot_start():
    """Writes this process's start time to BOT_STATE_PATH so
    generate_dashboard.py can show how long the bot has been running —
    called once per process launch, right after the first successful
    connect() in main(). Deliberately separate from the per-scan
    strategy_scores.json snapshot since this should NOT be overwritten on
    every loop tick, only on an actual (re)start."""
    state = {
        "started_at": datetime.now().isoformat(),
        "pid": os.getpid(),
    }
    try:
        _save_json(BOT_STATE_PATH, state)
    except OSError:
        logger.exception("Failed to write bot_state.json (uptime won't show on dashboard).")


def get_rates(symbol, timeframe, n=300, retries=3, retry_delay=2.0):
    for attempt in range(retries):
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df
        if attempt < retries - 1:
            logger.warning(
                f"No data for {symbol} on timeframe {timeframe} "
                f"(attempt {attempt + 1}/{retries}), retrying in {retry_delay}s..."
            )
            time.sleep(retry_delay)
    raise RuntimeError(f"No data for {symbol} on timeframe {timeframe} after {retries} attempts")


# ----------------------------- INDICATORS -------------------------------
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ----------------------------- SWING / FIBONACCI -------------------------
def find_last_swing(df, lookback=SWING_LOOKBACK):
    """Naive swing detection: highest high / lowest low in the lookback window,
    using whichever comes first to define leg direction."""
    window = df.tail(lookback).reset_index(drop=True)
    hi_idx = window["high"].idxmax()
    lo_idx = window["low"].idxmin()
    swing_high = window["high"].iloc[hi_idx]
    swing_low = window["low"].iloc[lo_idx]
    leg_up = lo_idx < hi_idx  # low formed before high => up-leg
    return swing_low, swing_high, leg_up


def fib_levels(swing_low, swing_high):
    diff = swing_high - swing_low
    return {
        "0.0": swing_high,
        "50.0": swing_high - 0.5 * diff,
        "61.8": swing_high - 0.618 * diff,
        "78.6": swing_high - 0.786 * diff,
        "100.0": swing_low,
        "ext_127": swing_high + 0.27 * diff,
        "ext_161": swing_high + 0.618 * diff,
    }


# ----------------------------- SIGNAL LOGIC -------------------------------
def get_trend_bias():
    df = get_rates(SYMBOL, TF_TREND, 250)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    last = df.iloc[-1]
    if last["close"] > last["ema50"] > last["ema200"]:
        return "long"
    if last["close"] < last["ema50"] < last["ema200"]:
        return "short"
    return None


def get_daily_bias():
    """Day-timeframe trend filter: 10/20/50/100/200 EMA stack + RSI(14).
    Returns "long" (Day uptrend), "short" (Day downtrend), or "neutral"
    (mixed/choppy — blocks both directions). Used as a hard veto in
    check_entry_signal() so the EA never fights the dominant Day trend."""
    df = get_rates(SYMBOL, TF_DAY, 260)
    emas = {p: ema(df["close"], p).iloc[-1] for p in DAILY_EMA_PERIODS}
    df["rsi"] = rsi(df["close"], DAILY_RSI_PERIOD)
    last = df.iloc[-1]
    price = last["close"]
    rsi_val = last["rsi"]
    e50, e200 = emas[50], emas[200]

    bullish = price > e200 and e50 > e200 and rsi_val < DAILY_RSI_OVERBOUGHT
    bearish = price < e200 and e50 < e200 and rsi_val > DAILY_RSI_OVERSOLD

    if DAILY_REQUIRE_FULL_STACK:
        stack = [emas[p] for p in DAILY_EMA_PERIODS]
        bullish = bullish and all(stack[i] > stack[i + 1] for i in range(len(stack) - 1))
        bearish = bearish and all(stack[i] < stack[i + 1] for i in range(len(stack) - 1))

    if bullish and not bearish:
        return "long"
    if bearish and not bullish:
        return "short"
    return "neutral"


def get_group_bias(scores, priority_keys, vote_threshold=None):
    """Logic Groups trend filter (Step 1): cascades through `priority_keys`
    in order. The FIRST strategy in the list whose score clears the vote
    threshold decisively on one side (i.e. that side's score >= threshold
    AND strictly higher than the other side) sets the bias for the whole
    group — strategies further down the list are never even consulted once
    one is decisive. If none of them are decisive (each is neutral, missing,
    benched, or contradicts itself), returns "neutral" and the group takes
    no trade in EITHER direction this scan — same "neutral blocks both
    sides" principle as the existing Daily Filter, just built from a
    custom, per-group priority chain of H1 strategies instead of the D1 EMA
    stack."""
    threshold = strategies.DEFAULT_VOTE_THRESHOLD if vote_threshold is None else vote_threshold
    for key in priority_keys:
        s = scores.get(key)
        if not s or s.get("benched"):
            continue
        long_v, short_v = s.get("long", 0.0), s.get("short", 0.0)
        if long_v >= threshold and long_v > short_v:
            return "long"
        if short_v >= threshold and short_v > long_v:
            return "short"
    return "neutral"


def pick_priority_strategy(scores, candidate_keys, direction, league_state):
    """Logic Groups entry selection (Step 2): among `candidate_keys` that
    voted `direction` this scan (score >= vote threshold, not benched),
    picks the SINGLE one to trade — ranked by League System standing
    (highest auto_weight first, then highest recent win-rate, then highest
    raw score) rather than blending/averaging them like confluence13 does.
    Returns (strategy_key, score) or (None, 0.0) if nobody in the pool
    fired in that direction this scan."""
    fired = [
        k for k in candidate_keys
        if scores.get(k) and not scores[k]["benched"]
        and scores[k].get(direction, 0.0) >= strategies.DEFAULT_VOTE_THRESHOLD
    ]
    if not fired:
        return None, 0.0

    def rank(k):
        aw = league.auto_weight(
            league_state, k, LEAGUE_MIN_WINRATE_PCT,
            LEAGUE_WINRATE_LOOKBACK_TRADES, LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT,
        ) if LEAGUE_ENABLED else 1.0
        wr = league.winrate(league_state, k, LEAGUE_WINRATE_LOOKBACK_TRADES) or 0.0
        return (aw, wr, scores[k][direction])

    fired.sort(key=rank, reverse=True)
    best = fired[0]
    return best, scores[best][direction]


def check_entry_signal():
    bias = get_trend_bias()
    if bias is None:
        return None

    if DAILY_FILTER_ENABLED:
        daily_bias = get_daily_bias()
        if daily_bias == "neutral":
            logger.info("Daily filter: Day trend is neutral/choppy — no trades either side.")
            return None
        if daily_bias != bias:
            logger.info(f"Daily filter veto: H4 bias={bias} conflicts with Day bias={daily_bias}.")
            return None

    df = get_rates(SYMBOL, TF_ENTRY, 300)
    df["rsi"] = rsi(df["close"], 14)
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(df["close"])
    df["atr"] = atr(df, ATR_PERIOD)

    swing_low, swing_high, leg_up = find_last_swing(df)
    fibs = fib_levels(swing_low, swing_high)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    in_fib_zone = fibs["61.8"] <= last["close"] <= fibs["50.0"] if leg_up \
        else fibs["50.0"] <= last["close"] <= fibs["61.8"]

    macd_cross_up = prev["macd_hist"] <= 0 and last["macd_hist"] > 0
    macd_cross_down = prev["macd_hist"] >= 0 and last["macd_hist"] < 0
    rsi_ok_long = 40 <= last["rsi"] <= 60 or (prev["rsi"] < 50 <= last["rsi"])
    rsi_ok_short = 40 <= last["rsi"] <= 60 or (prev["rsi"] > 50 >= last["rsi"])

    signal = None
    if bias == "long" and in_fib_zone and macd_cross_up and rsi_ok_long:
        sl = min(fibs["78.6"], swing_low) - ATR_SL_BUFFER_MULT * last["atr"]
        signal = {
            "direction": "long",
            "strategy": "fib_confluence",
            "entry": last["close"],
            "sl": sl,
            "tp1": last["close"] + (last["close"] - sl),
            "tp2": last["close"] + 2 * (last["close"] - sl),
            "fibs": fibs,
        }
    elif bias == "short" and in_fib_zone and macd_cross_down and rsi_ok_short:
        sl = max(fibs["78.6"], swing_high) + ATR_SL_BUFFER_MULT * last["atr"]
        signal = {
            "direction": "short",
            "strategy": "fib_confluence",
            "entry": last["close"],
            "sl": sl,
            "tp1": last["close"] - (sl - last["close"]),
            "tp2": last["close"] - 2 * (sl - last["close"]),
            "fibs": fibs,
        }

    if signal is None:
        return None

    # --- R:R gate: reject any setup whose TP2:SL reward-to-risk ratio is
    # below MIN_RISK_REWARD_RATIO, no matter how clean the technical setup
    # looked. This is the "R:R must be worth it" rule — gold's volatility
    # means a sub-1:1.5 setup isn't worth taking even with a good win rate.
    if not passes_risk_reward(signal):
        risk = abs(signal["entry"] - signal["sl"])
        reward = abs(signal["tp2"] - signal["entry"])
        rr = (reward / risk) if risk else 0.0
        logger.info(f"R:R gate: setup R:R 1:{rr:.2f} is below "
              f"MIN_RISK_REWARD_RATIO 1:{MIN_RISK_REWARD_RATIO} — skipping.")
        return None

    return signal


# ----------------------------- POSITION SIZING -----------------------------
def passes_risk_reward(signal, min_rr=None):
    """Fixed Fractional MM rule #2: reward must justify the risk. Compares
    TP2 distance to SL distance and requires the ratio to be at least
    min_rr (defaults to MIN_RISK_REWARD_RATIO, e.g. 1.5 = R:R 1:1.5)."""
    if min_rr is None:
        min_rr = MIN_RISK_REWARD_RATIO
    if min_rr is None:
        return True
    risk = abs(signal["entry"] - signal["sl"])
    reward = abs(signal["tp2"] - signal["entry"])
    if risk == 0:
        return False
    return (reward / risk) >= min_rr


def calc_tp_price_from_usd(entry, direction, lot, target_usd,
                           value_per_point=VALUE_PER_POINT_PER_LOT):
    """Price level that yields target_usd profit for lot lots, given the
    account's $/point/lot conversion. Returns None if lot or value_per_point
    is 0 (caller falls back to strategy TP)."""
    if lot <= 0 or value_per_point <= 0:
        return None
    distance = target_usd / (lot * value_per_point)
    return entry + distance if direction == "long" else entry - distance


def calc_lot_size(balance, risk_pct, entry, sl, value_per_point=VALUE_PER_POINT_PER_LOT,
                   lot_step=LOT_STEP, min_lot=MIN_LOT, max_lot=MAX_LOT):
    """Risk-based lot size, then clamped to [min_lot, max_lot]. Returns 0.0 if
    the trade should be skipped (stop distance is 0, or the risk-based size
    rounds below min_lot and ENFORCE_MIN_LOT is False)."""
    stop_distance = abs(entry - sl)
    if stop_distance == 0:
        return 0.0
    risk_amount = balance * risk_pct
    raw_lot = risk_amount / (stop_distance * value_per_point)
    lot = np.floor(raw_lot / lot_step) * lot_step
    lot = round(lot, 2)

    if lot < min_lot:
        if not ENFORCE_MIN_LOT:
            return 0.0
        lot = min_lot
    if lot > max_lot:
        lot = max_lot
    return round(lot, 2)


# ----------------------------- MONEY MANAGEMENT -----------------------------
_PEAK_BALANCE = None  # tracked across the running session, updated each loop


def update_peak_balance(equity):
    global _PEAK_BALANCE
    if _PEAK_BALANCE is None or equity > _PEAK_BALANCE:
        _PEAK_BALANCE = equity
    return _PEAK_BALANCE


def check_drawdown_breaker(info):
    """Returns (blocked: bool, reason: str|None). Compares current equity to
    the highest equity seen so far this session. Disabled if MAX_DRAWDOWN_PCT
    is None."""
    if MAX_DRAWDOWN_PCT is None or info is None:
        return False, None
    peak = update_peak_balance(info.equity)
    if peak <= 0:
        return False, None
    drawdown_pct = (peak - info.equity) / peak * 100.0
    if drawdown_pct >= MAX_DRAWDOWN_PCT:
        return True, f"drawdown {drawdown_pct:.2f}% >= MAX_DRAWDOWN_PCT {MAX_DRAWDOWN_PCT}%"
    return False, None


def get_today_realized_pnl():
    """Sum of profit+swap+commission for this EA's (MAGIC_NUMBER) closed
    deals since midnight (local time)."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, datetime.now())
    if not deals:
        return 0.0
    return sum(d.profit + d.swap + d.commission for d in deals if d.magic == MAGIC_NUMBER)


def get_floating_pnl():
    positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC_NUMBER]
    return sum(p.profit for p in positions)


def get_manual_realized_pnl():
    """Sum of closed P&L for manually placed trades (magic != MAGIC_NUMBER)
    recorded in manual_trades.json."""
    state = _load_json(MANUAL_TRADES_PATH, {"open": {}, "closed": []})
    return sum(t.get("profit", 0.0) for t in state.get("closed", []))


def get_manual_floating_pnl():
    """Current floating P&L of open manual positions (magic != MAGIC_NUMBER)."""
    state = _load_json(MANUAL_TRADES_PATH, {"open": {}, "closed": []})
    return sum(t.get("floating_pnl", 0.0) for t in state.get("open", {}).values())


def get_total_pnl():
    """Combined realized + floating P&L across bot trades AND manual trades."""
    return (get_today_realized_pnl() + get_floating_pnl() +
            get_manual_realized_pnl() + get_manual_floating_pnl())


def track_manual_trades():
    """Detect and record manually opened/closed positions (any magic != MAGIC_NUMBER).

    Runs every main loop tick. On each call:
    - Compares currently open non-bot positions against the last known state.
    - New tickets: records entry details and logs the open.
    - Disappeared tickets: looks up the exit deal, records close + profit, logs it.
    - Updates floating P&L for positions that are still open.
    - Saves everything to manual_trades.json so the dashboard can show it.
    """
    state = _load_json(MANUAL_TRADES_PATH, {"open": {}, "closed": []})
    known_open = state.get("open", {})
    closed_list = state.get("closed", [])

    all_positions = mt5.positions_get() or []
    manual_positions = {str(p.ticket): p for p in all_positions if p.magic != MAGIC_NUMBER}

    now_iso = datetime.now().isoformat()

    # --- Detect newly opened manual positions ---
    for ticket_str, pos in manual_positions.items():
        if ticket_str not in known_open:
            direction = "LONG" if pos.type == mt5.POSITION_TYPE_BUY else "SHORT"
            known_open[ticket_str] = {
                "ticket":    pos.ticket,
                "symbol":    pos.symbol,
                "type":      direction,
                "volume":    pos.volume,
                "entry":     pos.price_open,
                "sl":        pos.sl,
                "tp":        pos.tp,
                "magic":     pos.magic,
                "opened_at": now_iso,
                "floating_pnl": pos.profit,
            }
            logger.info(
                f"[Manual Trade] OPENED  ticket={pos.ticket} {direction} "
                f"{pos.volume} lot @ {pos.price_open:.2f}  SL={pos.sl:.2f}  TP={pos.tp:.2f}"
            )
        else:
            # Update floating P&L for existing open manual position
            known_open[ticket_str]["floating_pnl"] = pos.profit
            known_open[ticket_str]["sl"] = pos.sl
            known_open[ticket_str]["tp"] = pos.tp

    # --- Detect manually closed positions (were known, now gone) ---
    for ticket_str in list(known_open.keys()):
        if ticket_str not in manual_positions:
            rec = known_open.pop(ticket_str)
            # Try to find the closing deal in recent history
            start = datetime.now() - timedelta(days=1)
            deals = mt5.history_deals_get(start, datetime.now()) or []
            exit_deal = next(
                (d for d in deals
                 if d.position_id == rec["ticket"] and d.entry == mt5.DEAL_ENTRY_OUT),
                None
            )
            close_price = exit_deal.price  if exit_deal else 0.0
            profit      = exit_deal.profit if exit_deal else rec.get("floating_pnl", 0.0)
            swap        = exit_deal.swap   if exit_deal else 0.0
            commission  = exit_deal.commission if exit_deal else 0.0
            net_profit  = profit + swap + commission

            closed_record = {
                "ticket":      rec["ticket"],
                "symbol":      rec["symbol"],
                "type":        rec["type"],
                "volume":      rec["volume"],
                "entry":       rec["entry"],
                "close_price": close_price,
                "profit":      net_profit,
                "opened_at":   rec["opened_at"],
                "closed_at":   now_iso,
            }
            closed_list.append(closed_record)
            result_label = "WIN" if net_profit > 0 else "LOSS"
            logger.info(
                f"[Manual Trade] CLOSED  ticket={rec['ticket']} {rec['type']} "
                f"{rec['volume']} lot  entry={rec['entry']:.2f}  close={close_price:.2f}  "
                f"P&L={net_profit:+.2f}  [{result_label}]"
            )

    state["open"]   = known_open
    state["closed"] = closed_list
    _save_json(MANUAL_TRADES_PATH, state)


def count_today_new_trades():
    """Counts this EA's opening deals (DEAL_ENTRY_IN) since midnight — used
    for MAX_DAILY_TRADES."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, datetime.now())
    if not deals:
        return 0
    return sum(1 for d in deals if d.magic == MAGIC_NUMBER and d.entry == mt5.DEAL_ENTRY_IN)


def check_daily_loss_limit(balance):
    """Returns (blocked: bool, reason: str|None). Blocks new entries once
    today's realized + floating P&L for this EA breaches
    -(DAILY_LOSS_LIMIT_R * RISK_PER_TRADE * balance)."""
    if DAILY_LOSS_LIMIT_R is None:
        return False, None
    risk_amount = balance * RISK_PER_TRADE
    limit = -(DAILY_LOSS_LIMIT_R * risk_amount)
    total = get_today_realized_pnl() + get_floating_pnl()
    if total <= limit:
        return True, f"today's P&L {total:.2f} breached daily loss limit {limit:.2f} ({DAILY_LOSS_LIMIT_R}R)"
    return False, None


def get_today_closed_deals_ordered():
    """This EA's closing deals (DEAL_ENTRY_OUT) since midnight, oldest first."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, datetime.now())
    if not deals:
        return []
    out_deals = [d for d in deals if d.magic == MAGIC_NUMBER and d.entry == mt5.DEAL_ENTRY_OUT]
    return sorted(out_deals, key=lambda d: d.time)


def count_consecutive_losses():
    """Walks today's closed deals backward from most recent and counts how
    many losing trades in a row have just closed (streak breaks on the
    first winner/breakeven hit)."""
    deals = get_today_closed_deals_ordered()
    streak = 0
    for d in reversed(deals):
        pnl = d.profit + d.swap + d.commission
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def is_within_trading_hours(now=None):
    """Returns (in_session: bool, matched_session_key: str|None). Checks the
    current local clock time against every session in ALLOWED_SESSIONS (see
    TRADING_SESSIONS for the 3 Thai-time gold windows, plus "all_day"). If
    TRADING_HOURS_FILTER_ENABLED is False, always returns (True, None) —
    no time restriction. If "all_day" is one of the checked sessions in the
    UI, it short-circuits to always-allowed (00:00-23:59) regardless of
    whatever other session boxes are also ticked — same effect as turning
    the whole filter off, but selectable per-session from the UI so the user
    doesn't have to touch the master "enabled" toggle."""
    if not TRADING_HOURS_FILTER_ENABLED:
        return True, None
    if not ALLOWED_SESSIONS:
        return False, None
    if "all_day" in ALLOWED_SESSIONS:
        return True, "all_day"
    now = now or datetime.now()
    t = now.time()
    for key in ALLOWED_SESSIONS:
        sess = TRADING_SESSIONS.get(key)
        if not sess:
            continue
        start_t = dtime(*sess["start"])
        end_t = dtime(*sess["end"])
        if start_t <= t <= end_t:
            return True, key
    return False, None


# Signal-alert de-duplication: prevents the same pending setup from sending
# ~17 identical Telegram messages across repeated scans. Matches the
# one-time-per-transition pattern used by check_market_hours_and_notify() etc.
# Fingerprint = (strategy_key, direction, round(entry,1)). Re-alerts after
# _SIGNAL_ALERT_RENOTIFY_SECS even if the same setup is still pending (so a
# long-lived signal isn't silently dropped for hours).
_SIGNAL_ALERT_RENOTIFY_SECS = 3600   # re-alert every 1h if signal persists unfilled
_LAST_SIGNAL_ALERT_KEY: "dict[str, tuple | None]" = {
    "confluence13": None, "day_trade": None, "scalping_trade": None, "legacy": None,
}
_LAST_SIGNAL_ALERT_TS: "dict[str, float]" = {
    "confluence13": 0.0, "day_trade": 0.0, "scalping_trade": 0.0, "legacy": 0.0,
}


def _should_send_signal_alert(group, signal):
    """Returns True (and updates the cache) only if this signal is genuinely
    new for this group, OR the same signal has been pending for > 1h."""
    key = _group_key(group)
    strategy = signal.get("strategy", "")
    direction = signal.get("direction", "")
    entry = round(float(signal.get("entry") or 0), 1)
    fingerprint = (strategy, direction, entry)
    now = time.time()
    if (fingerprint != _LAST_SIGNAL_ALERT_KEY.get(key)
            or (now - _LAST_SIGNAL_ALERT_TS.get(key, 0)) >= _SIGNAL_ALERT_RENOTIFY_SECS):
        _LAST_SIGNAL_ALERT_KEY[key] = fingerprint
        _LAST_SIGNAL_ALERT_TS[key] = now
        return True
    return False


_LAST_MARKET_OPEN_STATE = None  # None = unknown yet; True/False after first check
# Timezone-independent tick freshness tracking: store the broker's tick
# timestamp and the LOCAL machine time when we first saw it. MT5 tick .time
# uses broker server time (e.g. UTC+3 for XM), not local machine time, so
# comparing tick.time directly against time.time() gives a wrong offset.
# Instead, we track when a NEW tick arrives by local clock — no offset needed.
_last_seen_tick_broker_ts = None   # tick.time value of the last seen tick
_last_new_tick_local_ts   = None   # time.time() when that tick first appeared


def is_market_open(max_tick_age_sec=None):
    """Broker ground-truth check: can SYMBOL actually be traded right now?
    Weekends, broker holidays, and a stalled/disconnected feed all show up
    here, independent of is_within_trading_hours()'s user-preference window.
    Returns (open: bool, reason: str).

    Two independent checks — either one can mark the market closed:
      1. mt5.symbol_info(SYMBOL).trade_mode = SYMBOL_TRADE_MODE_DISABLED
         (broker's own flag for holidays/maintenance).
      2. Tick freshness — measured by how long it has been since a NEW tick
         arrived, using the local machine clock (avoids the broker timezone
         offset that makes tick.time and time.time() disagree by ~hours)."""
    global _last_seen_tick_broker_ts, _last_new_tick_local_ts
    if not MARKET_HOURS_CHECK_ENABLED:
        return True, "market-hours check disabled"
    max_age = max_tick_age_sec if max_tick_age_sec is not None else MARKET_CLOSED_MAX_TICK_AGE_SEC
    sym_info = mt5.symbol_info(SYMBOL)
    if sym_info is None:
        return False, f"symbol_info({SYMBOL}) unavailable -- not subscribed / not found"
    if sym_info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return False, f"broker reports {SYMBOL} trading disabled (holiday/maintenance)"
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None or not tick.time:
        return False, f"no tick data for {SYMBOL} -- feed disconnected or market closed"
    now_local = time.time()
    if tick.time != _last_seen_tick_broker_ts:
        _last_seen_tick_broker_ts = tick.time
        _last_new_tick_local_ts   = now_local
    if _last_new_tick_local_ts is None:
        _last_new_tick_local_ts = now_local
    tick_age = now_local - _last_new_tick_local_ts
    if tick_age > max_age:
        return False, (f"no new ticks for {tick_age:.0f}s "
                       f"(> {max_age}s threshold) -- market closed or feed stale")
    return True, f"market open (last new tick {tick_age:.0f}s ago)"


def check_market_hours_and_notify():
    """Wraps is_market_open() with a one-time-per-transition Telegram alert
    and writes market_state.json for the dashboard. Call once per main-loop
    iteration before the new-entry scan. Returns (open, reason)."""
    global _LAST_MARKET_OPEN_STATE
    is_open, reason = is_market_open()
    if MARKET_CLOSED_NOTIFY and TELEGRAM_ENABLED and _LAST_MARKET_OPEN_STATE is not None:
        if _LAST_MARKET_OPEN_STATE and not is_open:
            try:
                send_telegram(telegram_alert.format_market_closed_alert(SYMBOL, reason))
            except Exception:
                logger.exception("Failed to send market-closed Telegram alert.")
        elif not _LAST_MARKET_OPEN_STATE and is_open:
            try:
                send_telegram(telegram_alert.format_market_reopened_alert(SYMBOL, reason))
            except Exception:
                logger.exception("Failed to send market-reopened Telegram alert.")
    if is_open != _LAST_MARKET_OPEN_STATE:
        logger.info(f"Market status -> {'OPEN' if is_open else 'CLOSED'} ({reason})")
    _LAST_MARKET_OPEN_STATE = is_open
    try:
        _save_json(MARKET_STATE_PATH, {
            "timestamp": datetime.now().isoformat(),
            "market_open": is_open,
            "market_reason": reason,
            "symbol": SYMBOL,
        })
    except Exception:
        pass
    return is_open, reason


def check_price_sanity():
    """Cross-checks the broker's current tick against a free independent
    reference price (macro_data.fetch_reference_gold_price()). Logs a
    warning if they diverge beyond MARKET_PRICE_SANITY_TOLERANCE_PCT.
    LOGGING/ALERTING only — never overrides the broker price for trading.
    Returns True if passed/skipped, False if both prices available and diverged."""
    if not MARKET_PRICE_SANITY_CHECK_ENABLED:
        return True
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return True
    ref = macro_data.fetch_reference_gold_price()
    if ref is None or not ref.get("price"):
        return True
    broker_mid = (tick.bid + tick.ask) / 2.0
    diff_pct = abs(broker_mid - ref["price"]) / ref["price"] * 100.0
    if diff_pct > MARKET_PRICE_SANITY_TOLERANCE_PCT:
        logger.warning(
            f"Price sanity: broker mid {broker_mid:.2f} vs reference "
            f"{ref['price']:.2f} ({ref['source']}) diverge {diff_pct:.2f}% "
            f"(> {MARKET_PRICE_SANITY_TOLERANCE_PCT}% tolerance) -- feed may be stale."
        )
        return False
    return True


def check_consecutive_loss_breaker():
    """Anti-Martingale circuit breaker (returns (blocked, reason)). Stops ALL
    new entries for the rest of the calendar day once MAX_CONSECUTIVE_LOSSES
    losing trades have closed in a row — this is the "stop and reassess,
    don't revenge-trade" rule. Disabled if MAX_CONSECUTIVE_LOSSES is None."""
    if MAX_CONSECUTIVE_LOSSES is None:
        return False, None
    streak = count_consecutive_losses()
    if streak >= MAX_CONSECUTIVE_LOSSES:
        return True, (f"{streak} consecutive losing trades today >= "
                       f"MAX_CONSECUTIVE_LOSSES {MAX_CONSECUTIVE_LOSSES} — stop and reassess")
    return False, None


def _group_key(group_label):
    """Normalises a group label to the key used in per-group dicts.
    'Day Trade' -> 'day_trade', 'Scalping Trade' -> 'scalping_trade', etc."""
    return group_label.lower().replace(" ", "_")


def check_trade_interval(group="confluence13"):
    """Enforces a per-group minimum time gap between consecutive new orders.
    Day Trade and Scalping Trade have independent cooldowns so a scalping fill
    no longer blocks day-trade entries and vice versa.
    Returns (blocked: bool, reason: str | None)."""
    key = _group_key(group)
    interval = (MIN_TRADE_INTERVAL_MINUTES_SCALPING
                if key == "scalping_trade" else MIN_TRADE_INTERVAL_MINUTES)
    last_time = _LAST_ORDER_TIME_BY_GROUP.get(key)
    if interval <= 0 or last_time is None:
        return False, None
    elapsed = (datetime.now() - last_time).total_seconds() / 60.0
    if elapsed < interval:
        remaining = interval - elapsed
        return True, (f"trade interval: {elapsed:.1f}min since last {group} order "
                      f"(min {interval:.0f}min) — wait {remaining:.1f}min more")
    return False, None


def _record_order_time(group="confluence13"):
    """Records the time of a just-placed order for the given group's cooldown."""
    _LAST_ORDER_TIME_BY_GROUP[_group_key(group)] = datetime.now()


# ----------------------------- CONFLUENCE MULTI-STRATEGY ENGINE (20 strategies) ----------------
def get_dom_snapshot(symbol):
    """Subscribes to a real MT5 Depth-of-Market (Level2 order book) feed for
    `symbol`, reads one snapshot, then unsubscribes. Returns None — never
    raises — if MT5 isn't connected, or if the broker/symbol doesn't expose
    DOM at all (many don't; this is normal, not a bug). Callers (specifically
    strategies.score_order_flow_dom() and the DOM-confirmation bonus inside
    score_order_block()) must already treat None/empty gracefully so the
    rest of the confluence scan is completely unaffected either way."""
    if mt5 is None:
        return None
    try:
        if not mt5.market_book_add(symbol):
            return None
        book = mt5.market_book_get(symbol)
    except Exception:
        book = None
    finally:
        try:
            mt5.market_book_release(symbol)
        except Exception:
            pass

    if not book:
        return None

    buy_types = {getattr(mt5, "BOOK_TYPE_BUY", 1), getattr(mt5, "BOOK_TYPE_BUY_MARKET", 3)}
    bids, asks = [], []
    for item in book:
        try:
            price, volume = float(item.price), float(item.volume)
        except (AttributeError, TypeError, ValueError):
            continue
        if item.type in buy_types:
            bids.append((price, volume))
        else:
            asks.append((price, volume))

    if not bids and not asks:
        return None

    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {
        "bids": bids,
        "asks": asks,
        "bid_volume": sum(v for _, v in bids),
        "ask_volume": sum(v for _, v in asks),
        "best_bid": bids[0][0] if bids else None,
        "best_ask": asks[0][0] if asks else None,
    }


def get_macro_snapshot_safe():
    """Wraps macro_data.get_macro_snapshot() so a macro-data problem can
    never break a scan. macro_data.py already caches each source to disk
    with a TTL tuned to its real update cadence (COT weekly, calendar/yield/
    DXY/COMEX every few hours) — calling this every scan is cheap, it's
    almost always just a local JSON read, not a network call. On any
    unexpected error this returns None and score_macro_bias() treats that
    exactly like DOM-unsupported: a graceful 0/0, never an exception."""
    try:
        metal = symbol_normalize.canonical_commodity(SYMBOL)
        mfb_symbol = symbol_normalize.canonical_display(SYMBOL) if metal == "GOLD" else "XAGUSD"
        return macro_data.get_macro_snapshot(
            metal,
            myfxbook_email=(MYFXBOOK_EMAIL if MYFXBOOK_ENABLED else None),
            myfxbook_password=(MYFXBOOK_PASSWORD if MYFXBOOK_ENABLED else None),
            myfxbook_symbol=mfb_symbol,
        )
    except Exception:
        logger.exception("macro_data.get_macro_snapshot() failed — macro_bias strategy will score 0/0 this scan.")
        return None


def get_fib_confluence_safe(data):
    """Wraps fib_confluence.compute_confluence() so a Fibonacci computation
    error can never break a scan. Returns None on any unexpected error and
    score_fib_confluence_sr() treats that as a graceful 0/0, exactly like
    score_macro_bias() does when data["macro"] is None."""
    try:
        return fib_confluence.compute_confluence(data)
    except Exception:
        logger.exception("fib_confluence.compute_confluence() failed — fib_confluence_sr will score 0/0 this scan.")
        return None


def get_harmonic_patterns_safe(data):
    """Wraps harmonic_patterns.compute_harmonic_patterns() so a problem in
    the XABCD harmonic-pattern engine can never break a scan -- same
    try/except pattern as get_fib_confluence_safe() above. Must run AFTER
    data["fib_confluence"] has already been set (see build_market_data()
    below) because compute_harmonic_patterns() cross-checks each pattern's
    PRZ against the fib_confluence snapshot. On any error this returns None
    and score_harmonic_patterns() treats that exactly like
    macro_bias/fib_confluence-unsupported: a graceful 0/0."""
    try:
        return harmonic_patterns.compute_harmonic_patterns(data)
    except Exception:
        logger.exception("harmonic_patterns.compute_harmonic_patterns() failed — harmonic_patterns strategy will score 0/0 this scan.")
        return None


def _mtr_is_danger(data):
    """MTR-inspired danger gate: blocks new entries when H1 ATR is spiking
    (> 1.5× 50-bar baseline). Runs from already-fetched market data so there
    is no extra MT5 call. Returns (danger: bool, reason: str)."""
    try:
        import pandas as pd
        h1 = data.get("h1")
        if h1 is None or len(h1) < 55:
            return False, ""
        atr_now = float(h1["atr14"].iloc[-1])
        atr_vals = h1["atr14"].dropna()
        if len(atr_vals) < 52:
            return False, ""
        atr_baseline = float(atr_vals.iloc[-51:-1].mean())
        if atr_baseline <= 0:
            return False, ""
        shock = atr_now / atr_baseline
        if shock > 1.5:
            return True, f"MTR danger: ATR {shock:.2f}x baseline — volatility spike, skipping new entries"
        return False, ""
    except Exception:
        return False, ""


def build_market_data():
    """Fetches D1/H4/H1/M15/M5/M1 OHLC once and enriches each with the common
    indicator columns the strategy functions expect, plus one live DOM
    (Depth of Market) snapshot for the Order Flow strategy and one Macro Bias
    (Big Data) snapshot for the macro_bias strategy. Shared across all
    24 strategies so a scan only costs 6 mt5.copy_rates_from_pos() calls + 1
    DOM read + 1 (mostly-cached) macro read, not one per strategy.
    M5/M1 were added for the 4 scalping strategies (#21-24) — they cost two
    extra calls per scan but use small bar counts (M5/M1 churn fast, so a
    deep history isn't needed the way it is for D1/H4)."""
    df_d1 = strategies.enrich(get_rates(SYMBOL, TF_DAY, 260))
    df_h4 = strategies.enrich(get_rates(SYMBOL, TF_TREND, 250))
    df_h1 = strategies.enrich(get_rates(SYMBOL, TF_ENTRY, 300))
    df_m15 = strategies.enrich(get_rates(SYMBOL, TF_M15, 200))
    df_m5 = strategies.enrich(get_rates(SYMBOL, TF_M5, 200))
    df_m1 = strategies.enrich(get_rates(SYMBOL, TF_M1, 200))
    dom = get_dom_snapshot(SYMBOL)
    macro = get_macro_snapshot_safe()
    data = {"d1": df_d1, "h4": df_h4, "h1": df_h1, "m15": df_m15, "m5": df_m5, "m1": df_m1,
            "now": datetime.now(), "dom": dom, "macro": macro,
            "myfxbook_contrarian": MYFXBOOK_CONTRARIAN}
    data["fib_confluence"] = get_fib_confluence_safe(data)
    data["harmonic"] = get_harmonic_patterns_safe(data)
    return data


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def save_scores_snapshot(scan_result, direction_taken=None, macro=None, logic_groups=None):
    """Writes the latest confluence scan to SCORES_SNAPSHOT_PATH so
    generate_dashboard.py can show per-strategy scores without needing a
    live MT5 connection of its own. `macro` (data["macro"] from
    build_market_data(), see macro_data.py) is included raw so the dashboard
    can render the 6-point institutional checklist, not just the
    macro_bias strategy's single combined score/note. `logic_groups` (only
    populated when ENTRY_MODE == "logic_groups") is a list of per-group
    status dicts — bias, the candidate strategy/score it would trade, and
    whether the Daily Filter is stacked on top — so the dashboard can show
    live WHY a group did or didn't fire instead of requiring log-reading."""
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "long_combined": scan_result["long_combined"],
        "short_combined": scan_result["short_combined"],
        "long_agreeing": scan_result["long_agreeing"],
        "short_agreeing": scan_result["short_agreeing"],
        "min_strategy_score": MIN_STRATEGY_SCORE,
        "min_agreeing_strategies": MIN_AGREEING_STRATEGIES,
        "direction_taken": direction_taken,
        "scores": scan_result["scores"],
        "league": league.status_snapshot(
            _LEAGUE_STATE,
            min_winrate_pct=LEAGUE_MIN_WINRATE_PCT if LEAGUE_ENABLED else None,
            winrate_lookback_trades=LEAGUE_WINRATE_LOOKBACK_TRADES,
            min_samples=LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT,
        ),
        "macro": macro,
        "logic_groups": logic_groups,
    }
    try:
        _save_json(SCORES_SNAPSHOT_PATH, snapshot)
    except OSError:
        logger.exception("Failed to write strategy_scores.json snapshot.")


def run_confluence_scan():
    """Multi-strategy (31) parallel scan: every strategy in strategies.py scores the
    market 0-100 for long/short; an entry only fires when MULTIPLE
    non-benched strategies agree (confluence), per the locked-in design:
    MIN_AGREEING_STRATEGIES of them must vote the same side AND the
    weighted-average combined score on that side must reach
    MIN_STRATEGY_SCORE. Runs all the same MM/session gates as run_once().

    IMPORTANT: scoring + save_scores_snapshot() always run first, unconditionally,
    so the dashboard reflects live confluence scores 24/7 — including outside the
    configured trading session(s) or while a risk breaker is tripped. The
    session/drawdown/daily-loss/streak gates below only block ACTING on a
    signal (sending an order), never the dashboard's visibility into what the
    strategies are currently seeing. (Previously these gates sat before the
    scan, so the dashboard showed "no data" whenever they ran outside the
    Asia/London/Overlap windows — e.g. right after midnight.)"""
    data = build_market_data()

    # --- ML decision layer: continuous performance-based weight adjustment ---
    # league.auto_weight() reads EVERY recorded result for a strategy — real
    # trade closes AND shadow/simulated closes (updated further below) —
    # and returns a 0.0-1.0 multiplier that smoothly down-weights a strategy
    # the moment its combined win-rate drops under LEAGUE_MIN_WINRATE_PCT,
    # and restores it automatically the instant that win-rate recovers (no
    # fixed cooldown). This runs every scan, independent of whether the
    # strategy is also time-benched by the existing rules below.
    adjusted_weights = dict(STRATEGY_WEIGHTS)
    _auto_weights = {}
    if LEAGUE_ENABLED:
        for k in CONFLUENCE_ENABLED_STRATEGIES:
            aw = league.auto_weight(
                _LEAGUE_STATE, k, LEAGUE_MIN_WINRATE_PCT,
                LEAGUE_WINRATE_LOOKBACK_TRADES, LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT,
            )
            _auto_weights[k] = aw
            adjusted_weights[k] = adjusted_weights.get(k, 1.0) * aw

    def bench_check(key):
        if LEAGUE_ENABLED and league.is_benched(_LEAGUE_STATE, key):
            return True
        # Treat a near-zero auto-weight as a full bench too — otherwise a
        # strategy that's effectively dead could still count toward
        # MIN_AGREEING_STRATEGIES even though its weighted contribution is
        # ~0 (weight only scales magnitude, it doesn't remove a strategy
        # from the agreeing-count on its own).
        if LEAGUE_ENABLED and _auto_weights.get(key, 1.0) <= LEAGUE_AUTO_DISABLE_WEIGHT_FLOOR:
            return True
        return False

    result = strategies.score_all(
        data,
        enabled_keys=CONFLUENCE_ENABLED_STRATEGIES,
        weights=adjusted_weights,
        bench_check=bench_check,
    )

    # --- ML decision layer: continuous shadow/paper-trade simulation --------
    # Runs unconditionally every scan (same as scoring above) so every
    # strategy keeps accumulating fresh win/loss history even while it's
    # down-weighted/benched and not taking real trades — this is what lets
    # auto_weight() above recover automatically once simulated performance
    # climbs back over LEAGUE_MIN_WINRATE_PCT. Pure paper trading: no real
    # orders are placed, zero real risk.
    if SHADOW_SIMULATION_ENABLED:
        try:
            tick = mt5.symbol_info_tick(SYMBOL)
            atr_now = data["h1"]["atr14"].iloc[-1]
            if tick is not None and pd.notna(atr_now) and atr_now > 0:
                shadow_closed = strategy_simulator.update_all(
                    _SHADOW_STATE, _LEAGUE_STATE, result["scores"],
                    bid=tick.bid, ask=tick.ask, atr_now=float(atr_now),
                    sl_atr_mult=CONFLUENCE_SL_ATR_MULT, tp_rr=CONFLUENCE_TP_RR,
                    vote_threshold=strategies.DEFAULT_VOTE_THRESHOLD,
                )
                for key, won, reason in shadow_closed:
                    logger.info(f"Shadow simulation '{key}': {'WIN' if won else 'LOSS'} "
                                f"({reason}; virtual paper trade, no real risk).")
                strategy_simulator.save_state(_SHADOW_STATE)
                if LEAGUE_ENABLED:
                    league.save_state(_LEAGUE_STATE)
        except Exception:
            logger.exception("Shadow simulation step failed (non-fatal — real trading continues).")

    long_ok = (result["long_combined"] >= MIN_STRATEGY_SCORE
               and result["long_agreeing"] >= MIN_AGREEING_STRATEGIES)
    short_ok = (result["short_combined"] >= MIN_STRATEGY_SCORE
                and result["short_agreeing"] >= MIN_AGREEING_STRATEGIES)

    direction = None
    if long_ok and short_ok:
        direction = "long" if result["long_combined"] >= result["short_combined"] else "short"
    elif long_ok:
        direction = "long"
    elif short_ok:
        direction = "short"

    save_scores_snapshot(result, direction_taken=direction, macro=data.get("macro"))

    if direction is None:
        logger.debug(f"Confluence scan: no side reached the threshold "
                     f"(long {result['long_combined']}/{result['long_agreeing']} agreeing, "
                     f"short {result['short_combined']}/{result['short_agreeing']} agreeing; "
                     f"need >= {MIN_STRATEGY_SCORE} with >= {MIN_AGREEING_STRATEGIES} agreeing).")
        return

    # ---- gates below block ACTING on the signal (order execution), not the
    # scoring/snapshot above, which already ran ----
    danger, danger_reason = _mtr_is_danger(data)
    if danger:
        logger.debug(f"Confluence scan: {danger_reason}")
        return

    in_session, matched = is_within_trading_hours()
    if not in_session:
        logger.debug("Confluence scan: outside selected trading session(s) — no new entries.")
        return

    info = mt5.account_info()

    dd_blocked, dd_reason = check_drawdown_breaker(info)
    if dd_blocked:
        logger.warning(f"Drawdown breaker: {dd_reason} — no new entries until equity recovers.")
        return

    loss_blocked, loss_reason = check_daily_loss_limit(info.balance)
    if loss_blocked:
        logger.warning(f"Daily loss limit: {loss_reason} — no new entries until tomorrow.")
        return

    if MAX_DAILY_TRADES is not None and count_today_new_trades() >= MAX_DAILY_TRADES:
        logger.info(f"Max daily trades ({MAX_DAILY_TRADES}) reached — no new entries until tomorrow.")
        return

    streak_blocked, streak_reason = check_consecutive_loss_breaker()
    if streak_blocked:
        logger.warning(f"Anti-Martingale breaker: {streak_reason}.")
        return

    if DAILY_FILTER_ENABLED:
        daily_bias = get_daily_bias()
        if daily_bias == "neutral":
            logger.info("Daily filter: Day trend is neutral/choppy — confluence signal vetoed.")
            return
        if daily_bias != direction:
            logger.info(f"Daily filter veto: confluence direction={direction} conflicts with Day bias={daily_bias}.")
            return

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        logger.warning("Confluence scan: no tick data — skipping.")
        return
    entry = tick.ask if direction == "long" else tick.bid
    atr_now = data["h1"]["atr14"].iloc[-1]
    if pd.isna(atr_now) or atr_now <= 0:
        logger.warning("Confluence scan: ATR unavailable — skipping.")
        return

    sl_distance = atr_now * CONFLUENCE_SL_ATR_MULT
    if direction == "long":
        sl = entry - sl_distance
        tp1 = entry + sl_distance
        tp2 = entry + sl_distance * CONFLUENCE_TP_RR
    else:
        sl = entry + sl_distance
        tp1 = entry - sl_distance
        tp2 = entry - sl_distance * CONFLUENCE_TP_RR

    side_key = "long" if direction == "long" else "short"
    contributing = sorted(
        [k for k, v in result["scores"].items()
         if not v["benched"] and v[side_key] >= strategies.DEFAULT_VOTE_THRESHOLD],
        key=lambda k: -result["scores"][k][side_key]
    )

    signal = {
        "direction": direction,
        "strategy": "confluence13",
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "contributing": contributing,
        "combined_score": result[f"{side_key}_combined"],
        "agreeing": result[f"{side_key}_agreeing"],
    }

    if not passes_risk_reward(signal):
        logger.info("Confluence scan: setup failed the R:R floor — skipping.")
        return

    contributing_str = ", ".join(f"{k}={result['scores'][k][side_key]:.0f}%" for k in contributing)

    # (2) Signal-found notification — fires as soon as a setup clears the
    # confluence + R:R gates, independent of whether an order actually gets
    # placed afterwards (AUTO_TRADE off, max concurrent trades, lot rounds
    # to 0, etc. can all still block the order below).
    if NOTIFY_SIGNAL and _should_send_signal_alert("confluence13", signal):
        send_telegram(telegram_alert.format_signal_alert(signal, contributing_str, symbol=SYMBOL))

    lot = calc_lot_size(info.balance, RISK_PER_TRADE, signal["entry"], signal["sl"])

    logger.info(f"Confluence signal: {direction.upper()} | combined score "
                f"{signal['combined_score']:.1f}% ({signal['agreeing']} strategies agreeing) | "
                f"Entry {entry:.2f} SL {sl:.2f} TP2 {tp2:.2f} | Lot {lot} | "
                f"Contributing: {contributing_str}")

    if lot <= 0:
        logger.warning(f"Confluence scan: risk-based lot size rounded to 0 — skipping.")
        return
    if open_positions_count() >= MAX_CONCURRENT_TRADES:
        logger.info("Confluence scan: max concurrent trades reached — skipping.")
        return

    interval_blocked, interval_reason = check_trade_interval("confluence13")
    if interval_blocked:
        logger.info(f"Confluence scan: {interval_reason} — skipping.")
        return

    if not AUTO_TRADE:
        logger.info("AUTO_TRADE is False — confluence signal only, no order sent.")
        return

    before_tickets = {p.ticket for p in (mt5.positions_get(symbol=SYMBOL) or [])}
    _apply_order_options(signal, lot)
    order_result = send_order(signal, lot)
    retcode = getattr(order_result, "retcode", None)
    if retcode != mt5.TRADE_RETCODE_DONE:
        return

    _record_order_time("confluence13")

    # Find the new position ticket so we can attribute league results later.
    after_positions = mt5.positions_get(symbol=SYMBOL) or []
    new_positions = [p for p in after_positions if p.ticket not in before_tickets and p.magic == MAGIC_NUMBER]
    new_ticket = new_positions[0].ticket if new_positions else getattr(order_result, "order", None)

    if new_ticket:
        entry_meta = _load_json(ENTRY_META_PATH, {})
        entry_meta[str(new_ticket)] = {
            "strategies": contributing,
            "direction": direction,
            "strategy": "confluence13",
            "entry_price": entry,
            "lot": lot,
            "risk_distance": sl_distance,
            "opened_at": datetime.now().isoformat(),
        }
        _save_json(ENTRY_META_PATH, entry_meta)

    # (3) Order-open notification — fires once an order is confirmed placed.
    if NOTIFY_ORDER_OPEN:
        msg = telegram_alert.format_order_alert(signal, lot, contributing_str, account_info=info, symbol=SYMBOL)
        send_telegram(msg)


def _build_group_signal(group_label, bias_priority, strategy_pool, scores):
    """Runs both steps of a Logic Group's decision for one scan: Step 1
    trend filter (get_group_bias) then Step 2 entry selection
    (pick_priority_strategy). Returns a small signal dict or None."""
    bias = get_group_bias(scores, bias_priority)
    if bias == "neutral":
        return None
    best_key, best_score = pick_priority_strategy(scores, strategy_pool, bias, _LEAGUE_STATE)
    if best_key is None:
        return None
    return {"group": group_label, "direction": bias, "strategy": best_key, "score": best_score}


def run_logic_groups_scan():
    """Alternative entry engine to run_confluence_scan(), selected via
    ENTRY_MODE == "logic_groups". Splits the strategy set into two
    purpose-built groups — "Day Trade" and "Scalping Trade" — each with its
    OWN priority-cascaded trend filter (see DAY_TRADE_BIAS_PRIORITY /
    SCALP_BIAS_PRIORITY) and its OWN entry-strategy pool (DAY_TRADE_STRATEGIES
    / SCALP_STRATEGIES). LOGIC_GROUP_SELECTION picks which group(s) run.

    Unlike confluence13's weighted-average blend, when multiple strategies in
    a group's pool fire the same scan, the single best one is chosen by
    League System standing (auto_weight, then win-rate) — see
    pick_priority_strategy(). If both groups fire in the same scan (possibly
    in opposite directions), the same League-standing rule breaks the tie
    between groups.

    Shares the scoring pass, shadow simulation, and all MM/risk/Daily-Filter
    gates with run_confluence_scan() — intentionally duplicated rather than
    refactored into one shared function, so the existing confluence13 path
    is not put at risk by this addition."""
    data = build_market_data()

    adjusted_weights = dict(STRATEGY_WEIGHTS)
    _auto_weights = {}
    if LEAGUE_ENABLED:
        for k in strategies.STRATEGY_REGISTRY:
            aw = league.auto_weight(
                _LEAGUE_STATE, k, LEAGUE_MIN_WINRATE_PCT,
                LEAGUE_WINRATE_LOOKBACK_TRADES, LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT,
            )
            _auto_weights[k] = aw
            adjusted_weights[k] = adjusted_weights.get(k, 1.0) * aw

    def bench_check(key):
        if LEAGUE_ENABLED and league.is_benched(_LEAGUE_STATE, key):
            return True
        if LEAGUE_ENABLED and _auto_weights.get(key, 1.0) <= LEAGUE_AUTO_DISABLE_WEIGHT_FLOOR:
            return True
        return False

    result = strategies.score_all(
        data,
        enabled_keys=CONFLUENCE_ENABLED_STRATEGIES,
        weights=adjusted_weights,
        bench_check=bench_check,
    )
    scores = result["scores"]

    if SHADOW_SIMULATION_ENABLED:
        try:
            tick = mt5.symbol_info_tick(SYMBOL)
            atr_now = data["h1"]["atr14"].iloc[-1]
            if tick is not None and pd.notna(atr_now) and atr_now > 0:
                shadow_closed = strategy_simulator.update_all(
                    _SHADOW_STATE, _LEAGUE_STATE, scores,
                    bid=tick.bid, ask=tick.ask, atr_now=float(atr_now),
                    sl_atr_mult=CONFLUENCE_SL_ATR_MULT, tp_rr=CONFLUENCE_TP_RR,
                    vote_threshold=strategies.DEFAULT_VOTE_THRESHOLD,
                )
                for key, won, reason in shadow_closed:
                    logger.info(f"Shadow simulation '{key}': {'WIN' if won else 'LOSS'} "
                                f"({reason}; virtual paper trade, no real risk).")
                strategy_simulator.save_state(_SHADOW_STATE)
                if LEAGUE_ENABLED:
                    league.save_state(_LEAGUE_STATE)
        except Exception:
            logger.exception("Shadow simulation step failed (non-fatal — real trading continues).")

    groups = []
    if LOGIC_GROUP_SELECTION in ("day_trade", "both"):
        groups.append(("Day Trade", DAY_TRADE_BIAS_PRIORITY, DAY_TRADE_STRATEGIES))
    if LOGIC_GROUP_SELECTION in ("scalping", "both"):
        groups.append(("Scalping Trade", SCALP_BIAS_PRIORITY, SCALP_STRATEGIES))

    signals = []
    groups_status = []
    for label, bias_priority, pool in groups:
        bias = get_group_bias(scores, bias_priority)
        if bias == "neutral":
            best_key, best_score = None, 0.0
        else:
            best_key, best_score = pick_priority_strategy(scores, pool, bias, _LEAGUE_STATE)
        groups_status.append({
            "group": label,
            "bias": bias,
            "candidate": best_key,
            "candidate_score": best_score,
            "apply_daily_filter": LOGIC_GROUPS_APPLY_DAILY_FILTER,
        })
        if best_key:
            signals.append({"group": label, "direction": bias, "strategy": best_key, "score": best_score})

    save_scores_snapshot(
        result,
        direction_taken=(signals[0]["direction"] if signals else None),
        macro=data.get("macro"),
        logic_groups=groups_status,
    )

    if not signals:
        logger.debug("Logic groups scan: no group's trend filter + entry pool produced a fireable signal.")
        return

    if len(signals) > 1:
        def group_rank(sig):
            k = sig["strategy"]
            aw = league.auto_weight(
                _LEAGUE_STATE, k, LEAGUE_MIN_WINRATE_PCT,
                LEAGUE_WINRATE_LOOKBACK_TRADES, LEAGUE_MIN_SAMPLES_FOR_ADJUSTMENT,
            ) if LEAGUE_ENABLED else 1.0
            wr = league.winrate(_LEAGUE_STATE, k, LEAGUE_WINRATE_LOOKBACK_TRADES) or 0.0
            return (aw, wr, sig["score"])
        signals.sort(key=group_rank, reverse=True)
        fired_summary = ", ".join(f"{s['group']}={s['direction']}" for s in signals)
        logger.info(
            f"Logic groups scan: both groups fired this scan ({fired_summary}) — "
            f"taking {signals[0]['group']} (stronger League standing)."
        )
    chosen = signals[0]

    direction = chosen["direction"]
    strat_key = chosen["strategy"]
    group_label = chosen["group"]

    # ---- shared MM/risk gates (identical to run_confluence_scan) ----
    danger, danger_reason = _mtr_is_danger(data)
    if danger:
        logger.debug(f"Logic groups scan: {danger_reason}")
        return

    in_session, matched = is_within_trading_hours()
    if not in_session:
        logger.debug("Logic groups scan: outside selected trading session(s) — no new entries.")
        return

    info = mt5.account_info()

    dd_blocked, dd_reason = check_drawdown_breaker(info)
    if dd_blocked:
        logger.warning(f"Drawdown breaker: {dd_reason} — no new entries until equity recovers.")
        return

    loss_blocked, loss_reason = check_daily_loss_limit(info.balance)
    if loss_blocked:
        logger.warning(f"Daily loss limit: {loss_reason} — no new entries until tomorrow.")
        return

    if MAX_DAILY_TRADES is not None and count_today_new_trades() >= MAX_DAILY_TRADES:
        logger.info(f"Max daily trades ({MAX_DAILY_TRADES}) reached — no new entries until tomorrow.")
        return

    streak_blocked, streak_reason = check_consecutive_loss_breaker()
    if streak_blocked:
        logger.warning(f"Anti-Martingale breaker: {streak_reason}.")
        return

    # The group's own Step-1 bias cascade already served as this trade's
    # trend filter (see _build_group_signal/get_group_bias). The global D1
    # Daily Filter is a second, independent trend filter and is OFF by
    # default here (LOGIC_GROUPS_APPLY_DAILY_FILTER) to avoid double-gating —
    # see the comment at LOGIC_GROUPS_APPLY_DAILY_FILTER's definition.
    if DAILY_FILTER_ENABLED and LOGIC_GROUPS_APPLY_DAILY_FILTER:
        daily_bias = get_daily_bias()
        if daily_bias == "neutral":
            logger.info(f"Daily filter: Day trend is neutral/choppy — {group_label} signal vetoed.")
            return
        if daily_bias != direction:
            logger.info(f"Daily filter veto: {group_label} direction={direction} conflicts with Day bias={daily_bias}.")
            return

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        logger.warning("Logic groups scan: no tick data — skipping.")
        return
    entry = tick.ask if direction == "long" else tick.bid
    atr_now = data["h1"]["atr14"].iloc[-1]
    if pd.isna(atr_now) or atr_now <= 0:
        logger.warning("Logic groups scan: ATR unavailable — skipping.")
        return

    sl_distance = atr_now * CONFLUENCE_SL_ATR_MULT
    if direction == "long":
        sl = entry - sl_distance
        tp1 = entry + sl_distance
        tp2 = entry + sl_distance * CONFLUENCE_TP_RR
    else:
        sl = entry + sl_distance
        tp1 = entry - sl_distance
        tp2 = entry - sl_distance * CONFLUENCE_TP_RR

    signal = {
        "direction": direction,
        "strategy": f"{group_label}:{strat_key}",
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "contributing": [strat_key],
        "combined_score": chosen["score"],
        "agreeing": 1,
    }

    if not passes_risk_reward(signal):
        logger.info(f"{group_label} scan: setup failed the R:R floor — skipping.")
        return

    contributing_str = f"{strat_key}={chosen['score']:.0f}% (League-prioritized)"

    if NOTIFY_SIGNAL and _should_send_signal_alert(group_label, signal):
        send_telegram(telegram_alert.format_signal_alert(signal, contributing_str, symbol=SYMBOL))

    lot = calc_lot_size(info.balance, RISK_PER_TRADE, signal["entry"], signal["sl"])

    logger.info(f"{group_label} signal: {direction.upper()} via '{strat_key}' "
                f"(score {chosen['score']:.1f}%) | Entry {entry:.2f} SL {sl:.2f} TP2 {tp2:.2f} | Lot {lot}")

    if lot <= 0:
        logger.warning(f"{group_label} scan: risk-based lot size rounded to 0 — skipping.")
        return
    if open_positions_count() >= MAX_CONCURRENT_TRADES:
        logger.info(f"{group_label} scan: max concurrent trades reached — skipping.")
        return

    interval_blocked, interval_reason = check_trade_interval(group_label)
    if interval_blocked:
        logger.info(f"{group_label} scan: {interval_reason} — skipping.")
        return

    if not AUTO_TRADE:
        logger.info(f"AUTO_TRADE is False — {group_label} signal only, no order sent.")
        return

    before_tickets = {p.ticket for p in (mt5.positions_get(symbol=SYMBOL) or [])}
    _apply_order_options(signal, lot)
    order_result = send_order(signal, lot)
    retcode = getattr(order_result, "retcode", None)
    if retcode != mt5.TRADE_RETCODE_DONE:
        return

    _record_order_time(group_label)

    after_positions = mt5.positions_get(symbol=SYMBOL) or []
    new_positions = [p for p in after_positions if p.ticket not in before_tickets and p.magic == MAGIC_NUMBER]
    new_ticket = new_positions[0].ticket if new_positions else getattr(order_result, "order", None)

    if new_ticket:
        entry_meta = _load_json(ENTRY_META_PATH, {})
        entry_meta[str(new_ticket)] = {
            "strategies": [strat_key],
            "direction": direction,
            "strategy": f"{group_label}:{strat_key}",
            "entry_price": entry,
            "lot": lot,
            "risk_distance": sl_distance,
            "opened_at": datetime.now().isoformat(),
        }
        _save_json(ENTRY_META_PATH, entry_meta)

    if NOTIFY_ORDER_OPEN:
        msg = telegram_alert.format_order_alert(signal, lot, contributing_str, account_info=info, symbol=SYMBOL)
        send_telegram(msg)


def update_league_from_closed_trades():
    """Looks for this EA's positions that have closed since the last check,
    matches them back to the strategies that contributed at entry (via
    ENTRY_META_PATH), and feeds the win/loss result into the League System.
    Sends a Telegram alert per closed position if enabled.

    (4) Order-close notification: this fires unconditionally on every closed
    trade (gated only by TELEGRAM_ENABLED/NOTIFY_ORDER_CLOSE), independent of
    LEAGUE_ENABLED — only the League bookkeeping below stays conditional on
    that flag. Previously this whole function (and therefore the close
    alert) returned early when the League System was disabled."""
    processed = set(_load_json(PROCESSED_DEALS_PATH, []))
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(start, datetime.now()) or []
    out_deals = [d for d in deals if d.magic == MAGIC_NUMBER and d.entry == mt5.DEAL_ENTRY_OUT]
    if not out_deals:
        return

    entry_meta = _load_json(ENTRY_META_PATH, {})
    info = mt5.account_info()
    changed = False
    for d in out_deals:
        if d.ticket in processed:
            continue
        position_id = str(getattr(d, "position_id", ""))
        meta = entry_meta.get(position_id)

        pnl = d.profit + d.swap + d.commission
        won = pnl > 0

        if LEAGUE_ENABLED and meta is not None:
            for strat_key in meta.get("strategies", []):
                _, reason = league.record_trade_result(
                    _LEAGUE_STATE, strat_key, won,
                    max_consecutive_losses=LEAGUE_MAX_CONSECUTIVE_LOSSES,
                    min_winrate_pct=LEAGUE_MIN_WINRATE_PCT,
                    winrate_lookback_trades=LEAGUE_WINRATE_LOOKBACK_TRADES,
                    bench_hours=LEAGUE_BENCH_HOURS,
                )
                if reason:
                    logger.warning(f"League System: benching '{strat_key}' for {LEAGUE_BENCH_HOURS}h — {reason}.")

        if NOTIFY_ORDER_CLOSE:
            deal_info = {"pnl": pnl}
            if meta is not None:
                deal_info["direction"] = meta.get("direction")
                deal_info["strategy"] = meta.get("strategy")
                deal_info["entry_price"] = meta.get("entry_price")
                deal_info["close_price"] = d.price
                deal_info["lot"] = meta.get("lot")
                opened_at = meta.get("opened_at")
                if opened_at:
                    try:
                        delta = datetime.now() - datetime.fromisoformat(opened_at)
                        deal_info["duration"] = str(delta).split(".")[0]
                    except ValueError:
                        pass
            msg = telegram_alert.format_close_alert(deal_info, account_info=info, symbol=SYMBOL)
            send_telegram(msg)

        if meta is not None:
            entry_meta.pop(position_id, None)
        processed.add(d.ticket)
        changed = True

    if changed:
        if LEAGUE_ENABLED:
            league.save_state(_LEAGUE_STATE)
        _save_json(ENTRY_META_PATH, entry_meta)
        _save_json(PROCESSED_DEALS_PATH, sorted(processed)[-2000:])


def manage_breakeven():
    """Moves SL to entry (+/- a small buffer) once a position's floating
    profit reaches BREAKEVEN_TRIGGER_R multiples of its ORIGINAL SL distance
    (captured at entry time in ENTRY_META_PATH — falls back to skipping a
    position if that metadata isn't available, e.g. trades opened before
    this feature existed or by the legacy entry mode). Runs independently of
    and before manage_trailing_stops(), and only ever tightens SL toward
    locking in the breakeven level — never loosens it."""
    if not BREAKEVEN_ENABLED:
        return

    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return

    entry_meta = _load_json(ENTRY_META_PATH, {})
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return

    for pos in positions:
        if pos.magic != MAGIC_NUMBER:
            continue
        if pos.sl == 0:
            continue  # opened with use_sl=False — no SL to move to breakeven
        meta = entry_meta.get(str(pos.ticket))
        if not meta or not meta.get("risk_distance"):
            continue

        risk_distance = float(meta["risk_distance"])
        is_long = pos.type == mt5.POSITION_TYPE_BUY
        price = tick.bid if is_long else tick.ask
        profit_distance = (price - pos.price_open) if is_long else (pos.price_open - price)

        trigger_distance = risk_distance * BREAKEVEN_TRIGGER_R
        if profit_distance < trigger_distance:
            continue

        breakeven_sl = (pos.price_open + BREAKEVEN_BUFFER_POINTS) if is_long else (pos.price_open - BREAKEVEN_BUFFER_POINTS)
        already_at_or_past = (pos.sl != 0 and ((is_long and pos.sl >= breakeven_sl) or
                                                (not is_long and pos.sl <= breakeven_sl)))
        if already_at_or_past:
            continue
        if is_long and breakeven_sl >= price:
            continue
        if not is_long and breakeven_sl <= price:
            continue

        modify_sl(pos, breakeven_sl)
        logger.info(f"Breakeven: ticket {pos.ticket} SL moved to {breakeven_sl:.2f} "
                    f"(profit reached {BREAKEVEN_TRIGGER_R}R of original risk).")


# ----------------------------- ORDER EXECUTION ------------------------------
def open_positions_count():
    positions = mt5.positions_get(symbol=SYMBOL)
    return len(positions) if positions else 0


def _apply_order_options(signal, lot):
    """Mutates signal["sl"] and signal["tp2"] in-place according to ORDER_OPTIONS.
    R:R gating (passes_risk_reward) has already run using the original strategy
    SL/TP — this only modifies what is actually SENT to the broker, not the
    setup-quality filter (option (b) per the spec)."""
    sl_to_use = signal["sl"] if ORDER_OPTIONS["use_sl"] else 0.0
    if ORDER_OPTIONS["use_tp"]:
        if ORDER_OPTIONS["tp_mode"] == "fixed_usd":
            fixed_tp = calc_tp_price_from_usd(
                signal["entry"], signal["direction"], lot, ORDER_OPTIONS["tp_fixed_usd"])
            tp_to_use = fixed_tp if fixed_tp is not None else signal["tp2"]
        else:
            tp_to_use = signal["tp2"]
    else:
        tp_to_use = 0.0
    signal["sl"]  = sl_to_use
    signal["tp2"] = tp_to_use


def send_order(signal, lot):
    order_type = mt5.ORDER_TYPE_BUY if signal["direction"] == "long" else mt5.ORDER_TYPE_SELL
    price = mt5.symbol_info_tick(SYMBOL).ask if signal["direction"] == "long" else mt5.symbol_info_tick(SYMBOL).bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": signal["sl"],
        "tp": signal["tp2"],
        "deviation": 20,
        "magic": MAGIC_NUMBER,
        "comment": "XAUUSD trend-pullback EA",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    retcode = getattr(result, "retcode", None)

    # Tag the order_send log line with the strategy that generated this
    # order and the MM settings used to size it, so each placed order is
    # traceable on its own without needing to cross-reference the earlier
    # "Signal"/"MM applied" lines.
    strategy_name = signal.get("strategy", "unknown")
    order_tag = (f"Strategy={strategy_name} | Lot={lot} | "
                 f"RiskPerTrade={RISK_PER_TRADE * 100:.2f}%")

    if retcode is not None and retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"order_send FAILED (retcode={retcode}) [{order_tag}]: {result}")
    else:
        logger.info(f"order_send result [{order_tag}]: {result}")
    return result


# ----------------------------- TRAILING STOP --------------------------------
def modify_sl(position, new_sl, new_tp=None):
    """Move SL (and optionally TP) on an already-open position. Callers must
    only pass a new_sl that improves the locked-in profit — this function
    does not re-check direction itself."""
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": round(new_sl, 2),
        "tp": position.tp if new_tp is None else (round(new_tp, 2) if new_tp else 0.0),
    }
    result = mt5.order_send(request)
    logger.info(f"Trail SL ticket {position.ticket} -> {new_sl:.2f} "
          f"({TRAILING_METHOD}) | {result}")
    return result


def _trail_candidate(df, is_long, price):
    """Returns (candidate_sl, distance_used) for the configured trailing method."""
    if TRAILING_METHOD == "ATR":
        dist = atr(df, ATR_PERIOD).iloc[-1] * TRAILING_ATR_MULT
        candidate = price - dist if is_long else price + dist
        return candidate, dist

    if TRAILING_METHOD == "FIXED_POINTS":
        dist = TRAILING_FIXED_POINTS
        candidate = price - dist if is_long else price + dist
        return candidate, dist

    if TRAILING_METHOD == "PERCENT":
        dist = price * TRAILING_PERCENT / 100.0
        candidate = price - dist if is_long else price + dist
        return candidate, dist

    if TRAILING_METHOD == "EMA":
        ema_val = ema(df["close"], TRAILING_EMA_PERIOD).iloc[-1]
        candidate = ema_val - TRAILING_EMA_BUFFER_POINTS if is_long else ema_val + TRAILING_EMA_BUFFER_POINTS
        dist = abs(price - candidate)
        return candidate, dist

    raise ValueError(f"Unknown TRAILING_METHOD: {TRAILING_METHOD}")


def manage_trailing_stops():
    """Check open positions opened by this EA (matched by MAGIC_NUMBER) and
    ratchet their stop loss forward as price moves favorably, using whichever
    TRAILING_METHOD is configured (ATR / EMA / FIXED_POINTS / PERCENT). SL
    only ever moves in the direction that locks in more profit — it never
    moves backward, and never to a level that would instantly stop the
    position out at a worse price than the current market. Requires MT5
    terminal + this script's loop to be running continuously; it does
    nothing while either is closed."""
    if not TRAILING_ENABLED:
        return

    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return

    df = get_rates(SYMBOL, TF_ENTRY, max(ATR_PERIOD, TRAILING_EMA_PERIOD) + 20)
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return

    for pos in positions:
        if pos.magic != MAGIC_NUMBER:
            continue  # don't touch positions this EA didn't open
        if pos.sl == 0:
            continue  # opened with use_sl=False — no SL to trail

        is_long = pos.type == mt5.POSITION_TYPE_BUY
        price = tick.bid if is_long else tick.ask

        candidate, dist = _trail_candidate(df, is_long, price)
        if pd.isna(candidate) or pd.isna(dist):
            continue

        profit_distance = (price - pos.price_open) if is_long else (pos.price_open - price)
        if profit_distance <= 0:
            continue  # not in profit yet — never trail a losing position

        if TRAILING_METHOD != "EMA" and profit_distance < dist * TRAILING_ACTIVATION_R:
            continue  # ATR/FIXED/PERCENT: wait for activation threshold

        # Safety: never set an SL that would instantly close the position at
        # a worse price than the current market.
        if is_long and candidate >= price:
            continue
        if not is_long and candidate <= price:
            continue

        improves = (candidate > pos.sl) if is_long else (candidate < pos.sl)
        if pos.sl != 0 and not improves:
            continue

        new_tp = 0.0 if TRAILING_REMOVE_TP_ON_ACTIVATE else None
        modify_sl(pos, candidate, new_tp=new_tp)


# ----------------------------- BASKET CLOSE ----------------------------------
def close_position(pos, reason=""):
    """Closes one position at market. Tries IOC fill first, then falls back
    to FOK and RETURN if MT5 returns None (busy trade context). Returns the
    successful OrderSendResult, or None if all attempts fail."""
    is_long = pos.type == mt5.POSITION_TYPE_BUY
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        logger.warning(f"close_position: no tick for {pos.symbol} — cannot close ticket {pos.ticket}")
        return None
    price = tick.bid if is_long else tick.ask
    base_request = {
        "action":   mt5.TRADE_ACTION_DEAL,
        "symbol":   pos.symbol,
        "volume":   pos.volume,
        "type":     mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket,
        "price":    price,
        "deviation": 20,
        "magic":    MAGIC_NUMBER,
        "comment":  f"Basket close: {reason}"[:31],
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        req = dict(base_request, type_filling=filling)
        result = mt5.order_send(req)
        if result is not None:
            retcode = getattr(result, "retcode", None)
            if retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Closed ticket {pos.ticket} ({reason}) | retcode={retcode}")
                return result
            logger.warning(f"close_position ticket {pos.ticket} filling={filling} retcode={retcode} — trying next fill type")
        else:
            logger.warning(f"close_position ticket {pos.ticket} filling={filling} returned None — MT5 context busy, retrying")
        time.sleep(0.3)
    logger.error(f"close_position: all attempts failed for ticket {pos.ticket} ({reason})")
    return None


def check_basket_close():
    """Close all of this EA's open positions together once total floating
    P&L crosses a configured $ or % threshold. Off by default — enable via
    BASKET_CLOSE_ENABLED and at least one threshold."""
    if not BASKET_CLOSE_ENABLED:
        return

    positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC_NUMBER]
    if not positions:
        return

    info = mt5.account_info()
    total_profit = sum(p.profit for p in positions)
    reason = None

    if BASKET_TARGET_PROFIT_USD is not None and total_profit >= BASKET_TARGET_PROFIT_USD:
        reason = f"target profit ${BASKET_TARGET_PROFIT_USD} reached"
    elif BASKET_MAX_LOSS_USD is not None and total_profit <= -abs(BASKET_MAX_LOSS_USD):
        reason = f"max loss ${BASKET_MAX_LOSS_USD} reached"
    elif info and BASKET_TARGET_PROFIT_PCT is not None and total_profit >= info.balance * BASKET_TARGET_PROFIT_PCT / 100:
        reason = f"target profit {BASKET_TARGET_PROFIT_PCT}% of balance reached"
    elif info and BASKET_MAX_LOSS_PCT is not None and total_profit <= -abs(info.balance * BASKET_MAX_LOSS_PCT / 100):
        reason = f"max loss {BASKET_MAX_LOSS_PCT}% of balance reached"

    if reason:
        logger.info(f"Basket close triggered: {reason} "
              f"(total floating P&L: {total_profit:.2f})")
        closed_count = 0
        for pos in positions:
            result = close_position(pos, reason=reason)
            if result is not None:
                closed_count += 1
            time.sleep(0.1)  # give MT5 trade context breathing room between closes
        logger.info(f"Basket close: {closed_count}/{len(positions)} positions closed successfully.")


# ----------------------------- MAIN LOOP ------------------------------------
def run_once():
    in_session, matched = is_within_trading_hours()
    if not in_session:
        labels = [TRADING_SESSIONS[k]["label"] for k in sorted(ALLOWED_SESSIONS) if k in TRADING_SESSIONS]
        logger.debug(f"Outside selected trading session(s) "
              f"({', '.join(labels) if labels else 'none selected'}) — no new entries.")
        return

    info = mt5.account_info()

    # --- Money management gates: checked before even looking for a signal,
    # so a breach blocks ALL new entries regardless of what check_entry_signal
    # would have returned. None of these touch already-open positions —
    # trailing stops and basket close keep managing those independently.
    dd_blocked, dd_reason = check_drawdown_breaker(info)
    if dd_blocked:
        logger.warning(f"Drawdown breaker: {dd_reason} — no new entries until equity recovers.")
        return

    loss_blocked, loss_reason = check_daily_loss_limit(info.balance)
    if loss_blocked:
        logger.warning(f"Daily loss limit: {loss_reason} — no new entries until tomorrow.")
        return

    if MAX_DAILY_TRADES is not None and count_today_new_trades() >= MAX_DAILY_TRADES:
        logger.info(f"Max daily trades ({MAX_DAILY_TRADES}) reached — no new entries until tomorrow.")
        return

    streak_blocked, streak_reason = check_consecutive_loss_breaker()
    if streak_blocked:
        logger.warning(f"Anti-Martingale breaker: {streak_reason}.")
        return

    signal = check_entry_signal()
    if signal is None:
        logger.debug("No signal.")
        return

    # (2) Signal-found notification — legacy-mode equivalent of the
    # confluence path's signal alert, fired as soon as check_entry_signal()
    # returns a qualifying setup (before order placement is attempted).
    if NOTIFY_SIGNAL and _should_send_signal_alert("legacy", signal):
        send_telegram(telegram_alert.format_signal_alert(signal, symbol=SYMBOL))

    lot = calc_lot_size(info.balance, RISK_PER_TRADE, signal["entry"], signal["sl"])

    # --- Build a record of exactly which strategy fired and which MM
    # parameters were applied to size this specific trade, so the log is
    # self-contained for later debugging/audit (per-order traceability).
    risk_amount = info.balance * RISK_PER_TRADE
    risk_points = abs(signal["entry"] - signal["sl"])
    reward_points = abs(signal["tp2"] - signal["entry"])
    achieved_rr = (reward_points / risk_points) if risk_points else 0.0
    strategy_name = signal.get("strategy", "unknown")

    logger.info(f"Signal: {signal['direction'].upper()} | Strategy: {strategy_name} | "
          f"Entry {signal['entry']:.2f} | SL {signal['sl']:.2f} | "
          f"TP1 {signal['tp1']:.2f} | TP2 {signal['tp2']:.2f} | Lot {lot}")
    logger.info(f"MM applied [{strategy_name}]: risk_per_trade={RISK_PER_TRADE * 100:.2f}% "
          f"(balance {info.balance:.2f} -> risk_amount {risk_amount:.2f}) | "
          f"R:R achieved 1:{achieved_rr:.2f} (min required 1:{MIN_RISK_REWARD_RATIO}) | "
          f"lot_range [{MIN_LOT}, {MAX_LOT}] enforce_min_lot={ENFORCE_MIN_LOT} -> Lot {lot}")

    if lot <= 0:
        logger.warning(f"Risk-based lot size rounded to 0 (below MIN_LOT={MIN_LOT} and ENFORCE_MIN_LOT=False) — skipping.")
        return

    if open_positions_count() >= MAX_CONCURRENT_TRADES:
        logger.info("Max concurrent trades reached — skipping.")
        return

    if not AUTO_TRADE:
        logger.info("AUTO_TRADE is False — signal only, no order sent. "
              "Set AUTO_TRADE=True after you've validated this on a demo account.")
        return

    before_tickets = {p.ticket for p in (mt5.positions_get(symbol=SYMBOL) or [])}
    _apply_order_options(signal, lot)
    order_result = send_order(signal, lot)

    # (3) Order-open notification + entry_meta write — legacy mode previously
    # had neither, so closed legacy trades couldn't get a rich close alert
    # and never appeared in any Telegram order-open notification at all.
    retcode = getattr(order_result, "retcode", None)
    if retcode == mt5.TRADE_RETCODE_DONE:
        after_positions = mt5.positions_get(symbol=SYMBOL) or []
        new_positions = [p for p in after_positions if p.ticket not in before_tickets and p.magic == MAGIC_NUMBER]
        new_ticket = new_positions[0].ticket if new_positions else getattr(order_result, "order", None)

        if new_ticket:
            entry_meta = _load_json(ENTRY_META_PATH, {})
            entry_meta[str(new_ticket)] = {
                "strategies": [strategy_name],
                "direction": signal["direction"],
                "strategy": strategy_name,
                "entry_price": signal["entry"],
                "lot": lot,
                "risk_distance": risk_points,
                "opened_at": datetime.now().isoformat(),
            }
            _save_json(ENTRY_META_PATH, entry_meta)

        if NOTIFY_ORDER_OPEN:
            msg = telegram_alert.format_order_alert(signal, lot, strategy_name, account_info=info, symbol=SYMBOL)
            send_telegram(msg)


def main():
    global _LAST_CONFIG_MTIME
    load_ui_config()
    try:
        _LAST_CONFIG_MTIME = os.path.getmtime(CONFIG_JSON_PATH)
    except OSError:
        _LAST_CONFIG_MTIME = None
    try:
        startup_account_info = connect()
    except Exception:
        logger.exception("Failed to connect to MT5 — aborting startup.")
        return

    # Record this process's start time for the dashboard's uptime display.
    # Once per launch — restarting the EA resets the clock, as expected.
    record_bot_start()

    # (1) Startup notification — once, right after a successful connect.
    send_startup_notification(startup_account_info)

    last_signal_scan = 0.0
    consecutive_errors = 0
    try:
        while True:
            now = time.time()

            try:
                # Live configuration: pick up any strategy_config.json change
                # saved while the bot is already running (e.g. from the UI's
                # Save button, or a direct edit) — no restart needed. Runs
                # first, every tick, so everything below this point in the
                # same iteration already sees the freshly-applied settings.
                maybe_reload_config()

                # Breakeven runs first (fast cycle) — once a position has
                # moved BREAKEVEN_TRIGGER_R into profit it can no longer
                # turn into a loss, before the regular trailing method even
                # gets a chance to run.
                manage_breakeven()

                # Trailing stop runs on a fast cycle — it only adjusts SL on
                # positions that are already open, never opens new trades.
                manage_trailing_stops()

                # Basket close (if enabled) checks combined P&L across all
                # open positions and closes everything together once a
                # threshold hits.
                check_basket_close()

                # League System: attribute newly-closed trades back to the
                # strategies that contributed at entry and update bench status.
                # (This also sends the order-close Telegram notification —
                # see update_league_from_closed_trades()'s docstring.)
                update_league_from_closed_trades()

                # Capture any manually placed/closed trades so they appear in
                # the combined P&L on the dashboard.
                track_manual_trades()

                # (5)-(8) Periodic Telegram notifications — each function is
                # self-throttled internally (interval/dedup-state checks), so
                # it's safe to call all four every loop tick.
                check_macro_update_notify()
                check_proxy_staleness_notify()
                check_pre_news_notify()
                check_post_news_notify()
                check_daily_status_notify()

                # Real market-open check (broker ground truth) -- separate
                # from is_within_trading_hours()'s user-preference window.
                # Cheap (one symbol_info + one tick read) so runs every tick.
                market_open, market_reason = check_market_hours_and_notify()
                check_price_sanity()

                # New-entry scan. ENTRY_MODE selects which engine runs and on
                # what cadence: "confluence13" (default) scans all strategies
                # every SCAN_INTERVAL_SECONDS; "logic_groups" runs the
                # Day Trade / Scalping Trade grouped engine on the same
                # cadence; "legacy" runs the original single fib-retracement
                # check on POLL_SECONDS.
                scan_interval = SCAN_INTERVAL_SECONDS if ENTRY_MODE in ("confluence13", "logic_groups") else POLL_SECONDS
                if not market_open:
                    logger.debug(f"Market closed/feed stale ({market_reason}) -- no new-entry scan this tick.")
                elif now - last_signal_scan >= scan_interval:
                    if ENTRY_MODE == "confluence13":
                        run_confluence_scan()
                    elif ENTRY_MODE == "logic_groups":
                        run_logic_groups_scan()
                    else:
                        run_once()
                    last_signal_scan = now
                consecutive_errors = 0  # a clean iteration resets the streak
            except Exception:
                consecutive_errors += 1
                # Always log the full traceback either way.
                logger.exception(
                    f"Unhandled error during main loop iteration "
                    f"({consecutive_errors}/{MAX_ERRORS_BEFORE_STOP} before stop)."
                )
                if STOP_ON_ERROR and consecutive_errors >= MAX_ERRORS_BEFORE_STOP:
                    logger.error(
                        f"STOP_ON_ERROR is enabled and {consecutive_errors} consecutive "
                        f"error(s) reached the configured limit — stopping the bot now. "
                        f"Open positions will NOT be managed (no trailing stop/basket close) "
                        f"until you restart the script."
                    )
                    if TELEGRAM_ENABLED:
                        try:
                            telegram_alert.send_message(
                                TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                                "⛔ Bot STOPPED after a run error (stop_on_error is ON).\n"
                                "Open positions are no longer being managed automatically — "
                                "check the log and restart the bot.",
                                enabled=TELEGRAM_ENABLED,
                            )
                        except Exception:
                            logger.exception("Failed to send the stop-on-error Telegram alert.")
                    break
                # Otherwise: a single bad iteration (e.g. a transient
                # MT5/network hiccup) doesn't kill the whole EA, so open
                # positions keep getting trailing-stop/basket-close care.

            time.sleep(TRAILING_CHECK_SECONDS)
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
