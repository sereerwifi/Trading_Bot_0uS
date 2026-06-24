"""
XAUUSD Strategy Config UI
==========================
Desktop GUI (Tkinter — ships with Python, no extra install) for choosing
which entry-condition logics are active, filling in their parameters, and
setting trailing stop / risk / basket-close parameters.

Saves everything to strategy_config.json in the same folder. The EA script
(xauusd_mt5_strategy.py) loads that file at startup and applies the
Trailing Stop / Risk / Basket Close / Log-Debug sections automatically.

NOTE on the 10 entry-condition strategies: only "Fibonacci Retracement +
Confluence" (fib_confluence) is currently wired into the EA's actual signal
logic (check_entry_signal()). The other 9 are included here so you can
design and save their parameters now — turning each one into a live
detection function inside the EA is a follow-up coding step. Enabling a
checkbox here records your intent; it does not yet change EA behavior for
strategies other than fib_confluence.

Run:
    python strategy_config_ui.py
"""

import json
import os
import subprocess
import sys
import time
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

BOT_VERSION = "9.0"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_config.json")
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT_PATH = os.path.join(THIS_DIR, "xauusd_mt5_strategy.py")
DASHBOARD_SCRIPT_PATH = os.path.join(THIS_DIR, "generate_dashboard.py")
DASHBOARD_SERVER_SCRIPT_PATH = os.path.join(THIS_DIR, "dashboard_server.py")
DASHBOARD_HTML_PATH = os.path.join(THIS_DIR, "dashboard.html")
DASHBOARD_SERVER_PORT = 8787
# Cloudflare Tunnel — exposes the local dashboard server at dashboard.sereewifi.net.
# Launched as its own process alongside the bot/dashboard/web-server when Start
# Bot is pressed, using the same command the user runs manually in PowerShell:
#   cd C:\Users\Administrator\Desktop
#   .\cloudflared.exe tunnel run sereewifi-dashboard
CLOUDFLARED_DIR = r"C:\Users\Administrator\Desktop"
CLOUDFLARED_EXE = "cloudflared.exe"
CLOUDFLARED_TUNNEL_NAME = "sereewifi-dashboard"

# Backup — keeps strategy_config.json, League System state, macro/Big-Data
# history (SQLite), open-position attribution, and logs all bundled into
# timestamped .zip files, so a future VPS move (or a crash) doesn't lose
# the data that strategy/analysis continuity depends on. Launched alongside
# the bot as its own background process (backup_restore.py watch), same
# pattern as the dashboard/tunnel processes above.
BACKUP_SCRIPT_PATH = os.path.join(THIS_DIR, "backup_restore.py")
BACKUP_DIR = os.path.join(THIS_DIR, "backups")
BACKUP_INTERVAL_HOURS = 6
BACKUP_KEEP = 28  # ~1 week of history at the default 6h interval


def _deep_merge(base, override):
    """Recursively merges override into base and returns a NEW dict.
    Unlike dict.update(), nested dicts are merged key-by-key instead of one
    replacing the other wholesale — so a saved config that only has e.g.
    {"strategies": {"fib_confluence": {"enabled": true}}} still keeps every
    other default field (swing_lookback, etc.) instead of losing them."""
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

DEFAULT_CONFIG = {
    "strategies": {
        "ema_cross": {"enabled": False, "fast": 20, "slow": 50},
        "macd_cross": {"enabled": False, "fast": 12, "slow": 26, "signal": 9},
        "rsi_divergence": {"enabled": False, "period": 14, "oversold": 30, "overbought": 70, "lookback": 20},
        "fib_confluence": {"enabled": True, "swing_lookback": 50, "fib_low": 50.0, "fib_high": 61.8,
                            "rsi_low": 40, "rsi_high": 60, "use_macd_trigger": True},
        "bb_breakout": {"enabled": False, "period": 20, "std": 2.0, "squeeze_pct": 2.0},
        "sr_breakout_retest": {"enabled": False, "lookback": 50, "retest_tol": 2.0, "breakout_buffer": 1.0},
        "price_action": {"enabled": False, "pin_bar": True, "engulfing": True, "proximity_points": 5.0},
        "atr_donchian_breakout": {"enabled": False, "donchian_period": 20, "atr_period": 14, "atr_mult": 1.0},
        "mtf_alignment": {"enabled": False, "use_h4": True, "use_h1": True, "use_m15": False, "ema_period": 50},
        "news_momentum": {"enabled": False, "avoid_minutes": 30, "momentum_atr_mult": 1.5},
    },
    "trailing_stop": {
        "enabled": True,
        "method": "ATR",
        "atr_mult": 1.5,
        "fixed_points": 5.0,
        "percent": 0.3,
        "ema_period": 20,
        "ema_buffer_points": 7.0,
        "activation_r": 1.0,
        "remove_tp_on_activate": False,
        "check_seconds": 30,
    },
    "risk": {
        "symbol": "XAUUSD",
        "auto_trade": False,
        "risk_per_trade_pct": 1.0,
        "lot_step": 0.01,
        "value_per_point_per_lot": 100.0,
        "max_concurrent_trades": 2,
    },
    "money_management": {
        "min_lot": 0.01,
        "max_lot": 5.0,
        "enforce_min_lot": True,
        "max_daily_trades": 5,
        "max_drawdown_pct": 10.0,
        "daily_loss_limit_r": 3,
        "min_risk_reward_ratio": 1.5,
        "max_consecutive_losses": 3,
    },
    "basket_close": {
        "enabled": False,
        "target_profit_usd": "",
        "max_loss_usd": "",
        "target_profit_pct": "",
        "max_loss_pct": "",
    },
    "daily_filter": {
        "enabled": True,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "require_full_stack": False,
    },
    "trading_hours": {
        "enabled": True,
        "sessions": {
            "asia": False,
            "london": False,
            "overlap": False,
            "all_day": True,
        },
    },
    "logging": {
        "level": "INFO",
        "log_to_console": True,
        "log_dir": "logs",
        "max_bytes": 5242880,
        "backup_count": 5,
    },
    "error_handling": {
        "stop_on_error": False,
        "max_errors_before_stop": 1,
    },
    "confluence": {
        "entry_mode": "confluence13",
        # Only used when entry_mode == "logic_groups": "day_trade" / "scalping" / "both".
        "logic_group_selection": "both",
        # Each Logic Group already has its own trend filter (Step 1) — keep this
        # OFF by default so the D1 Daily Filter doesn't ALSO have to agree, which
        # double-gates and tends to block almost every trade. See the comment at
        # LOGIC_GROUPS_APPLY_DAILY_FILTER in xauusd_mt5_strategy.py.
        "logic_groups_apply_daily_filter": False,
        "scan_interval_seconds": 30,
        "min_strategy_score": 70.0,
        "min_agreeing_strategies": 3,
        "sl_atr_mult": 1.5,
        "tp_rr": 2.0,
        # Weights below are MY recommended defaults (not all 1.0) — a higher
        # number means the strategy gets more influence in the weighted
        # combined score, a lower number means it still votes but counts for
        # less. Rough logic: 1.2-1.4 = high-conviction structural/SMC concepts
        # and the institutional Big Data check; 1.0-1.1 = solid, dependable
        # classics; 0.7-0.9 = useful confirmation but historically more prone
        # to lag or false signals on XAUUSD specifically. Every strategy stays
        # ENABLED by default — nothing is muted, only down-weighted — so you
        # still see all 24 scores and can re-tune any of this yourself.
        "strategies": {
            "order_block": {"enabled": True, "weight": 1.3},       # ICT concept, strong edge
            "supply_demand": {"enabled": True, "weight": 1.1},
            "ema_cross": {"enabled": True, "weight": 0.8},          # lagging trend filter
            "rsi_divergence": {"enabled": True, "weight": 0.8},     # decent but false-signal prone
            "london_breakout": {"enabled": True, "weight": 1.1},
            "fibonacci": {"enabled": True, "weight": 0.8},          # retracement levels are subjective
            "vwap_rejection": {"enabled": True, "weight": 1.0},
            "news_fade": {"enabled": True, "weight": 0.7},          # contrarian/riskier setup
            "multi_tf_align": {"enabled": True, "weight": 1.3},     # strong confirmation layer
            "bos_choch": {"enabled": True, "weight": 1.2},          # core SMC structure shift
            "liquidity_sweep": {"enabled": True, "weight": 1.3},    # core SMC, strong edge
            "fair_value_gap": {"enabled": True, "weight": 1.1},
            "opening_range_breakout": {"enabled": True, "weight": 1.0},
            # merged in from the original v1 10-strategy list (the other 5 —
            # ema_cross, rsi_divergence, fib_confluence, mtf_alignment,
            # news_momentum — were already covered above under different
            # names, so they were NOT duplicated here):
            "macd_cross": {"enabled": True, "weight": 0.7},         # lagging momentum
            "bb_breakout": {"enabled": True, "weight": 0.8},        # prone to whipsaws
            "sr_breakout_retest": {"enabled": True, "weight": 1.1},
            "price_action": {"enabled": True, "weight": 1.0},
            "atr_donchian_breakout": {"enabled": True, "weight": 0.9},
            # real MT5 Depth-of-Market (Level2) order-flow approximation —
            # scores 0/0 gracefully if your broker/symbol doesn't expose DOM.
            "order_flow_dom": {"enabled": True, "weight": 1.0},
            # institutional Big Data / macro fundamentals checklist (DXY,
            # US10Y yield, ETF flow, COT positioning, COMEX inventory, H1
            # trend) — see macro_data.py. Scores 0/0 gracefully until the
            # first successful background fetch. Weighted a bit above the
            # 1.0 baseline since it's the institutional/macro baseline the
            # other 23 price-action strategies don't otherwise see.
            "macro_bias": {"enabled": True, "weight": 1.2},
            # 4 scalping strategies (#21-24) — weighted per the win-rate info
            # you gave when these were added: London Sweep 55-65% WR, EMA
            # Pullback 60-70% WR (but only fires in a strong, fully-stacked
            # trend, so it's rarer), NY ORB is the most news-dependent/risky
            # of the four, and the Combo (#24) is your own "most recommended"
            # setup since it requires all 4 layers (H1 trend + M5 EMA filter
            # + sweep + reclaim) to align — hence the highest weight here.
            "scalp_london_sweep": {"enabled": True, "weight": 1.0},
            "scalp_ema_pullback": {"enabled": True, "weight": 1.1},
            "scalp_ny_orb": {"enabled": True, "weight": 0.8},
            "scalp_combo_sweep": {"enabled": True, "weight": 1.4},
            # 25th: Myfxbook public Community Outlook (retail sentiment).
            # Scores 0/0 gracefully until Myfxbook is enabled + credentials set.
            # Weight kept strictly below macro_bias (1.2) — secondary signal only.
            "myfxbook_sentiment": {"enabled": True, "weight": 0.8},
            # 26th: extreme/exhausted directional move that slams into a fresh
            # extreme or known S/R level and snaps back with a rejection candle.
            "climax_reversal_sr": {"enabled": True, "weight": 1.0},
        },
    },
    "league": {
        "enabled": True,
        "max_consecutive_losses": 3,
        "min_winrate_pct": 45.0,
        "winrate_lookback_trades": 10,
        "bench_hours": 24,
        "shadow_simulation_enabled": True,
        "min_samples_for_adjustment": 5,
    },
    "breakeven": {
        "enabled": True,
        "trigger_r": 1.0,
        "buffer_points": 2.0,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
    },
    "myfxbook": {
        "enabled": False,
        "email": "",
        "password": "",
        "contrarian": True,
    },
    "dashboard_auth": {
        "username": "",
        "password": "",
    },
    "process_control": {
        "kill_stale_on_start": True,
    },
}

