# Hardware Requirements — XAUUSD/GOLD Confluence Trading Bot

This bot is light on CPU/RAM. Its actual workload (from the code):
confluence scan + trailing-stop check every **30 seconds**, macro/Big Data
refresh every **6 hours**, plus periodic Telegram alerts and dashboard
generation. Nothing here is GPU-bound or CPU-heavy — what matters most for
live trading is **uptime** and **network latency to your broker**, not raw
horsepower.

---

## 1. PC (development / manual testing — not running 24/7)

Use this for building, testing config changes, and checking the dashboard
locally. Not meant for unattended overnight trading.

| Component | Minimum | Recommended |
|---|---|---|
| OS | Windows 10/11 64-bit (MT5 desktop requires Windows, or Wine on Mac/Linux) | Windows 11 64-bit |
| CPU | Dual-core 2.0GHz+ | Quad-core (i5 / Ryzen 5) |
| RAM | 4 GB | 8 GB |
| Storage | 20 GB free | 20 GB free SSD |
| Internet | Stable broadband | Stable broadband, low latency |

---

## 2. VPS (production — 24/7 live trading)

This is the one that matters. Any downtime here means open positions go
unmanaged — no trailing stop, no breakeven, no basket close — until the VPS
is back up.

| Component | Minimum | Recommended |
|---|---|---|
| OS | Windows Server 2019/2022 (most "Forex VPS" providers preinstall MT5 on this) | Windows Server 2022 |
| CPU | 1–2 vCPU | 2 vCPU |
| RAM | 2 GB | 4 GB |
| Storage | 40 GB SSD | 60 GB NVMe SSD |
| Bandwidth | A few Mbps is plenty — actual usage is a few MB/day | Same — don't overpay for bandwidth |
| Uptime SLA | 99.5%+ | 99.9%+ |

### What actually matters more than specs

1. **Latency to your broker's trade server.** Aim under 50ms, ideally
   under 20ms — this matters most for the scalping strategies (London Open
   Liquidity Sweep, EMA Pullback, NY Session Breakout), which scan every
   30 seconds. Check which data center your broker recommends (common ones:
   London/LD4, New York/NY4, Tokyo, Singapore) and pick a VPS in that same
   region/data center.
2. **A Forex-specialized VPS provider**, not a generic cheap VPS — these
   are built for low-latency proximity to broker servers and higher uptime
   guarantees than typical web-hosting VPS plans.
3. **SSD/NVMe storage**, not spinning disk — the bot writes state
   frequently (league results, shadow positions, live config reload,
   logs), and slow disk I/O can introduce lag between what the strategy
   decides and what gets persisted.
4. **A UPS or equivalent backup power** if you ever run this from a PC
   instead of a VPS — a power cut mid-trade is the scenario you most want
   to avoid.

---

## 3. Why specs can stay modest

- No machine learning training happens on this box — `league.py`'s
  auto-weight and `strategy_simulator.py`'s shadow trading are simple
  arithmetic over JSON-stored history, not model training/inference.
- All 24 strategies score against already-fetched OHLC/tick data — no
  heavy backtesting loop runs in production.
- The dashboard (`generate_dashboard.py` / `dashboard_server.py`) is a
  static HTML generator + a small built-in HTTP server — negligible load.
- Macro data fetches (`macro_data.py`) are infrequent (every 6 hours) and
  network-bound, not compute-bound.

If you later run multiple symbols or multiple EA instances on the same
VPS, scale RAM up (4GB → 8GB) rather than CPU — each MT5 terminal instance
is the main RAM consumer, not the Python script.