# Display labels for the confluence strategies — the original 13 (★ = ICT/SMC
# concept, ✦ = liquidity/structure concept) PLUS 5 strategies merged in from
# the v1 "10 strategies" list (macd_cross, bb_breakout, sr_breakout_retest,
# price_action, atr_donchian_breakout). The other 5 v1 strategies (ema_cross,
# rsi_divergence, fib_confluence, mtf_alignment, news_momentum) already had a
# near-identical strategy in the 13, so they were NOT duplicated. PLUS #19,
# Order Flow (DOM) — a real MT5 Depth-of-Market read, NOT the 3rd-party
# Order Flow Footprint / FXSSI Order Book / Order Block Flow Elite indicators
# (those require data MT5's Python API can't access: per-tick bid/ask volume
# history or FXSSI's own broker-sentiment service). PLUS #20, Macro Bias (Big
# Data) — real macro/fundamental data (DXY, US10Y yield, COT, COMEX
# inventory, ETF flow where available) fetched from free public sources by
# macro_data.py, NOT price-derived like the other 19 — 20 total.
STRATEGY13_LABELS = {
    "order_block": "1. Order Block (ICT) ★",
    "supply_demand": "2. Supply & Demand",
    "ema_cross": "3. EMA Cross",
    "rsi_divergence": "4. RSI Divergence",
    "london_breakout": "5. London Breakout",
    "fibonacci": "6. Fibonacci",
    "vwap_rejection": "7. VWAP Rejection",
    "news_fade": "8. News Fade",
    "multi_tf_align": "9. Multi-TF Align",
    "bos_choch": "10. BOS/CHoCH",
    "liquidity_sweep": "11. Liquidity Sweep ✦",
    "fair_value_gap": "12. Fair Value Gap ✦",
    "opening_range_breakout": "13. Opening Range Breakout ✦",
    "macd_cross": "14. MACD Signal Cross (จาก v1)",
    "bb_breakout": "15. Bollinger Band Breakout (จาก v1)",
    "sr_breakout_retest": "16. S/R Breakout + Retest (จาก v1)",
    "price_action": "17. Price Action Candlestick (จาก v1)",
    "atr_donchian_breakout": "18. ATR/Donchian Breakout (จาก v1)",
    "order_flow_dom": "19. Order Flow (DOM) — Bid/Ask Imbalance",
    "macro_bias": "20. Macro Bias (Big Data) — DXY/Yield/COT/COMEX/ETF",
    "scalp_london_sweep": "21. Scalping: London Open Liquidity Sweep (M5) 🩳",
    "scalp_ema_pullback": "22. Scalping: EMA Pullback (M1, เทรนด์แรงเท่านั้น) 🩳",
    "scalp_ny_orb": "23. Scalping: NY Session Breakout (M5, 19:30-23:00) 🩳",
    "scalp_combo_sweep": "24. Scalping: EMA20+EMA50+Liquidity Sweep ★ (แนะนำที่สุด) 🩳",
    "myfxbook_sentiment": "25. Myfxbook Retail Sentiment (Community Outlook)",
    "climax_reversal_sr": "26. Climax Reversal at Support/Resistance ★",
}

SESSION_INFO = {
    "asia": {
        "title": "1) 07:00 - 12:00 น. ☀️  ตลาดเอเชีย — เงียบสงบ / วิ่งในกรอบ",
        "desc": "ปริมาณซื้อขายน้อยที่สุด กราฟไซด์เวย์ในกรอบแคบ ไม่ค่อยมีเทรนด์รุนแรง "
                "(ยกเว้นมีข่าวด่วน) เหมาะกับมือใหม่หรือสายเก็บสั้นในกรอบ",
    },
    "london": {
        "title": "2) 14:00 - 17:00 น. ⛅  ลอนดอนเปิดทำการ — คึกคักกำลังดี",
        "desc": "ปริมาณซื้อขายฝั่งยุโรปเข้ามา กราฟเริ่มเลือกทางและมักเกิด Breakout "
                "ทะลุกรอบราคาช่วงเช้า เหมาะกับสายเทรดตามเทรนด์ที่ไม่อยากเจอสวิงรุนแรงเกินไป",
    },
    "overlap": {
        "title": "3) 19:00 - 23:00 น. ⭐  London-NY Overlap — Golden Period (ดีที่สุด/วิ่งแรงที่สุด)",
        "desc": "ลอนดอน (ซื้อขายทองคำแท่งใหญ่ที่สุด) ซ้อนทับนิวยอร์ก (COMEX futures) "
                "กราฟวิ่งแรงที่สุดในวัน สเปรดแคบสุด ทิศทางชัดเจน ข่าวเศรษฐกิจสหรัฐฯ "
                "(NFP, CPI ฯลฯ) มักประกาศ 19:30/20:30 ทำให้สวิงแรงเป็นพิเศษ "
                "เหมาะกับ Day Trade / Scalping",
    },
    "all_day": {
        "title": "4) 00:00 - 23:59 น. 🌐  All Day — เทรดได้ตลอด 24 ชม. (ไม่จำกัดช่วงเวลา)",
        "desc": "ปิดการกรองตามช่วงเวลา ปล่อยให้บอทเข้าออเดอร์ได้ตลอดทั้งวัน เหมาะถ้าต้องการให้ "
                "4 กลยุทธ์ Scalping (London Sweep / EMA Pullback / NY ORB / Combo) ทำงานครบทุกช่วงตาม "
                "เงื่อนไขเวลาของมันเอง โดยไม่ถูกตัวกรองช่วงเวลาหลักนี้บังตัดทิ้งไปก่อน — ใส่ ✓ ที่ช่องนี้ "
                "ช่องเดียวก็พอ ระบบจะไม่สนใจช่องอื่นที่ติ๊กไว้อีกต่อไป (เทรดได้ทุกชั่วโมง)",
    },
}

STRATEGY_LABELS = {
    "ema_cross": "1. EMA Crossover",
    "macd_cross": "2. MACD Signal Cross",
    "rsi_divergence": "3. RSI Divergence",
    "fib_confluence": "4. Fibonacci Retracement + Confluence (live in EA)",
    "bb_breakout": "5. Bollinger Band Breakout/Squeeze",
    "sr_breakout_retest": "6. Support/Resistance Breakout + Retest",
    "price_action": "7. Price Action Candlestick at Key Level",
    "atr_donchian_breakout": "8. ATR/Donchian Channel Breakout",
    "mtf_alignment": "9. Multi-Timeframe Trend Alignment",
    "news_momentum": "10. News/Fundamental Momentum Breakout",
}


class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self._setup_thai_font()
        self.title(f"XAUUSD Strategy Config — v{BOT_VERSION}")
        self.geometry("760x640")
        self.config_data = self.load()
        self.vars = {}
        self.bot_proc = None
        self.dashboard_proc = None
        self.web_server_proc = None
        self.tunnel_proc = None
        self.backup_proc = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_confluence = ScrollFrame(nb)
        self.tab_logic_groups = ScrollFrame(nb)
        self.tab_league = ttk.Frame(nb)
        self.tab_strategies = ScrollFrame(nb)
        self.tab_daily = ttk.Frame(nb)
        self.tab_hours = ScrollFrame(nb)
        self.tab_trailing = ttk.Frame(nb)
        self.tab_risk = ttk.Frame(nb)
        self.tab_telegram = ttk.Frame(nb)
        self.tab_myfxbook = ttk.Frame(nb)
        self.tab_logging = ttk.Frame(nb)
        self.tab_dashboard_auth = ttk.Frame(nb)
        nb.add(self.tab_confluence, text="24 กลยุทธ์ (Confluence)")
        nb.add(self.tab_logic_groups, text="Logic Groups (Day Trade / Scalping)")
        nb.add(self.tab_league, text="League System")
        nb.add(self.tab_strategies, text="เงื่อนไขเข้าออเดอร์เดิม (10) [ใช้เมื่อ entry_mode=legacy]")
        nb.add(self.tab_daily, text="Daily Trend Filter")
        nb.add(self.tab_hours, text="ช่วงเวลาเทรด")
        nb.add(self.tab_trailing, text="Trailing Stop")
        nb.add(self.tab_risk, text="Risk & Basket Close")
        nb.add(self.tab_telegram, text="Telegram Alert")
        nb.add(self.tab_myfxbook, text="Myfxbook Sentiment")
        nb.add(self.tab_logging, text="Log / Debug")
        nb.add(self.tab_dashboard_auth, text="Dashboard Web Access")

        self.build_confluence_tab(self.tab_confluence.inner)
        self.build_logic_groups_tab(self.tab_logic_groups.inner)
        self.build_league_tab(self.tab_league)
        self.build_strategies_tab(self.tab_strategies.inner)
        self.build_daily_tab(self.tab_daily)
        self.build_trading_hours_tab(self.tab_hours.inner)
        self.build_trailing_tab(self.tab_trailing)
        self.build_risk_tab(self.tab_risk)
        self.build_telegram_tab(self.tab_telegram)
        self.build_myfxbook_tab(self.tab_myfxbook)
        self.build_logging_tab(self.tab_logging)
        self.build_dashboard_auth_tab(self.tab_dashboard_auth)

        ctrl = ttk.LabelFrame(self, text="Bot Control — เลือกกลยุทธ์แล้วกด Start เพื่อรันทุกอย่างที่ต้องใช้")
        ctrl.pack(fill="x", padx=8, pady=(0, 4))
        row1 = ttk.Frame(ctrl)
        row1.pack(fill="x", padx=6, pady=4)
        self.btn_start = ttk.Button(row1, text="▶ Start Bot (Save + Run All)", command=self.start_bot)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row1, text="■ Stop Bot", command=self.stop_bot, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(row1, text="🌐 Open Dashboard", command=self.open_dashboard).pack(side="left", padx=8)
        ttk.Button(row1, text="💾 Backup Now", command=self.backup_now).pack(side="left", padx=8)
        ttk.Button(row1, text="📂 Open Backups Folder", command=self.open_backups_folder).pack(side="left", padx=8)
        ttk.Button(row1, text="🔪 Force Kill Stale", command=self.force_kill_stale).pack(side="left", padx=8)
        row2 = ttk.Frame(ctrl)
        row2.pack(fill="x", padx=6, pady=(0, 4))
        self.lbl_bot_status = ttk.Label(row2, text="EA Bot: ไม่ได้รัน")
        self.lbl_bot_status.pack(side="left")
        self.lbl_dash_status = ttk.Label(row2, text="   |   Dashboard (auto-refresh): ไม่ได้รัน")
        self.lbl_dash_status.pack(side="left")
        self.lbl_web_status = ttk.Label(row2, text=f"   |   Web (port {DASHBOARD_SERVER_PORT}): ไม่ได้รัน")
        self.lbl_web_status.pack(side="left")
        self.lbl_tunnel_status = ttk.Label(row2, text="   |   Cloudflare Tunnel: ไม่ได้รัน")
        self.lbl_tunnel_status.pack(side="left")
        self.lbl_backup_status = ttk.Label(row2, text="   |   Auto-Backup: ไม่ได้รัน")
        self.lbl_backup_status.pack(side="left")
        row3 = ttk.Frame(ctrl)
        row3.pack(fill="x", padx=6, pady=(0, 2))
        self.reg_bool(
            row3, "process_control", "kill_stale_on_start",
            "ฆ่าโปรเซสเก่าที่หลงเหลือก่อน Start Bot และหลัง Stop Bot "
            "(EA/Dashboard/Web/Tunnel/Backup) — แนะนำให้เปิดไว้",
            row=0,
        )
        note = ttk.Label(
            ctrl,
            text="หมายเหตุ: Big Data/Macro จะถูกดึงอัตโนมัติโดยตัวบอทเองทุกรอบสแกน ไม่ต้องรันแยก. "
                 "ตั้ง username/password ในแท็บ \"Dashboard Web Access\" ก่อน Start เพื่อให้ dashboard_server.py "
                 f"เปิดพอร์ต {DASHBOARD_SERVER_PORT} ให้ดูผ่านเน็ตได้ (ต้องตั้ง DNS/Tunnel เพิ่มเองสำหรับโดเมนจริง). "
                 f"กด Start Bot จะรัน cloudflared tunnel \"{CLOUDFLARED_TUNNEL_NAME}\" ให้อัตโนมัติด้วย "
                 f"(ต้องมี {CLOUDFLARED_EXE} อยู่ใน {CLOUDFLARED_DIR}). "
                 f"กด Start Bot จะเริ่ม Auto-Backup ให้ด้วย — สำรองข้อมูลทั้งหมด (config, League System, "
                 f"ประวัติ Big Data, ตำแหน่งที่เปิดอยู่, log) ทุก {BACKUP_INTERVAL_HOURS} ชม. ไว้ที่โฟลเดอร์ "
                 f"backups/ เก็บไว้ {BACKUP_KEEP} ไฟล์ล่าสุด — ใช้ตอนย้าย VPS ได้โดยไม่ขาดข้อมูลสำหรับวิเคราะห์/ตัดสินใจกลยุทธ์ต่อเนื่อง. "
                 "ปิดหน้าต่างนี้ไม่ได้หยุดบอท/แดชบอร์ดที่รันอยู่ — ใช้ปุ่ม Stop Bot เพื่อหยุดจริง ๆ",
            wraplength=720, justify="left", foreground="#555",
        )
        note.pack(fill="x", padx=6, pady=(0, 4))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=8)
        ttk.Button(bottom, text="Save Config", command=self.save).pack(side="right")
        ttk.Button(bottom, text="Reset to Defaults", command=self.reset_defaults).pack(side="right", padx=8)
        self.status = ttk.Label(bottom, text=f"Config file: {CONFIG_PATH}")
        self.status.pack(side="left")

    # ---------- persistence ----------
    def load(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return _deep_merge(DEFAULT_CONFIG, data)
            except Exception:
                pass
        return json.loads(json.dumps(DEFAULT_CONFIG))

    def reset_defaults(self):
        if messagebox.askyesno("Reset", "Reset all fields to default values?"):
            self.config_data = json.loads(json.dumps(DEFAULT_CONFIG))
            for key, var in self.vars.items():
                section, field = key.split(".", 1)
                val = self.config_data[section]
                for part in field.split("."):
                    val = val[part]
                var.set(val)

    def save(self, silent=False):
        try:
            for key, var in self.vars.items():
                section, field = key.split(".", 1)
                parts = field.split(".")
                target = self.config_data[section]
                for p in parts[:-1]:
                    target = target[p]
                target[parts[-1]] = var.get()
            # Atomic write (tmp file + os.replace): the bot now hot-reloads
            # this same file live while running (polls its mtime every loop
            # tick), so a plain in-place write could in theory be read
            # mid-write. Writing to a temp file first and atomically
            # replacing it means the bot only ever sees a complete file.
            tmp_path = CONFIG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.config_data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, CONFIG_PATH)
            if not silent:
                messagebox.showinfo("Saved", f"Config saved to:\n{CONFIG_PATH}")
            return True
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return False

    # ---------- bot control (start/stop EA + dashboard) ----------
    def _popen_script(self, script_path, extra_args=None):
        """Launches a Python script as its own process so it keeps running
        independently of this UI window. On Windows it gets its own console
        window (CREATE_NEW_CONSOLE) so the user can watch the EA's live log
        output exactly like running it manually in PowerShell; on
        macOS/Linux it just inherits this process's stdout/stderr."""
        args = [sys.executable, script_path] + (extra_args or [])
        kwargs = {"cwd": THIS_DIR}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        return subprocess.Popen(args, **kwargs)

    def _popen_tunnel(self):
        """Launches the Cloudflare Tunnel that exposes the local dashboard
        web server at dashboard.sereewifi.net, equivalent to running manually:
            cd C:\\Users\\Administrator\\Desktop
            .\\cloudflared.exe tunnel run sereewifi-dashboard
        Raises FileNotFoundError if cloudflared.exe isn't where expected, so
        callers can show a clear message instead of a generic crash."""
        exe_path = os.path.join(CLOUDFLARED_DIR, CLOUDFLARED_EXE)
        if not os.path.exists(exe_path):
            raise FileNotFoundError(exe_path)
        args = [exe_path, "tunnel", "run", CLOUDFLARED_TUNNEL_NAME]
        kwargs = {"cwd": CLOUDFLARED_DIR}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        return subprocess.Popen(args, **kwargs)

    def _popen_backup_watch(self):
        """Launches backup_restore.py in continuous 'watch' mode, which
        zips strategy_config.json/strategy_league.json/macro_data_history.db/
        macro_data_cache.json/open_entry_meta.json/strategy_scores.json/
        processed_deals.json/news_alert_state.json/logs/ into a timestamped
        archive every BACKUP_INTERVAL_HOURS, pruning to the last BACKUP_KEEP.
        This is what keeps strategy/analysis data continuous across a future
        VPS move — without it, a move that only copies the .py files would
        silently reset League System history, Big Data history, and
        open-position attribution."""
        if not os.path.exists(BACKUP_SCRIPT_PATH):
            raise FileNotFoundError(BACKUP_SCRIPT_PATH)
        return self._popen_script(
            BACKUP_SCRIPT_PATH,
            ["watch", "--out", BACKUP_DIR,
             "--interval-hours", str(BACKUP_INTERVAL_HOURS),
             "--keep", str(BACKUP_KEEP)],
        )

    def backup_now(self):
        """Synchronous one-off backup, triggered from the UI button — runs
        in-process via backup_restore.create_backup() rather than spawning
        a subprocess, since a single zip is fast enough not to need it."""
        try:
            import backup_restore
            path = backup_restore.create_backup(out_dir=BACKUP_DIR)
            backup_restore.prune_old_backups(out_dir=BACKUP_DIR, keep=BACKUP_KEEP)
        except Exception as e:
            messagebox.showerror("Backup ไม่สำเร็จ", str(e))
            return
        messagebox.showinfo("Backup สำเร็จ", f"สำรองข้อมูลแล้ว:\n{path}")

    def open_backups_folder(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(BACKUP_DIR)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", BACKUP_DIR])
        else:
            subprocess.Popen(["xdg-open", BACKUP_DIR])

    def start_bot(self):
        if self.config_data.get("process_control", {}).get("kill_stale_on_start", True):
            killed = self.kill_stale_processes(log_fn=lambda msg: print(f"[start_bot] {msg}"))
            if killed:
                time.sleep(1)
        if self.bot_proc is not None and self.bot_proc.poll() is None:
            messagebox.showinfo("Bot กำลังรันอยู่", "บอทกำลังรันอยู่แล้ว — กด Stop Bot ก่อนถ้าต้องการรันใหม่ด้วยค่าที่เพิ่งแก้ไข")
            return

        if not self.save(silent=True):
            return  # save() already showed the error dialog

        if not os.path.exists(BOT_SCRIPT_PATH):
            messagebox.showerror("ไม่พบไฟล์", f"ไม่พบ {BOT_SCRIPT_PATH}")
            return

        web_auth = self.config_data.get("dashboard_auth", {})
        web_creds_ok = bool(web_auth.get("username")) and bool(web_auth.get("password"))

        tunnel_error = None
        backup_error = None
        try:
            self.bot_proc = self._popen_script(BOT_SCRIPT_PATH)
            self.dashboard_proc = None
            self.web_server_proc = None
            self.tunnel_proc = None
            self.backup_proc = None
            if os.path.exists(DASHBOARD_SCRIPT_PATH):
                self.dashboard_proc = self._popen_script(DASHBOARD_SCRIPT_PATH, ["--watch", "--interval", "60"])
            if web_creds_ok and os.path.exists(DASHBOARD_SERVER_SCRIPT_PATH):
                self.web_server_proc = self._popen_script(
                    DASHBOARD_SERVER_SCRIPT_PATH, ["--port", str(DASHBOARD_SERVER_PORT)]
                )
            try:
                self.tunnel_proc = self._popen_tunnel()
            except FileNotFoundError as e:
                # Don't abort the whole start sequence over a missing tunnel
                # binary — the bot/dashboard/web-server above are already
                # running fine; just surface this in the final message.
                tunnel_error = str(e)
            try:
                self.backup_proc = self._popen_backup_watch()
            except FileNotFoundError as e:
                backup_error = str(e)
        except Exception as e:
            messagebox.showerror("เริ่มบอทไม่สำเร็จ", str(e))
            self.bot_proc = None
            self.dashboard_proc = None
            self.web_server_proc = None
            self.tunnel_proc = None
            self.backup_proc = None
            return

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._poll_processes()
        web_note = (
            f"เปิดเว็บ dashboard ที่พอร์ต {DASHBOARD_SERVER_PORT} แล้ว (ต้องตั้ง DNS/Tunnel เพิ่มเองเพื่อให้เข้าผ่านโดเมนจริงได้)"
            if web_creds_ok else
            "ยังไม่ได้เปิดเว็บ dashboard — ไปตั้ง username/password ในแท็บ \"Dashboard Web Access\" ก่อน แล้วกด Start Bot อีกครั้ง"
        )
        tunnel_note = (
            f"ไม่พบ {tunnel_error} — ไม่ได้รัน Cloudflare Tunnel (เปิด dashboard ผ่าน dashboard.sereewifi.net ไม่ได้จนกว่าจะรันเองหรือแก้ที่อยู่ไฟล์)"
            if tunnel_error else
            f"รัน Cloudflare Tunnel \"{CLOUDFLARED_TUNNEL_NAME}\" แล้ว — เข้าผ่าน dashboard.sereewifi.net ได้เลย"
        )
        backup_note = (
            f"ไม่พบ {backup_error} — ไม่ได้รัน Auto-Backup (กด \"💾 Backup Now\" เพื่อสำรองเองได้ตลอด)"
            if backup_error else
            f"เริ่ม Auto-Backup แล้ว — สำรองทุก {BACKUP_INTERVAL_HOURS} ชม. เก็บ {BACKUP_KEEP} ไฟล์ล่าสุดที่ backups/"
        )
        messagebox.showinfo(
            "Started",
            "เริ่มบอทแล้ว (ตาม config ที่บันทึกไว้)\n"
            "Dashboard จะ refresh ทุก 60 วินาทีโดยอัตโนมัติ (dashboard.html)\n"
            "Big Data/Macro ถูกดึงโดยตัวบอทเองอยู่แล้วทุกรอบสแกน — ไม่ต้องรันแยก\n"
            f"{web_note}\n"
            f"{tunnel_note}\n"
            f"{backup_note}",
        )

    def stop_bot(self):
        for attr in ("bot_proc", "dashboard_proc", "web_server_proc", "tunnel_proc", "backup_proc"):
            proc = getattr(self, attr)
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.bot_proc = None
        self.dashboard_proc = None
        self.web_server_proc = None
        self.tunnel_proc = None
        self.backup_proc = None

        if self.config_data.get("process_control", {}).get("kill_stale_on_start", True):
            killed = self.kill_stale_processes(log_fn=lambda msg: print(f"[stop_bot] {msg}"))
            if killed:
                detail = "\n".join(f"  PID {pid}: {cmd[:100]}" for pid, cmd in killed)
                messagebox.showinfo(
                    "พบโปรเซสที่หลงเหลือ",
                    f"พบและปิดโปรเซสที่หลงเหลือ {len(killed)} รายการ\n{detail}",
                )

        self.lbl_bot_status.config(text="EA Bot: ไม่ได้รัน")
        self.lbl_dash_status.config(text="   |   Dashboard (auto-refresh): ไม่ได้รัน")
        self.lbl_web_status.config(text=f"   |   Web (port {DASHBOARD_SERVER_PORT}): ไม่ได้รัน")
        self.lbl_tunnel_status.config(text="   |   Cloudflare Tunnel: ไม่ได้รัน")
        self.lbl_backup_status.config(text="   |   Auto-Backup: ไม่ได้รัน")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")

    def open_dashboard(self):
        if not os.path.exists(DASHBOARD_HTML_PATH):
            messagebox.showinfo(
                "ยังไม่มี Dashboard",
                "ยังไม่พบ dashboard.html — กด Start Bot ก่อน (จะ generate ให้อัตโนมัติ) "
                "หรือรัน generate_dashboard.py เองครั้งหนึ่งก่อน",
            )
            return
        webbrowser.open(f"file://{DASHBOARD_HTML_PATH}")

    def _poll_processes(self):
        bot_alive = self.bot_proc is not None and self.bot_proc.poll() is None
        dash_alive = self.dashboard_proc is not None and self.dashboard_proc.poll() is None
        web_alive = self.web_server_proc is not None and self.web_server_proc.poll() is None
        tunnel_alive = self.tunnel_proc is not None and self.tunnel_proc.poll() is None
        backup_alive = self.backup_proc is not None and self.backup_proc.poll() is None
        self.lbl_bot_status.config(
            text=f"EA Bot: {'🟢 RUNNING (PID ' + str(self.bot_proc.pid) + ')' if bot_alive else '🔴 หยุด/ไม่ได้รัน'}"
        )
        self.lbl_dash_status.config(
            text=f"   |   Dashboard (auto-refresh): {'🟢 RUNNING (PID ' + str(self.dashboard_proc.pid) + ')' if dash_alive else '🔴 หยุด/ไม่ได้รัน'}"
        )
        self.lbl_web_status.config(
            text=f"   |   Web (port {DASHBOARD_SERVER_PORT}): "
                 f"{'🟢 RUNNING (PID ' + str(self.web_server_proc.pid) + ')' if web_alive else '🔴 หยุด/ไม่ได้รัน'}"
        )
        self.lbl_tunnel_status.config(
            text=f"   |   Cloudflare Tunnel: "
                 f"{'🟢 RUNNING (PID ' + str(self.tunnel_proc.pid) + ')' if tunnel_alive else '🔴 หยุด/ไม่ได้รัน'}"
        )
        self.lbl_backup_status.config(
            text=f"   |   Auto-Backup: "
                 f"{'🟢 RUNNING (PID ' + str(self.backup_proc.pid) + ')' if backup_alive else '🔴 หยุด/ไม่ได้รัน'}"
        )
        if not bot_alive and self.btn_stop["state"] == "normal" and self.bot_proc is not None:
            # The bot process exited on its own (crash or manual stop) — flip the
            # buttons back so the user notices and can restart instead of
            # thinking it's still running.
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
        if bot_alive or dash_alive or web_alive or tunnel_alive or backup_alive:
            self.after(3000, self._poll_processes)

    def on_close(self):
        bot_alive = self.bot_proc is not None and self.bot_proc.poll() is None
        dash_alive = self.dashboard_proc is not None and self.dashboard_proc.poll() is None
        web_alive = self.web_server_proc is not None and self.web_server_proc.poll() is None
        tunnel_alive = self.tunnel_proc is not None and self.tunnel_proc.poll() is None
        backup_alive = self.backup_proc is not None and self.backup_proc.poll() is None
        if bot_alive or dash_alive or web_alive or tunnel_alive or backup_alive:
            keep = messagebox.askyesno(
                "บอทยังรันอยู่",
                "บอท/Dashboard ยังรันอยู่ในพื้นหลัง\n\n"
                "กด Yes เพื่อปล่อยให้รันต่อไป (ปิดแค่หน้าต่างนี้)\n"
                "กด No เพื่อหยุดบอทพร้อมปิดหน้าต่างนี้",
            )
            if not keep:
                self.stop_bot()
        self.destroy()

    # ---------- Thai font ----------
    def _setup_thai_font(self):
        """Sets Leelawadee UI (Windows 10+ standard Thai font) as the app-wide
        default so all Thai labels render as real characters, not empty boxes.
        Falls back to Tahoma (older but still has full Thai coverage), then
        leaves Tk's default unchanged if neither is available (e.g. macOS/Linux)."""
        candidates = ["Leelawadee UI", "Tahoma"]
        available = set(tkfont.families())
        chosen = next((f for f in candidates if f in available), None)
        if chosen:
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                try:
                    tkfont.nametofont(name).configure(family=chosen, size=10)
                except Exception:
                    pass
            try:
                ttk.Style().configure(".", font=(chosen, 10))
            except Exception:
                pass

    # ---------- stale-process cleanup ----------
    def kill_stale_processes(self, log_fn=None):
        """Scans the OS process table and kills any leftover instance of this
        project's own helper scripts (EA, dashboard --watch, web server,
        cloudflared tunnel, backup watch) from a previous session that this
        UI process lost track of. Never touches MT5's terminal.exe /
        terminal64.exe or unrelated processes — matches strictly on full
        command-line script path. Returns list of (pid, cmdline_str) tuples
        for what was killed, so callers can report a count to the user."""
        log_fn = log_fn or (lambda msg: None)
        killed = []
        try:
            import psutil
        except ImportError:
            log_fn("psutil not installed — skipping stale-process cleanup (pip install psutil to enable)")
            return killed

        targets = [
            (BOT_SCRIPT_PATH, None),
            (DASHBOARD_SCRIPT_PATH, "--watch"),
            (DASHBOARD_SERVER_SCRIPT_PATH, None),
            (BACKUP_SCRIPT_PATH, "watch"),
        ]
        my_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.info["pid"] == my_pid:
                    continue
                cmdline = proc.info["cmdline"] or []
                cmdline_str = " ".join(cmdline)
                matched = False
                for script_path, required_arg in targets:
                    if script_path and script_path in cmdline_str:
                        if required_arg and required_arg not in cmdline_str:
                            continue
                        matched = True
                        break
                if not matched and CLOUDFLARED_EXE.lower() in cmdline_str.lower() and CLOUDFLARED_TUNNEL_NAME in cmdline_str:
                    matched = True
                if matched:
                    log_fn(f"Killing stale process PID {proc.info['pid']}: {cmdline_str}")
                    try:
                        proc.kill()
                        killed.append((proc.info["pid"], cmdline_str))
                    except Exception as e:
                        log_fn(f"  could not kill PID {proc.info['pid']}: {e}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return killed

    def force_kill_stale(self):
        if not messagebox.askyesno(
            "Force Kill Stale Processes",
            "ยืนยันการค้นหาและปิดโปรเซสที่หลงเหลือทั้งหมด?\n"
            "(EA / Dashboard --watch / Web Server / Cloudflare Tunnel / Backup Watch)\n\n"
            "MT5 terminal.exe จะไม่ถูกปิด",
        ):
            return
        killed = self.kill_stale_processes(log_fn=lambda msg: print(f"[force_kill] {msg}"))
        if killed:
            detail = "\n".join(f"  PID {pid}: {cmd[:100]}" for pid, cmd in killed)
            messagebox.showinfo(
                "Force Kill สำเร็จ",
                f"ปิดโปรเซสที่หลงเหลือ {len(killed)} รายการ:\n{detail}",
            )
        else:
            messagebox.showinfo("Force Kill", "ไม่พบโปรเซสที่หลงเหลือ — ระบบสะอาดอยู่แล้ว")

    # ---------- field helpers ----------
    def _get_nested(self, section, field):
        """Resolves a (possibly dotted, e.g. 'ema_cross.fast') field path
        within self.config_data[section]."""
        val = self.config_data[section]
        for part in field.split("."):
            val = val[part]
        return val

    def reg_bool(self, parent, section, field, label, row, col=0):
        val = self._get_nested(section, field)
        var = tk.BooleanVar(value=val)
        self.vars[f"{section}.{field}"] = var
        cb = ttk.Checkbutton(parent, text=label, variable=var)
        cb.grid(row=row, column=col, sticky="w", padx=4, pady=2)
        return var

    def reg_entry(self, parent, section, field, label, row, col=0, width=10, numeric_type=float, show=None):
        val = self._get_nested(section, field)
        var = tk.StringVar(value=str(val) if val != "" else "")
        self.vars[f"{section}.{field}"] = _TypedStringVar(var, numeric_type)
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=4, pady=2)
        e = ttk.Entry(parent, textvariable=var, width=width, show=show or "")
        e.grid(row=row, column=col + 1, sticky="w", padx=4, pady=2)
        return var

    def reg_combo(self, parent, section, field, label, options, row, col=0, editable=False):
        val = self._get_nested(section, field)
        var = tk.StringVar(value=val)
        self.vars[f"{section}.{field}"] = var
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=4, pady=2)
        # editable=True lets the user type a custom value (e.g. a broker-specific
        # symbol suffix like "XAUUSDm") instead of being locked to the dropdown list
        cb = ttk.Combobox(parent, textvariable=var, values=options, width=12,
                           state="normal" if editable else "readonly")
        cb.grid(row=row, column=col + 1, sticky="w", padx=4, pady=2)
        return var

    # ---------- tab builders ----------
    def build_confluence_tab(self, parent):
        top = ttk.LabelFrame(parent, text="Bot วิเคราะห์ 24 กลยุทธ์พร้อมกัน — Entry Mode & Confluence Gate")
        top.pack(fill="x", padx=12, pady=12)
        s = "confluence"
        ttk.Label(
            top,
            text="ทุก ๆ Scan interval วินาที บอทจะให้คะแนน 0-100% แต่ละกลยุทธ์ทั้งฝั่ง Long/Short แยกกัน\n"
                 "จะเข้าออเดอร์ก็ต่อเมื่อ (1) คะแนนรวม (ถ่วงน้ำหนัก) ของฝั่งนั้น >= เกณฑ์ที่ตั้งไว้ AND\n"
                 "(2) มีจำนวนกลยุทธ์ที่ \"เห็นตรงกัน\" (คะแนน >= 50%) ฝั่งนั้น >= จำนวนขั้นต่ำที่ตั้งไว้\n"
                 "(ต้องมีหลายกลยุทธ์เห็นพ้อง — confluence — ไม่ใช่กลยุทธ์เดียวข้ามเกณฑ์ก็เข้าได้เลย)\n"
                 "รวม 24 กลยุทธ์ = 13 กลยุทธ์เดิม + 5 กลยุทธ์จากระบบ v1 ที่ยังไม่ซ้ำกัน "
                 "(MACD Cross, Bollinger Breakout, S/R Breakout+Retest, Price Action, ATR/Donchian Breakout) "
                 "+ Order Flow (DOM) + Macro Bias (Big Data) + 4 กลยุทธ์ Scalping (#21-24)\n"
                 "อีก 5 กลยุทธ์ของ v1 (EMA Cross, RSI Divergence, Fibonacci, MTF Alignment, News Momentum) "
                 "ซ้ำกับกลยุทธ์ในชุด 13 อยู่แล้ว จึงไม่เพิ่มซ้ำ — ไปปรับน้ำหนัก/เปิดปิดที่รายการด้านล่างแทน\n"
                 "🩳 กลยุทธ์ Scalping (#21-24) ใช้กราฟ M1/M5 (เร็วกว่ากลยุทธ์อื่นที่ใช้ H1/H4/D1) — "
                 "เหมาะกับการเข้า-ออกไม้เร็ว SL/TP แคบ ($3-30) ทำงานเฉพาะช่วงเวลาที่กำหนด "
                 "(London Open / NY Session) ยกเว้น EMA Pullback ที่ทำงานได้ทั้งวันถ้าเทรนด์ M1 เรียงตัวชัด "
                 "#24 (EMA20+EMA50+Liquidity Sweep) คือสูตรที่แนะนำที่สุดเพราะต้องผ่านเงื่อนไขครบ 4 ชั้น "
                 "(H1 trend + M5 EMA filter + sweep + reclaim) จึงคัดกรองเข้มกว่ากลยุทธ์ scalping เดี่ยวๆ\n"
                 "⚠️ การจัดการความเสี่ยงสำหรับ Scalping ที่แนะนำ (ตั้งค่าได้ในแท็บ Risk & Basket Close): "
                 "เสี่ยงไม่เกิน 0.5-1% ต่อไม้ (RISK_PER_TRADE), ตั้ง SL ทุกครั้ง, จำกัดไม่เกิน ~3 ไม้ต่อช่วงเวลา "
                 "(MAX_DAILY_TRADES) และหยุดเทรดทันทีถ้าขาดทุนติดกัน 2 ไม้ (MAX_CONSECUTIVE_LOSSES=2)\n"
                 "Order Flow (DOM) อ่านข้อมูล Depth of Market จริงจาก MT5 (mt5.market_book_get) — ไม่ใช่ "
                 "Order Flow Footprint / FXSSI Order Book / Order Block Flow Elite ของผู้ให้บริการภายนอก "
                 "เพราะ API ของ MT5 ดึงข้อมูลแบบนั้นไม่ได้ตรงๆ ถ้าโบรกเกอร์/symbol ไม่รองรับ DOM กลยุทธ์นี้จะให้ "
                 "คะแนน 0/0 เฉยๆ ไม่กระทบกลยุทธ์อื่น ส่วน Order Block (ICT) เดิม (#1) ก็ได้ bonus คะแนนเพิ่มเวลา "
                 "DOM ยืนยันทิศทางเดียวกันด้วย\n"
                 "Macro Bias (Big Data) ใช้ข้อมูลจริงจากเว็บ (DXY, US10Y Yield, CFTC COT Report, "
                 "CME COMEX คลังทอง, ปฏิทินข่าว ForexFactory) เช็คตามสูตร 6 ข้อที่กองทุนใช้จริง — "
                 "ข้อมูลอัปเดตไม่บ่อยเท่ากลยุทธ์อื่น (COT รายสัปดาห์, ที่เหลือราย 3-6 ชม.) ดู macro_data.py "
                 "ถ้าโหลดข้อมูลไม่สำเร็จ (เช่น ตอนเพิ่งเริ่มรันบอท) กลยุทธ์นี้จะให้คะแนน 0/0 เฉยๆ ไม่กระทบกลยุทธ์อื่น",
            wraplength=680, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_combo(top, s, "entry_mode", "Entry mode:", ["confluence13", "logic_groups", "legacy"], row=1)
        self.reg_entry(top, s, "scan_interval_seconds", "Scan interval (วินาที):", row=1, col=2, numeric_type=int)
        self.reg_entry(top, s, "min_strategy_score", "เกณฑ์คะแนนรวมขั้นต่ำ (%) [แนะนำ 70]:", row=2, numeric_type=float)
        self.reg_entry(top, s, "min_agreeing_strategies", "จำนวนกลยุทธ์ที่ต้องเห็นตรงกันขั้นต่ำ:", row=2, col=2, numeric_type=int)
        self.reg_entry(top, s, "sl_atr_mult", "SL = ATR x:", row=3, numeric_type=float)
        self.reg_entry(top, s, "tp_rr", "TP (R:R multiple):", row=3, col=2, numeric_type=float)

        list_frame = ttk.LabelFrame(parent, text="เปิด/ปิด และถ่วงน้ำหนัก (weight) แต่ละกลยุทธ์")
        list_frame.pack(fill="x", padx=12, pady=12)
        r = 0
        for key, label in STRATEGY13_LABELS.items():
            self.reg_bool(list_frame, s, f"strategies.{key}.enabled", label, row=r, col=0)
            self.reg_entry(list_frame, s, f"strategies.{key}.weight", "weight:", row=r, col=2, width=6, numeric_type=float)
            r += 1

    def build_logic_groups_tab(self, parent):
        top = ttk.LabelFrame(parent, text="Logic Groups — แยกตรรกะเข้าออเดอร์เป็น 2 กลุ่ม (ทางเลือกใหม่แทน Confluence 24)")
        top.pack(fill="x", padx=12, pady=12)
        s = "confluence"
        ttk.Label(
            top,
            text="ต้องตั้ง Entry mode = \"logic_groups\" ในแท็บ \"24 กลยุทธ์ (Confluence)\" ก่อน ค่านี้จึงจะมีผล\n"
                 "แต่ละกลุ่มทำงาน 2 ขั้นตอนเหมือนกัน:\n"
                 "  ขั้น 1 (Trend Filter / Bias) — ไล่เช็คกลยุทธ์ตามลำดับความสำคัญที่กำหนด ตัวแรกที่ให้สัญญาณ\n"
                 "         ชัดเจน (Long หรือ Short) จะเป็นตัวกำหนด \"อคติ\" (bias) ของกลุ่มทั้งหมด ถ้าไล่จนหมด\n"
                 "         รายการแล้วไม่มีตัวไหนชัดเจน กลุ่มนี้จะไม่เข้าออเดอร์เลยทั้ง 2 ทิศทาง (ป้องกันเข้าสวนเทรนด์)\n"
                 "  ขั้น 2 (เลือกกลยุทธ์เข้าออเดอร์) — ในกลุ่มกลยุทธ์ของกลุ่มนี้ ถ้ามีหลายตัวให้สัญญาณทิศทาง\n"
                 "         เดียวกับ bias พร้อมกัน ระบบจะเลือก \"ตัวเดียว\" ที่จะใช้เข้าออเดอร์จริง โดยให้ความสำคัญ\n"
                 "         ตามสถานะ League System ก่อน (Auto-Weight สูงสุด → win-rate ล่าสุดสูงสุด → คะแนนสูงสุด)\n"
                 "ถ้าเลือก \"both\" และทั้ง 2 กลุ่มให้สัญญาณพร้อมกันในรอบสแกนเดียวกัน ระบบจะเลือกกลุ่มที่กลยุทธ์ที่ถูก\n"
                 "เลือกมีสถานะ League ดีกว่าเป็นตัวที่เข้าออเดอร์",
            wraplength=700, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_combo(
            top, s, "logic_group_selection", "เปิดใช้กลุ่มไหน:",
            ["day_trade", "scalping", "both"], row=1,
        )
        self.reg_bool(
            top, s, "logic_groups_apply_daily_filter",
            "ใช้ Daily Filter (D1 EMA veto) ซ้อนทับ Trend Filter ของแต่ละกลุ่มด้วย (ค่าเริ่มต้น: ปิด — "
            "เพราะแต่ละกลุ่มมี Trend Filter ของตัวเองอยู่แล้ว เปิดซ้อนทั้ง 2 ตัวจะเข้มเกินไปจนมักไม่เข้าออเดอร์เลย)",
            row=2,
        )

        day_frame = ttk.LabelFrame(parent, text="กลุ่ม 1: Day Trade")
        day_frame.pack(fill="x", padx=12, pady=12)
        ttk.Label(
            day_frame,
            text="Trend Filter ลำดับความสำคัญ: Macro Bias (Big Data) → Multi-TF Align → News Fade → "
                 "Order Flow (DOM) → Supply & Demand\n"
                 "กลุ่มกลยุทธ์เข้าออเดอร์ (16 กลยุทธ์): Bollinger Band Breakout, MACD Signal Cross, "
                 "Opening Range Breakout, Price Action Candlestick, VWAP Rejection, RSI Divergence, "
                 "ATR/Donchian Breakout, Fair Value Gap, Fibonacci, Multi-TF Align, News Fade, "
                 "Scalping: EMA Pullback (M1), EMA Cross, Liquidity Sweep, BOS/CHoCH, Order Block (ICT)\n"
                 "(แก้ไขรายการนี้ได้ใน xauusd_mt5_strategy.py → DAY_TRADE_BIAS_PRIORITY / DAY_TRADE_STRATEGIES — "
                 "เปิด/ปิดและถ่วงน้ำหนักแต่ละกลยุทธ์ยังใช้ค่าจากแท็บ \"24 กลยุทธ์ (Confluence)\" ตามเดิม)",
            wraplength=700, justify="left",
        ).pack(fill="x", padx=8, pady=4, anchor="w")

        scalp_frame = ttk.LabelFrame(parent, text="กลุ่ม 2: Scalping Trade")
        scalp_frame.pack(fill="x", padx=12, pady=12)
        ttk.Label(
            scalp_frame,
            text="Trend Filter ลำดับความสำคัญ: Macro Bias (Big Data) → Order Flow (DOM) → Supply & Demand → "
                 "S/R Breakout + Retest\n"
                 "กลุ่มกลยุทธ์เข้าออเดอร์ (5 กลยุทธ์): Scalping: EMA Pullback (M1), London Breakout, "
                 "Scalping: EMA20+EMA50+Liquidity Sweep, Scalping: NY Session Breakout, "
                 "Scalping: London Open Liquidity Sweep\n"
                 "(แก้ไขรายการนี้ได้ใน xauusd_mt5_strategy.py → SCALP_BIAS_PRIORITY / SCALP_STRATEGIES)",
            wraplength=700, justify="left",
        ).pack(fill="x", padx=8, pady=4, anchor="w")

    def build_league_tab(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="League System — พักกลยุทธ์ที่แพ้บ่อยชั่วคราว (ยังโชว์คะแนนอยู่ แต่ไม่ถูกนับใน confluence)",
        )
        frame.pack(fill="x", padx=12, pady=12)
        s = "league"
        self.reg_bool(frame, s, "enabled", "เปิดใช้ League System", row=0)
        ttk.Label(
            frame,
            text="กลยุทธ์จะถูกพัก (bench) เมื่อเข้าเงื่อนไขใดเงื่อนไขหนึ่งต่อไปนี้:\n"
                 "  1) แพ้ติดต่อกันครบจำนวนที่กำหนด หรือ\n"
                 "  2) อัตราชนะ (win-rate) ย้อนหลัง N ไม้ ต่ำกว่าเกณฑ์ขั้นต่ำที่กำหนด",
            wraplength=680, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_entry(frame, s, "max_consecutive_losses", "แพ้ติดต่อกัน (ไม้) ก่อนถูกพัก:", row=2, numeric_type=int)
        self.reg_entry(frame, s, "min_winrate_pct", "Win-rate ขั้นต่ำ (%) ก่อนถูกพัก:", row=2, col=2, numeric_type=float)
        self.reg_entry(frame, s, "winrate_lookback_trades", "คำนวณ win-rate จากไม้ย้อนหลังกี่ไม้:", row=3, numeric_type=int)
        self.reg_entry(frame, s, "bench_hours", "พักนานกี่ชั่วโมง:", row=3, col=2, numeric_type=float)

        ttk.Separator(frame, orient="horizontal").grid(
            row=4, column=0, columnspan=4, sticky="ew", padx=4, pady=10
        )
        ttk.Label(
            frame,
            text="ML Auto-Adjust (เพิ่มเติมจากด้านบน) — บอทจะเปิด \"ไม้จำลอง\" (paper trade, ไม่มีความเสี่ยง\n"
                 "จริง) ให้ทุกกลยุทธ์ทุกรอบสแกน เพื่อเก็บผลแพ้/ชนะต่อเนื่อง แล้วใช้ผลรวม (ไม้จริง + ไม้จำลอง)\n"
                 "ปรับ \"คะแนนน้ำหนัก\" ของกลยุทธ์นั้นแบบต่อเนื่องอัตโนมัติ:\n"
                 "  • win-rate ต่ำกว่าเกณฑ์ → ลดน้ำหนักลงเรื่อย ๆ (ใกล้ 0% win-rate = ปิดแทบสนิท)\n"
                 "  • win-rate กลับมา ≥ เกณฑ์ → คืนน้ำหนักเต็มทันที ไม่ต้องรอครบเวลาพัก",
            wraplength=680, justify="left",
        ).grid(row=5, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_bool(frame, s, "shadow_simulation_enabled", "เปิดใช้ Shadow Simulation (ไม้จำลอง)", row=6)
        self.reg_entry(frame, s, "min_samples_for_adjustment", "ต้องมีผลไม้ขั้นต่ำกี่ไม้ก่อนเริ่มปรับน้ำหนัก:",
                        row=7, numeric_type=int)

    def build_telegram_tab(self, parent):
        frame = ttk.LabelFrame(parent, text="Telegram Alert — แจ้งเตือนทุกครั้งที่มีออเดอร์ใหม่/ปิดออเดอร์")
        frame.pack(fill="x", padx=12, pady=12)
        s = "telegram"
        self.reg_bool(frame, s, "enabled", "เปิดใช้ Telegram Alert", row=0)
        ttk.Label(
            frame,
            text="กรอก Bot Token และ Chat ID ของคุณเองที่นี่ (หรือแก้ไขตรงในไฟล์ strategy_config.json ก็ได้)\n"
                 "วิธีขอ Token: คุยกับ @BotFather บน Telegram แล้วสั่ง /newbot\n"
                 "วิธีหา Chat ID: ส่งข้อความให้บอทของคุณ 1 ครั้ง แล้วเปิดเบราว์เซอร์ไปที่\n"
                 "https://api.telegram.org/bot<TOKEN>/getUpdates แล้วดูเลข \"chat\":{\"id\": ...}\n"
                 "⚠️ อย่าแชร์ Token/Chat ID นี้ให้ใครเห็น (รวมถึงไม่ต้องพิมพ์ใส่ในแชทกับ Claude)",
            wraplength=680, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_entry(frame, s, "bot_token", "Bot Token:", row=2, numeric_type=str, width=40)
        self.reg_entry(frame, s, "chat_id", "Chat ID:", row=3, numeric_type=str, width=20)

    def build_myfxbook_tab(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="Myfxbook Retail Sentiment — กลยุทธ์ที่ 25 (Community Outlook)",
        )
        frame.pack(fill="x", padx=12, pady=12)
        s = "myfxbook"
        self.reg_bool(frame, s, "enabled", "เปิดใช้ Myfxbook Sentiment", row=0)
        ttk.Label(
            frame,
            text="กรอกอีเมล/รหัสผ่านบัญชี Myfxbook ของคุณเอง (สมัครฟรีที่ myfxbook.com) เพื่อดึงข้อมูล\n"
                 "% นักเทรดรายย่อย Long/Short ของ XAUUSD มาเป็นอีก 1 เสียงโหวตใน Confluence engine\n"
                 "(ไม่ใช่การคัดลอกกลยุทธ์ของใคร — เป็นข้อมูล sentiment สาธารณะเท่านั้น)\n"
                 "⚠️ อย่าแชร์อีเมล/รหัสผ่านนี้ให้ใครเห็น (รวมถึงไม่ต้องพิมพ์ใส่ในแชทกับ Claude)\n"
                 "ระบบจะส่งไปที่ myfxbook.com โดยตรงเท่านั้น และเก็บแค่ session token ในเครื่อง ไม่เก็บรหัสผ่าน",
            wraplength=680, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_entry(frame, s, "email", "Myfxbook Email:", row=2, numeric_type=str, width=30)
        self.reg_entry(frame, s, "password", "Myfxbook Password:", row=3, numeric_type=str, width=20, show="*")
        self.reg_bool(
            frame, s, "contrarian",
            "Contrarian (Fade the crowd) — ติ๊กออกถ้าต้องการแบบ Trend-following (ตามฝูงชน) แทน",
            row=4,
        )

    def build_dashboard_auth_tab(self, parent):
        frame = ttk.LabelFrame(parent, text="ตั้งรหัสผ่านสำหรับเปิด Dashboard ผ่านอินเทอร์เน็ต (dashboard_server.py)")
        frame.pack(fill="x", padx=12, pady=12)
        s = "dashboard_auth"
        ttk.Label(
            frame,
            text="Dashboard โชว์ยอดเงิน/ออเดอร์จริงของคุณ — ตั้ง username/password ที่นี่ก่อนเปิดให้ดูผ่าน\n"
                 "อินเทอร์เน็ต (เช่นผ่าน dashboard.sereewifi.net) มิฉะนั้น dashboard_server.py จะไม่ยอมรันให้\n"
                 "เมื่อตั้งแล้ว กด Save Config แล้วกด Start Bot — ระบบจะรัน dashboard_server.py ให้อัตโนมัติ",
            wraplength=680, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_entry(frame, s, "username", "Username:", row=1, numeric_type=str, width=20)
        self.reg_entry(frame, s, "password", "Password:", row=2, numeric_type=str, width=20, show="*")

    def build_strategies_tab(self, parent):
        note = ttk.Label(
            parent,
            text="หน้านี้มีผลเฉพาะตอน entry_mode = \"legacy\" เท่านั้น (ดูแท็บ \"24 กลยุทธ์ (Confluence)\")\n"
                 "5 ใน 10 กลยุทธ์นี้ (MACD Cross, BB Breakout, S/R Breakout+Retest, Price Action, "
                 "ATR/Donchian) ถูกรวมเข้าไปในชุด 24 กลยุทธ์ของ Confluence แล้ว — ตั้งค่าที่นี่จะไม่มีผล "
                 "ถ้า entry_mode = \"confluence13\" (ค่า default)",
            wraplength=700, justify="left",
        )
        note.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        parent.grid_columnconfigure(0, weight=1)
        r = 1
        for key, label in STRATEGY_LABELS.items():
            frame = ttk.LabelFrame(parent, text=label)
            frame.grid(row=r, column=0, sticky="ew", padx=6, pady=6)
            parent.grid_columnconfigure(0, weight=1)
            self.reg_bool(frame, "strategies", f"{key}.enabled", "Enabled", row=0)
            self.build_strategy_params(frame, key)
            r += 1

    def build_strategy_params(self, frame, key):
        s = "strategies"
        if key == "ema_cross":
            self.reg_entry(frame, s, "ema_cross.fast", "Fast EMA period:", row=1, numeric_type=int)
            self.reg_entry(frame, s, "ema_cross.slow", "Slow EMA period:", row=1, col=2, numeric_type=int)
        elif key == "macd_cross":
            self.reg_entry(frame, s, "macd_cross.fast", "Fast:", row=1, numeric_type=int)
            self.reg_entry(frame, s, "macd_cross.slow", "Slow:", row=1, col=2, numeric_type=int)
            self.reg_entry(frame, s, "macd_cross.signal", "Signal:", row=1, col=4, numeric_type=int)
        elif key == "rsi_divergence":
            self.reg_entry(frame, s, "rsi_divergence.period", "RSI period:", row=1, numeric_type=int)
            self.reg_entry(frame, s, "rsi_divergence.oversold", "Oversold:", row=1, col=2, numeric_type=int)
            self.reg_entry(frame, s, "rsi_divergence.overbought", "Overbought:", row=1, col=4, numeric_type=int)
            self.reg_entry(frame, s, "rsi_divergence.lookback", "Divergence lookback (bars):", row=2, numeric_type=int)
        elif key == "fib_confluence":
            self.reg_entry(frame, s, "fib_confluence.swing_lookback", "Swing lookback (bars):", row=1, numeric_type=int)
            self.reg_entry(frame, s, "fib_confluence.fib_low", "Fib zone low (%):", row=1, col=2)
            self.reg_entry(frame, s, "fib_confluence.fib_high", "Fib zone high (%):", row=1, col=4)
            self.reg_entry(frame, s, "fib_confluence.rsi_low", "RSI filter low:", row=2, numeric_type=int)
            self.reg_entry(frame, s, "fib_confluence.rsi_high", "RSI filter high:", row=2, col=2, numeric_type=int)
            self.reg_bool(frame, s, "fib_confluence.use_macd_trigger", "Require MACD trigger", row=2, col=4)
        elif key == "bb_breakout":
            self.reg_entry(frame, s, "bb_breakout.period", "BB period:", row=1, numeric_type=int)
            self.reg_entry(frame, s, "bb_breakout.std", "BB std dev:", row=1, col=2)
            self.reg_entry(frame, s, "bb_breakout.squeeze_pct", "Squeeze bandwidth (%):", row=1, col=4)
        elif key == "sr_breakout_retest":
            self.reg_entry(frame, s, "sr_breakout_retest.lookback", "S/R lookback (bars):", row=1, numeric_type=int)
            self.reg_entry(frame, s, "sr_breakout_retest.retest_tol", "Retest tolerance (points):", row=1, col=2)
            self.reg_entry(frame, s, "sr_breakout_retest.breakout_buffer", "Breakout buffer (points):", row=1, col=4)
        elif key == "price_action":
            self.reg_bool(frame, s, "price_action.pin_bar", "Pin bar", row=1)
            self.reg_bool(frame, s, "price_action.engulfing", "Engulfing", row=1, col=2)
            self.reg_entry(frame, s, "price_action.proximity_points", "Proximity to key level (points):", row=2)
        elif key == "atr_donchian_breakout":
            self.reg_entry(frame, s, "atr_donchian_breakout.donchian_period", "Donchian period:", row=1, numeric_type=int)
            self.reg_entry(frame, s, "atr_donchian_breakout.atr_period", "ATR period:", row=1, col=2, numeric_type=int)
            self.reg_entry(frame, s, "atr_donchian_breakout.atr_mult", "ATR confirm multiplier:", row=1, col=4)
        elif key == "mtf_alignment":
            self.reg_bool(frame, s, "mtf_alignment.use_h4", "H4", row=1)
            self.reg_bool(frame, s, "mtf_alignment.use_h1", "H1", row=1, col=2)
            self.reg_bool(frame, s, "mtf_alignment.use_m15", "M15", row=1, col=4)
            self.reg_entry(frame, s, "mtf_alignment.ema_period", "EMA period for alignment:", row=2, numeric_type=int)
        elif key == "news_momentum":
            self.reg_entry(frame, s, "news_momentum.avoid_minutes", "Avoid window (minutes around news):", row=1, numeric_type=int)
            self.reg_entry(frame, s, "news_momentum.momentum_atr_mult", "Momentum ATR multiplier:", row=1, col=2)

    def build_daily_tab(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="ป้องกันออกออเดอร์ผิดฝั่งของ Day Timeframe (10/20/50/100/200 EMA + RSI)",
        )
        frame.pack(fill="x", padx=12, pady=12)
        s = "daily_filter"
        self.reg_bool(frame, s, "enabled", "Enabled — block entries against the Day trend", row=0)
        ttk.Label(
            frame,
            text="Long blocked if Day RSI(14) >= Overbought. Short blocked if Day RSI(14) <= Oversold.\n"
                 "Trades are also blocked outright when Day price/EMA50/EMA200 don't agree (neutral/choppy Day chart).",
            wraplength=620, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 6))
        self.reg_entry(frame, s, "rsi_overbought", "RSI overbought level:", row=2)
        self.reg_entry(frame, s, "rsi_oversold", "RSI oversold level:", row=2, col=2)
        self.reg_bool(frame, s, "require_full_stack",
                      "Strict mode: require full 10>20>50>100>200 EMA stack alignment", row=3)

    def build_trading_hours_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=12, pady=(12, 4))
        self.reg_bool(top, "trading_hours", "enabled",
                      "เปิดใช้ตัวกรองช่วงเวลาเทรด (ปิด = เทรดได้ตลอด 24 ชม.)", row=0)
        ttk.Label(
            top,
            text="เวลาที่ใช้คือเวลาของคอมพิวเตอร์/เซิร์ฟเวอร์ที่รันสคริปต์นี้ — ต้องตั้งนาฬิกาเป็นเวลาไทย (UTC+7)\n"
                 "เลือกได้มากกว่า 1 ช่วง: ระบบจะอนุญาตให้เปิดออเดอร์ใหม่เมื่อเวลาปัจจุบันอยู่ในช่วงที่เลือกไว้",
            wraplength=680, justify="left",
        ).grid(row=1, column=0, sticky="w", padx=4, pady=(2, 8))

        for key, info in SESSION_INFO.items():
            frame = ttk.LabelFrame(parent, text=info["title"])
            frame.pack(fill="x", padx=12, pady=6)
            self.reg_bool(frame, "trading_hours", f"sessions.{key}", "เลือกช่วงเวลานี้", row=0)
            ttk.Label(frame, text=info["desc"], wraplength=680, justify="left").grid(
                row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 6))

    def build_trailing_tab(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        s = "trailing_stop"
        self.reg_bool(frame, s, "enabled", "Trailing stop enabled", row=0)
        self.reg_combo(frame, s, "method", "Method:", ["ATR", "EMA", "FIXED_POINTS", "PERCENT"], row=1)
        self.reg_entry(frame, s, "atr_mult", "ATR multiplier:", row=2)
        self.reg_entry(frame, s, "fixed_points", "Fixed points distance:", row=3)
        self.reg_entry(frame, s, "percent", "Percent of price (%):", row=4)
        self.reg_entry(frame, s, "ema_period", "EMA period:", row=5, numeric_type=int)
        self.reg_entry(frame, s, "ema_buffer_points", "EMA buffer (points):", row=6)
        self.reg_entry(frame, s, "activation_r", "Activation (R multiple):", row=7)
        self.reg_bool(frame, s, "remove_tp_on_activate",
                      "ตัด TP ทิ้งเมื่อ Trailing เริ่มทำงาน (ปล่อยให้กำไรวิ่งยาวตาม Trailing SL อย่างเดียว)",
                      row=8)
        ttk.Label(
            frame,
            text="ค่า default = ปิด (False) เหมือน v1 เดิม: TP2 จะยังฝากไว้ที่โบรกเกอร์ตลอด ไม้จะปิดทันทีที่ราคาแตะ TP2 "
                 "แม้ Trailing SL จะตามมาคุ้มครองกำไรไปด้วยก็ตาม\n"
                 "ถ้าเปิด (True): พอราคาวิ่งถึงจุด Activation (R multiple) แล้ว ระบบจะถอด TP ออกจากโบรกเกอร์ "
                 "แล้วให้ Trailing SL เป็นตัวปิดไม้เพียงอย่างเดียว (ปล่อยกำไรวิ่งต่อได้ไม่จำกัดที่ TP2)\n"
                 "ตั้งค่านี้ที่นี่เพียงครั้งเดียว มีผลทั้งไม้ legacy (10 กลยุทธ์เดิม) และไม้ confluence18 เหมือนกัน "
                 "เพราะ Trailing Stop เป็นระบบกลางที่จัดการทุก position ของ EA นี้โดยไม่สนใจว่าเปิดมาจาก mode ไหน",
            wraplength=700, justify="left",
        ).grid(row=9, column=0, columnspan=4, sticky="w", padx=4, pady=(2, 10))
        self.reg_entry(frame, s, "check_seconds", "Check interval (seconds):", row=10, numeric_type=int)

    def build_risk_tab(self, parent):
        at_frame = ttk.LabelFrame(parent, text="⚠ Auto Trade Mode — เปิด/ปิดการส่งออเดอร์จริง")
        at_frame.pack(fill="x", padx=12, pady=12)
        at = "risk"
        self.reg_bool(
            at_frame, at, "auto_trade",
            "เปิด Auto Trade — บอทจะส่งออเดอร์จริงเข้าตลาดทันทีที่มีสัญญาณ "
            "(ปิดไว้ = Signal Only โหมดทดสอบ ไม่ส่งออเดอร์จริง)",
            row=0,
        )
        ttk.Label(
            at_frame,
            text="คำเตือน: ทดสอบบนบัญชี Demo ให้แน่ใจก่อนเปิดใช้กับบัญชีจริง "
                 "การเปลี่ยนค่านี้แล้วกด Save จะมีผลกับบอททันทีโดยไม่ต้อง restart "
                 "(ภายใน ~30 วินาที ผ่านระบบ Live Config Reload)",
            foreground="#b35900", wraplength=560, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 6))

        frame = ttk.LabelFrame(parent, text="Risk Management")
        frame.pack(fill="x", padx=12, pady=12)
        s = "risk"
        self.reg_combo(frame, s, "symbol", "Symbol (เลือกหรือพิมพ์เองตามชื่อใน Market Watch ของโบรกเกอร์):",
                       ["GOLD", "XAUUSD"], row=0, editable=True)
        self.reg_entry(frame, s, "risk_per_trade_pct", "Risk per trade (%):", row=1)
        self.reg_entry(frame, s, "lot_step", "Lot step:", row=2)
        self.reg_entry(frame, s, "value_per_point_per_lot", "Value per $1 move per lot ($):", row=3)
        self.reg_entry(frame, s, "max_concurrent_trades", "Max concurrent trades:", row=4, numeric_type=int)

        mframe = ttk.LabelFrame(parent, text="Money Management (min/max lot + daily safety limits)")
        mframe.pack(fill="x", padx=12, pady=12)
        m = "money_management"
        self.reg_entry(mframe, m, "min_lot", "Min lot size:", row=0)
        self.reg_entry(mframe, m, "max_lot", "Max lot size:", row=0, col=2)
        self.reg_bool(mframe, m, "enforce_min_lot",
                      "If risk-based size < Min lot, use Min lot anyway (else skip trade)", row=1, col=0)
        self.reg_entry(mframe, m, "max_daily_trades", "Max new trades per day (blank = unlimited):", row=2, numeric_type=str)
        self.reg_entry(mframe, m, "max_drawdown_pct", "Stop new entries at equity drawdown (% from session peak):", row=3, numeric_type=str)
        self.reg_entry(mframe, m, "daily_loss_limit_r", "Stop new entries at daily loss (R multiples, blank = disabled):", row=4, numeric_type=str)
        self.reg_entry(mframe, m, "min_risk_reward_ratio",
                        "Min R:R required to take a setup (e.g. 1.5 = R:R 1:1.5, blank = disabled):", row=5, numeric_type=str)
        self.reg_entry(mframe, m, "max_consecutive_losses",
                        "Anti-Martingale: stop for the day after this many losses in a row (blank = disabled):", row=6, numeric_type=str)

        beframe = ttk.LabelFrame(parent, text="Breakeven Stop Loss (auto-move SL to entry once profit reaches X * risk)")
        beframe.pack(fill="x", padx=12, pady=12)
        be = "breakeven"
        self.reg_bool(beframe, be, "enabled", "เปิดใช้ Breakeven SL", row=0)
        self.reg_entry(beframe, be, "trigger_r", "เลื่อน SL มาเท่าทุนเมื่อกำไรถึง (R multiple):", row=1)
        self.reg_entry(beframe, be, "buffer_points", "บัฟเฟอร์เหนือ/ใต้จุดเข้า (points):", row=1, col=2)

        bframe = ttk.LabelFrame(parent, text="Basket Close (closes ALL open positions together)")
        bframe.pack(fill="x", padx=12, pady=12)
        b = "basket_close"
        self.reg_bool(bframe, b, "enabled", "Basket close enabled", row=0)
        self.reg_entry(bframe, b, "target_profit_usd", "Target profit ($):", row=1, numeric_type=str)
        self.reg_entry(bframe, b, "max_loss_usd", "Max loss ($):", row=2, numeric_type=str)
        self.reg_entry(bframe, b, "target_profit_pct", "Target profit (% of balance):", row=3, numeric_type=str)
        self.reg_entry(bframe, b, "max_loss_pct", "Max loss (% of balance):", row=4, numeric_type=str)


    def build_logging_tab(self, parent):
        frame = ttk.LabelFrame(parent, text="ระบบ Log สำหรับตรวจสอบและ Debug")
        frame.pack(fill="x", padx=12, pady=12)
        s = "logging"
        ttk.Label(
            frame,
            text="EA จะเขียน log ทุกเหตุการณ์ (เชื่อมต่อ MT5, สัญญาณเข้าออเดอร์, เกตของ MM ที่บล็อกออเดอร์,\n"
                 "ผลลัพธ์การส่งออเดอร์, trailing stop, basket close, error) ลงไฟล์ที่หมุนรอบอัตโนมัติ\n"
                 "(rotating log) ในโฟลเดอร์ที่กำหนดไว้ ข้างใต้โฟลเดอร์โปรเจกต์",
            wraplength=680, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_combo(frame, s, "level", "Log level:",
                        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], row=1)
        self.reg_bool(frame, s, "log_to_console", "แสดง log บนหน้าจอ/console ด้วย", row=2)
        self.reg_entry(frame, s, "log_dir", "โฟลเดอร์ log (relative to script):", row=3, numeric_type=str, width=20)
        self.reg_entry(frame, s, "max_bytes", "ขนาดไฟล์สูงสุดก่อนหมุนรอบ (bytes):", row=4, numeric_type=int)
        self.reg_entry(frame, s, "backup_count", "จำนวนไฟล์ log เก่าที่เก็บไว้:", row=5, numeric_type=int)

        eframe = ttk.LabelFrame(parent, text="Stop on Error — หยุดบอททันทีถ้าเจอ error (ปลอดภัยกว่าแต่ต้องมาเปิดเอง)")
        eframe.pack(fill="x", padx=12, pady=12)
        e = "error_handling"
        ttk.Label(
            eframe,
            text="ค่าเริ่มต้น (ปิด): ถ้า error เกิดขึ้นใน 1 รอบสแกน บอทจะ log ไว้แล้ว \"รันต่อ\" เพราะ error "
                 "ส่วนใหญ่เป็นแค่เน็ตสะดุด/MT5 หลุดชั่วคราว — ไม้ที่เปิดอยู่แล้วจะยังถูกดูแล trailing stop ต่อปกติ\n"
                 "ถ้าเปิดตัวเลือกนี้: พอ error ครบจำนวนที่กำหนด บอทจะส่ง Telegram แจ้งเตือน (ถ้าเปิดใช้) แล้ว"
                 "หยุดทำงานทั้งหมดทันที (ต้องมารันใหม่เอง) — ไม้ที่เปิดอยู่จะไม่มีใครดูแล trailing stop/basket close ต่อ "
                 "จนกว่าจะรันบอทใหม่",
            wraplength=680, justify="left",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        self.reg_bool(eframe, e, "stop_on_error", "หยุดบอททันทีเมื่อเจอ error (ไม่ใช่แค่ log แล้วรันต่อ)", row=1)
        self.reg_entry(eframe, e, "max_errors_before_stop",
                        "จำนวน error ติดต่อกันก่อนหยุด (1 = หยุดทันทีตั้งแต่ error แรก):", row=2, numeric_type=int)


class _TypedStringVar:
    """Wraps a tk.StringVar so .get() returns a typed Python value (int/float/str)
    while the Entry widget itself still works on text."""
    def __init__(self, str_var, py_type):
        self.str_var = str_var
        self.py_type = py_type

    def set(self, value):
        self.str_var.set(str(value) if value not in (None, "") else "")

    def get(self):
        raw = self.str_var.get().strip()
        if raw == "":
            return "" if self.py_type is str else None
        if self.py_type is str:
            return raw
        try:
            return self.py_type(raw)
        except ValueError:
            return raw  # leave as-is; save() will surface it, user can fix


if __name__ == "__main__":
    App().mainloop()
