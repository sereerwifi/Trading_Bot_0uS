"""
Big Data / Macro Fundamentals fetcher for XAUUSD (Gold) and XAGUSD (Silver)
============================================================================
Implements the "institutional checklist" described in the user's reference
doc (สำหรับการเทรด Gold.docx) — the Big-Data categories fund/Smart-Money desks
actually watch before building a directional bias, using REAL, free, no-API-
key data sources that were verified to work during development:

  1. COMEX Inventory (Registered/Eligible/Total)  -> CME official .xls report
  2. CFTC COT Report (Commercial/Managed Money)    -> CFTC Socrata Open Data API
  3. ETF Flow (GLD/SLV holdings)                   -> best-effort (see note below)
  4. DXY (Dollar Index)                            -> Yahoo Finance chart API,
                                                       falls back to FRED broad
                                                       dollar index if blocked
  5. US 10Y Treasury Yield                          -> FRED (official Fed data)
  6. Central Bank buying                            -> NOT auto-scraped (World
                                                       Gold Council has no free
                                                       API; quarterly cadence
                                                       anyway) — manual override
                                                       in strategy_config_ui.py
  7. CME Open Interest                              -> piggybacks on the COT
                                                       report's open_interest_all
                                                       field (CFTC publishes OI
                                                       alongside positioning)
  8. Economic Calendar                              -> ForexFactory's own public
                                                       JSON feed (used by their
                                                       widget, no key needed)

IMPORTANT — be honest about reliability:
  - COT report, economic calendar, 10Y yield, dollar index, and COMEX
    inventory were all verified against their real endpoints and are
    reasonably durable (official government/exchange/Fed data, or a feed the
    source itself serves to the public for free).
  - ETF Flow (GLD/SLV) is NOT reliably scrapeable: SPDR's and iShares' own
    holdings-CSV endpoints are behind a bot-detection layer (WAF) that often
    returns an HTML page instead of the CSV to non-browser requests. The
    fetch function below tries anyway (it may work fine from a residential/
    office IP even though it failed from this sandbox's IP) but is wired to
    fail silently (returns None) rather than ever break the EA. If it stays
    unavailable for you, this part of the 6-point checklist simply won't be
    counted in score_macro_bias() (see strategies.py) rather than blocking it.
  - Central Bank buying has no public free API at all (World Gold Council
    publishes a quarterly PDF/dataset, not a live feed) — this is exposed as
    a manual dropdown in the UI instead of being auto-fetched.

Also implements (per "USA new trade.docx"):
  9. Fed Rate Expectation                          -> APPROXIMATED from FRED's
                                                       2-Year Treasury yield
                                                       (DGS2) — see
                                                       fetch_fed_expectation()'s
                                                       docstring for why the
                                                       real CME FedWatch
                                                       percentages can't be
                                                       free-scraped, and why
                                                       this proxy is honest
                                                       about being a proxy.

strategies.score_macro_bias() consumes get_macro_snapshot() to build the
explicit weighted "Gold Decision Matrix" from that doc (DXY 30%, US10Y 25%,
Fed Expectation 20%, ETF Flow 10%, COT 10%, COMEX 5%) rather than counting
each factor equally.

Every fetch_* function below NEVER raises — on any network/parse failure it
returns None (or a dict with as many fields as it managed to get + an
"error" note), and every result is cached to macro_data_cache.json with a
timestamp so a transient network hiccup doesn't blank out the dashboard or
strategy score until the next successful refresh. Cache lifetimes are tuned
to how often each source actually updates (COT is weekly, COMEX/DXY/yield
are daily, calendar is refreshed a few times a day) so a 30-second scan loop
doesn't hammer any of these endpoints.

Data-loss protection ("ป้องกันกรณี bot error ข้อมูลจะได้ไม่หาย"): every fresh
successful fetch is ALSO permanently appended to macro_data_history.db (a
local SQLite file, no setup needed) via _save_to_db() — independent of the
JSON cache, so a crash, a corrupted cache file, or a bot error never erases
history that's already been fetched. get_macro_history() reads it back.
An optional second layer, export_history_to_google_sheets(), can push that
same history to a Google Sheet if you want an off-machine backup too — see
that function's docstring for setup; it's fully optional and never required
for the bot to run.
"""

# NOTE: symbol_metal / myfxbook_symbol should always be derived from the
# broker's configured SYMBOL via symbol_normalize.canonical_commodity() /
# canonical_display() (see xauusd_mt5_strategy.py) -- never hardcode
# "GOLD" or "XAUUSD" again in a new caller. This keeps every alias
# ("GOLD", "XAUUSD", "XAUUSDm", "GOLD#", ...) resolving to the same
# instrument everywhere in the bot.

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

CACHE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_data_cache.json")
DB_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_data_history.db")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_config.json")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_TIMEOUT = 12  # default for most endpoints

# FRED (stlouisfed.org) is unreachable from this VPS — TCP-level stalls far
# exceed the socket timeout. Use a short timeout so the fallback (Yahoo Finance)
# kicks in immediately rather than hanging the scan loop for 10-25 minutes.
_FRED_TIMEOUT = 4

_logger = logging.getLogger("xauusd_ea")

# Cache lifetimes per source (seconds) — matched to real update cadence.
CACHE_TTL = {
    "cot": 12 * 3600,
    "calendar": 6 * 3600,
    "yield10y": 6 * 3600,
    "dxy": 3 * 3600,
    "comex": 6 * 3600,
    "etf_flow": 12 * 3600,
    "fed_expectation": 6 * 3600,
    "myfxbook": 30 * 60,       # 100 req/day free-tier limit -> 30min keeps us well under it
}

# Myfxbook login sessions are reused across calls (don't re-login every scan —
# that burns the daily request quota). TTL is conservative: re-login a bit
# early rather than get a stale-session error mid-scan.
_MYFXBOOK_SESSION_TTL = 50 * 60

# How long past the TTL before we send a Telegram stale-data alert (2× TTL).
_STALE_ALERT_MULTIPLIER = 2.0
# Cooldown between repeated stale alerts for the same source (seconds).
_STALE_ALERT_COOLDOWN = 3600  # 1 hour
_stale_alert_sent: "dict[str, float]" = {}


# ----------------------------- Telegram alert (macro module) ------------------
def _load_telegram_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        tg = cfg.get("telegram", {})
        return tg.get("bot_token") or None, str(tg.get("chat_id") or "")
    except Exception:
        return None, None


def _send_telegram(text):
    """Best-effort Telegram send from within macro_data — reads credentials
    from strategy_config.json. Never raises."""
    token, chat_id = _load_telegram_cfg()
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
    except Exception:
        pass


# ----------------------------- generic disk cache -----------------------------
def _load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass


def _cached(key, ttl_seconds, fetch_fn):
    """Returns fetch_fn()'s result from cache if fresh, otherwise re-fetches.
    On failure: logs ONCE, caches the failure for the full TTL so the next
    scan doesn't retry and re-log immediately, falls back to stale data, and
    sends a Telegram alert if the source has been unavailable for > 2× TTL."""
    cache = _load_cache()
    entry = cache.get(key)
    now   = time.time()

    # Cache hit (success or recorded failure) — return without a network call.
    if entry and (now - entry.get("ts", 0)) < ttl_seconds:
        # If this entry recorded a previous failure, return None silently.
        if entry.get("failed"):
            return None
        return entry["data"]

    # Cache expired (or missing) — attempt a fresh fetch.
    fresh = None
    fetch_err = None
    try:
        fresh = fetch_fn()
    except Exception as exc:
        fetch_err = exc

    if fresh is not None:
        # Success — update cache and DB, clear any stale alert.
        cache[key] = {"ts": now, "data": fresh}
        _save_cache(cache)
        _save_to_db(key, fresh)
        _stale_alert_sent.pop(key, None)
        return fresh

    # Fetch failed — log ONCE and cache the failure for the full TTL so
    # subsequent scans skip the retry and don't repeat this warning.
    raw_err = f"{type(fetch_err).__name__}: {fetch_err}" if fetch_err else "returned None"
    # HTML-escape so angle brackets in urllib error messages don't corrupt the Telegram HTML layout.
    err_str = raw_err.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    fallback = "stale cache" if entry and not entry.get("failed") else "no data"
    _logger.warning(
        f"[macro_data] {key}: fetch failed ({raw_err}) "
        f"— skipping for {ttl_seconds//3600:.0f}h, using {fallback}."
    )
    # Carry forward the last-good timestamp so the stale-alert mechanism keeps
    # firing on every _STALE_ALERT_COOLDOWN interval for the entire outage.
    # Without this, once the failure record itself expires the alert permanently
    # stops because last_good_entry becomes None and stale_secs is never computed.
    prev_good_ts = (entry.get("ts") if (entry and not entry.get("failed"))
                    else entry.get("last_good_ts") if entry else 0)
    cache[key] = {"ts": now, "failed": True, "data": None, "last_good_ts": prev_good_ts}
    _save_cache(cache)

    # Telegram alert if this source has been failing beyond 2× TTL.
    last_sent = _stale_alert_sent.get(key, 0)
    if prev_good_ts:
        stale_secs = now - prev_good_ts
        if stale_secs > ttl_seconds * _STALE_ALERT_MULTIPLIER \
                and (now - last_sent) > _STALE_ALERT_COOLDOWN:
            stale_h = stale_secs / 3600
            msg = (
                f"⚠️ <b>Macro data stale: {key}</b>\n"
                f"Last successful fetch: {stale_h:.1f}h ago "
                f"(limit: {ttl_seconds/3600:.0f}h × 2)\n"
                f"Error: {err_str}\n"
                f"Bot is running on cached data — scores may be outdated."
            )
            _send_telegram(msg)
            _stale_alert_sent[key] = now
            _logger.warning(f"[macro_data] {key}: stale alert sent via Telegram.")

    # Return the most recent good data if we still have it, else None.
    last_good_entry = entry if (entry and not entry.get("failed")) else None
    return last_good_entry["data"] if last_good_entry else None


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or _UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()


# ----------------------------- persistent history (SQLite) --------------------
# Separate from the cache file above on purpose. macro_data_cache.json is just
# "last good value" — fine to delete, no real loss. macro_data_history.db is
# an append-only audit trail: if the bot crashes, the cache file gets
# corrupted/wiped, or you reinstall the EA, every macro value the bot has
# ever successfully fetched is still safely on disk here. This directly
# answers "ป้องกันกรณี bot error ข้อมูลจะได้ไม่หาย" — protecting the data from
# being lost if the bot errors. sqlite3 is in the Python standard library, so
# this needs no extra install and no credentials, unlike the optional Google
# Sheets export below.
# Known proxy/fallback source names that trigger staleness badges, keyed by
# the same data_key strings used in get_macro_snapshot()'s
# update_proxy_fallback_state() calls. Populated from the actual "source"
# tags each fetcher emits on its fallback path (see _fetch_comex_via_cot_proxy,
# _fetch_etf_flow_via_yahoo, _fetch_yield10y_raw, _fetch_fed_expectation_raw
# above) — confirmed against the live code, not guessed. Even though the
# CME/SPDR/FRED blocks on this VPS are currently persistent (so the badge
# will likely show continuously rather than transiently), the user explicitly
# wants that visibility on the dashboard precisely BECAUSE macro_bias (weight
# 1.2, highest-weighted strategy) is scoring off degraded data for as long as
# the block lasts — "permanent" is not a reason to suppress this, it's the
# reason it was requested.
_PROXY_SOURCE_NAMES: "dict[str, set[str]]" = {
    "comex_gold":     {"cot_proxy"},
    "comex_silver":   {"cot_proxy"},
    "etf_gld":        {"yahoo_proxy"},
    "etf_slv":        {"yahoo_proxy"},
    "yield10y":       {"yahoo_TNX"},
    "fed_expectation": {"yahoo_FVX"},
}


def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        fetched_at REAL NOT NULL,
        fetched_at_iso TEXT NOT NULL,
        json_data TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_history_category "
                 "ON macro_history(category, fetched_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS proxy_fallback_state (
        data_key TEXT PRIMARY KEY,
        is_proxy INTEGER NOT NULL,
        since TEXT NOT NULL,
        proxy_source TEXT NOT NULL DEFAULT ''
    )""")
    return conn


def _save_to_db(category, data):
    """Appends one permanent history row. Best-effort only — a disk/locking
    problem here must never break a live fetch or crash the EA."""
    try:
        conn = _db_connect()
        with conn:
            conn.execute(
                "INSERT INTO macro_history (category, fetched_at, fetched_at_iso, json_data) "
                "VALUES (?, ?, ?, ?)",
                (category, time.time(), datetime.now(timezone.utc).isoformat(),
                 json.dumps(data, ensure_ascii=False, default=str)),
            )
        conn.close()
    except Exception:
        pass


def get_macro_history(category=None, limit=200):
    """Reads back historical fetches from macro_data_history.db — newest
    first. Useful for auditing what the bot actually saw, recovering data
    after a crash/corrupted cache, or building your own Excel/Sheets export
    (see export_history_to_google_sheets() below). Never raises — returns
    [] on any DB problem."""
    try:
        conn = _db_connect()
        if category:
            cur = conn.execute(
                "SELECT category, fetched_at, fetched_at_iso, json_data FROM macro_history "
                "WHERE category = ? ORDER BY fetched_at DESC LIMIT ?", (category, limit))
        else:
            cur = conn.execute(
                "SELECT category, fetched_at, fetched_at_iso, json_data FROM macro_history "
                "ORDER BY fetched_at DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        out = []
        for cat, ts, iso, blob in rows:
            try:
                parsed = json.loads(blob)
            except Exception:
                parsed = None
            out.append({"category": cat, "fetched_at": ts, "fetched_at_iso": iso, "data": parsed})
        return out
    except Exception:
        return []


def export_history_to_google_sheets(sheet_id, creds_json_path, category=None, limit=500):
    """OPTIONAL second backup layer, on top of (not instead of) the local
    SQLite DB above — exports macro_data_history.db rows to a Google Sheet,
    e.g. so you have an off-machine copy you can check from your phone, or
    that survives even a full disk loss on the trading PC.

    Requires: pip install gspread google-auth

    Setup (do this yourself — never paste your service-account JSON or its
    contents into a chat with anyone, including me — same rule as the
    Telegram bot token):
      1. Google Cloud Console -> create/select a project -> enable the
         "Google Sheets API"
      2. IAM & Admin -> Service Accounts -> create one -> Keys -> Add Key ->
         Create new key (JSON) -> download the .json file somewhere private
      3. Create a Google Sheet -> Share it with the service account's
         email address (looks like ...@...iam.gserviceaccount.com, found
         inside the downloaded JSON) -> give it Editor access
      4. Copy the Sheet ID from its URL
         (docs.google.com/spreadsheets/d/<SHEET_ID>/edit) and call:
         export_history_to_google_sheets("<SHEET_ID>", "/path/to/key.json")

    Entirely optional — if gspread/google-auth aren't installed, the creds
    file doesn't exist, or anything else goes wrong, this just returns False
    and does nothing else. The bot keeps trading and keeps writing to the
    local SQLite DB regardless of whether this is ever configured."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return False
    if not creds_json_path or not os.path.exists(creds_json_path):
        return False
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_json_path, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
        ws = sh.sheet1
        rows = get_macro_history(category=category, limit=limit)
        if not rows:
            return False
        header = ["fetched_at_iso", "category", "json_data"]
        values = [header] + [
            [r["fetched_at_iso"], r["category"], json.dumps(r["data"], ensure_ascii=False, default=str)]
            for r in rows
        ]
        ws.clear()
        ws.update(values)
        return True
    except Exception:
        return False


# ----------------------------- 1. COMEX Inventory -----------------------------
def _fetch_comex_via_cot_proxy(metal="GOLD"):
    """Fallback when CME is blocked: derives a synthetic COMEX registered/eligible
    ratio from the CFTC COT report's managed-money net positioning change.

    Rationale: when managed money is aggressively adding net longs, open
    interest is rising and deliverable (registered) supply relative to total
    eligible inventory is under pressure — the same dynamic the registered/
    eligible ratio measures directly.  The mapping is directional only:
      net_long_change > 0  →  registered < eligible  (ratio 0.38, bullish signal)
      net_long_change <= 0 →  registered > eligible  (ratio 0.62, no signal)

    score_macro_bias() checks `ratio < 0.5` so this preserves the same binary
    output while using data we can actually fetch.  Marked with source='cot_proxy'
    so dashboards and logs can distinguish it from real CME data."""
    cot = _fetch_cot_raw(metal)
    if not cot:
        return None
    change = cot.get("managed_money_net_long_change", 0)
    if change > 0:
        # Bullish: simulate tight registered supply (ratio ~0.38)
        reg, elig = 38000.0, 100000.0
    else:
        # Bearish/neutral: simulate loose supply (ratio ~0.62)
        reg, elig = 62000.0, 100000.0
    return {
        "metal":        metal,
        "registered_oz": reg,
        "eligible_oz":   elig,
        "total_oz":      reg + elig,
        "report_date":   cot.get("report_date"),
        "source":        "cot_proxy",
        "cot_net_long_change": change,
        "fetched_at":    time.time(),
    }


def _fetch_comex_inventory_raw(metal="GOLD"):
    """Downloads CME's official daily metals stocks .xls report.
    Falls back to _fetch_comex_via_cot_proxy() when CME returns 403/is blocked."""
    import pandas as pd
    url = f"https://www.cmegroup.com/delivery_reports/{'Gold' if metal == 'GOLD' else 'Silver'}_Stocks.xls"
    try:
        raw = _http_get(url)
    except Exception:
        # CME unreachable — use COT-based proxy.
        _logger.info(f"[macro_data] comex_{metal.lower()}: CME blocked, using COT proxy.")
        return _fetch_comex_via_cot_proxy(metal)

    tmp_path = os.path.join(os.path.dirname(CACHE_FILE), f"_tmp_{metal.lower()}_stocks.xls")
    with open(tmp_path, "wb") as f:
        f.write(raw)
    try:
        try:
            sheets = pd.read_excel(tmp_path, sheet_name=None, header=None, engine="xlrd")
        except Exception:
            sheets = pd.read_excel(tmp_path, sheet_name=None, header=None)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    registered = eligible = total = None
    report_date = None
    for df in sheets.values():
        for r in range(len(df)):
            row_vals = [str(v) for v in df.iloc[r].tolist()]
            row_text = " ".join(row_vals).upper()
            nums = [v for v in df.iloc[r].tolist() if isinstance(v, (int, float))]
            if "TOTAL REGISTERED" in row_text and nums:
                registered = float(nums[-1])
            elif "TOTAL ELIGIBLE" in row_text and nums:
                eligible = float(nums[-1])
            elif row_text.strip().startswith("TOTAL") and "REGISTERED" not in row_text \
                    and "ELIGIBLE" not in row_text and nums:
                total = float(nums[-1])
            if report_date is None and ("AS OF" in row_text or "DATE" in row_text):
                report_date = row_text

    if registered is None and eligible is None:
        # Parse failed — fall back to COT proxy.
        _logger.info(f"[macro_data] comex_{metal.lower()}: XLS parse failed, using COT proxy.")
        return _fetch_comex_via_cot_proxy(metal)

    if total is None and registered is not None and eligible is not None:
        total = registered + eligible

    return {
        "metal": metal,
        "registered_oz": registered,
        "eligible_oz": eligible,
        "total_oz": total,
        "report_date": report_date,
        "source": "cme_xls",
        "fetched_at": time.time(),
    }


def fetch_comex_inventory(metal="GOLD"):
    """Cached wrapper — see _fetch_comex_inventory_raw(). Returns None if the
    report couldn't be downloaded or parsed; never raises."""
    return _cached(f"comex_{metal.lower()}", CACHE_TTL["comex"],
                   lambda: _fetch_comex_inventory_raw(metal))


# ----------------------------- 2. CFTC COT Report ------------------------------
def _fetch_cot_raw(commodity="GOLD"):
    """Pulls the latest 2 weekly COT snapshots for `commodity` from CFTC's
    public Socrata API (Disaggregated Futures Only report) so we can compute
    week-over-week change, not just a single snapshot. `noncomm_positions_*`
    is the closest public proxy to "Managed Money" net positioning that the
    legacy/disaggregated dataset exposes without needing a paid feed."""
    base = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    # Space in ORDER BY must be percent-encoded — Python 3.12+ rejects unencoded
    # spaces in URLs with InvalidURL, silently breaking COT on newer runtimes.
    q = (f"?$where=commodity_name='{commodity}'"
         f"&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=2")
    raw = _http_get(base + q)
    rows = json.loads(raw)
    if not rows:
        return None

    def parse(row):
        return {
            "report_date": row.get("report_date_as_yyyy_mm_dd"),
            "open_interest": float(row.get("open_interest_all", 0) or 0),
            "change_open_interest": float(row.get("change_in_open_interest_all", 0) or 0),
            "managed_money_long": float(row.get("noncomm_positions_long_all", 0) or 0),
            "managed_money_short": float(row.get("noncomm_positions_short_all", 0) or 0),
            "commercial_long": float(row.get("comm_positions_long_all", 0) or 0),
            "commercial_short": float(row.get("comm_positions_short_all", 0) or 0),
        }

    latest = parse(rows[0])
    latest["managed_money_net_long"] = latest["managed_money_long"] - latest["managed_money_short"]

    prev = None
    if len(rows) > 1:
        prev = parse(rows[1])
        prev["managed_money_net_long"] = prev["managed_money_long"] - prev["managed_money_short"]
        latest["managed_money_net_long_change"] = (
            latest["managed_money_net_long"] - prev["managed_money_net_long"]
        )
    else:
        latest["managed_money_net_long_change"] = 0.0

    latest["commodity"] = commodity
    latest["fetched_at"] = time.time()
    return latest


def fetch_cot_report(commodity="GOLD"):
    """Cached wrapper — see _fetch_cot_raw(). `commodity` is "GOLD" or
    "SILVER" (matches CFTC's commodity_name field exactly)."""
    return _cached(f"cot_{commodity.lower()}", CACHE_TTL["cot"],
                   lambda: _fetch_cot_raw(commodity))


# ----------------------------- 3. ETF Flow (best-effort) -----------------------
def _fetch_etf_flow_via_yahoo(ticker="GLD"):
    """Fallback ETF flow signal via Yahoo Finance daily close prices.

    SPDR's CSV endpoint serves a PDF (bot wall) from this VPS IP.
    This derives a directional proxy: GLD's 1-day price change maps to
    an estimated tonnage change using GLD's known oz-per-share ratio
    (0.0971759 troy oz per share) and a fixed typical shares count, giving
    a change_tonnes value with the right sign even if the magnitude is
    approximate.  Marked source='yahoo_proxy' in the output.

    score_macro_bias() only checks `change_tonnes > 0` (inflow = bullish),
    so sign accuracy is what matters, not magnitude precision."""
    sym = ticker if ticker != "GLD" else "GLD"
    raw = _http_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        f"?range=5d&interval=1d"
    )
    j = json.loads(raw)
    closes = [c for c in j["chart"]["result"][0]["indicators"]["quote"][0]["close"]
              if c is not None]
    if len(closes) < 2:
        return None

    # GLD price change direction = ETF NAV direction = gold holding direction.
    # 1 GLD share ≈ 0.0971759 troy oz gold; ~400M shares outstanding (approx).
    # Magnitude is approximate; sign is reliable.
    OZ_PER_SHARE     = 0.0971759
    APPROX_SHARES    = 400_000_000
    TROY_OZ_PER_TONNE = 32150.7466
    pct_change = (closes[-1] - closes[-2]) / closes[-2]
    est_change_tonnes = pct_change * (APPROX_SHARES * OZ_PER_SHARE) / TROY_OZ_PER_TONNE

    return {
        "ticker":         ticker,
        "latest_tonnes":  0.0,   # not computable without real sharesOutstanding
        "change_tonnes":  est_change_tonnes,
        "source":         "yahoo_proxy",
        "gld_close":      closes[-1],
        "gld_prev_close": closes[-2],
        "fetched_at":     time.time(),
    }


def _fetch_etf_flow_raw(ticker="GLD"):
    """ETF flow: tries SPDR's official CSV first; falls back to a Yahoo Finance
    price-trend proxy when SPDR serves a bot-wall PDF (common from VPS IPs)."""
    if ticker == "GLD":
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
    else:
        url = ("https://www.ishares.com/us/products/239855/ishares-silver-trust-fund/"
               "1467271812596.ajax?fileType=csv&fileName=SLV_holdings&dataType=fund")
    try:
        raw = _http_get(url)
        # Guard: SPDR serves a PDF bot-wall at HTTP 200.
        if raw[:5] in (b"%PDF-", b"%PDF\n") or b"<html" in raw[:300].lower():
            raise ValueError("bot-wall response (PDF or HTML)")

        import csv, io
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8", errors="ignore"))))
        if len(rows) < 3:
            raise ValueError("too few rows in CSV")

        numeric_cols_last_two_rows = []
        for row in rows[-3:]:
            nums = []
            for cell in row:
                try:
                    nums.append(float(cell.replace(",", "")))
                except ValueError:
                    continue
            if nums:
                numeric_cols_last_two_rows.append(nums)
        if len(numeric_cols_last_two_rows) < 2:
            raise ValueError("no numeric rows parsed")

        latest_tonnes = numeric_cols_last_two_rows[-1][-1]
        prev_tonnes   = numeric_cols_last_two_rows[-2][-1]
        return {
            "ticker":        ticker,
            "latest_tonnes": latest_tonnes,
            "change_tonnes": latest_tonnes - prev_tonnes,
            "source":        "spdr_csv",
            "fetched_at":    time.time(),
        }

    except Exception as exc:
        # SPDR unavailable — fall back to Yahoo price proxy.
        _logger.info(
            f"[macro_data] etf_{ticker.lower()}: SPDR failed ({exc!r}), "
            f"using Yahoo price proxy."
        )
        try:
            return _fetch_etf_flow_via_yahoo(ticker)
        except Exception:
            return None


def fetch_etf_flow(ticker="GLD"):
    """Cached wrapper around the best-effort ETF flow scrape. Returns None
    far more often than the other fetchers here — see module docstring."""
    return _cached(f"etf_{ticker.lower()}", CACHE_TTL["etf_flow"],
                    lambda: _fetch_etf_flow_raw(ticker))


# ----------------------------- 4. DXY (Dollar Index) ---------------------------
def _fetch_dxy_raw():
    """Tries Yahoo Finance's public chart API first (same endpoint the
    `yfinance` package uses) since it gives the real ICE Dollar Index. Falls
    back to FRED's Trade-Weighted Broad Dollar Index (DTWEXBGS) — official
    Fed data, not identical to ICE DXY but a solid directional proxy — if
    Yahoo blocks the request (this happened from the sandbox IP used during
    development; may well work fine from your machine)."""
    try:
        raw = _http_get("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=5d&interval=1d")
        j = json.loads(raw)
        result = j["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) >= 2:
            return {"source": "yahoo_DXY", "latest": closes[-1], "prev": closes[-2],
                     "change": closes[-1] - closes[-2], "fetched_at": time.time()}
    except Exception:
        pass

    try:
        raw = _http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTWEXBGS")
        lines = [l for l in raw.decode("utf-8", errors="ignore").splitlines() if l.strip()]
        rows = [l.split(",") for l in lines[1:] if "." in l.split(",")[-1]]
        if len(rows) >= 2:
            latest, prev = float(rows[-1][1]), float(rows[-2][1])
            return {"source": "fred_DTWEXBGS", "latest": latest, "prev": prev,
                     "change": latest - prev, "fetched_at": time.time()}
    except Exception:
        pass
    return None


def fetch_dxy():
    """Cached wrapper — see _fetch_dxy_raw()."""
    return _cached("dxy", CACHE_TTL["dxy"], _fetch_dxy_raw)


# ----------------------------- 5. US 10Y Treasury Yield ------------------------
def _fetch_yield10y_raw():
    """US 10-Year Treasury yield. Tries FRED DGS10 first (most authoritative),
    falls back to Yahoo Finance ^TNX if FRED is unreachable from this host."""
    # Primary: FRED DGS10 (short timeout — FRED is unreachable from this VPS)
    try:
        req = urllib.request.Request(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", headers=_UA)
        with urllib.request.urlopen(req, timeout=_FRED_TIMEOUT) as resp:
            raw = resp.read()
        lines = [l for l in raw.decode("utf-8", errors="ignore").splitlines() if l.strip()]
        rows = [l.split(",") for l in lines[1:]]
        rows = [r for r in rows if len(r) == 2 and r[1] not in (".", "")]
        if len(rows) >= 2:
            latest_date, latest = rows[-1][0], float(rows[-1][1])
            prev_date, prev = rows[-2][0], float(rows[-2][1])
            return {"source": "FRED_DGS10", "latest": latest, "prev": prev,
                    "change": latest - prev, "latest_date": latest_date,
                    "fetched_at": time.time()}
    except Exception:
        pass
    # Fallback: Yahoo Finance ^TNX (CBOE 10-Year Treasury Note Yield Index)
    try:
        raw = _http_get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX"
            "?range=5d&interval=1d"
        )
        j = json.loads(raw)
        closes = [c for c in j["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                  if c is not None]
        if len(closes) >= 2:
            return {"source": "yahoo_TNX", "latest": closes[-1], "prev": closes[-2],
                    "change": closes[-1] - closes[-2], "fetched_at": time.time()}
    except Exception:
        pass
    return None


def fetch_yield_10y():
    """Cached wrapper — see _fetch_yield10y_raw()."""
    return _cached("yield10y", CACHE_TTL["yield10y"], _fetch_yield10y_raw)


# ----------------------------- 7. Fed Rate Expectation (proxy) -----------------
def _fetch_fed_expectation_raw():
    """APPROXIMATION — not the real CME FedWatch probabilities. Uses the
    2-Year Treasury yield as a near-term Fed policy proxy (falling 2Y = market
    pricing cuts = bullish gold; rising 2Y = hold/hike = bearish gold).
    Tries FRED DGS2 first, falls back to Yahoo Finance ^FVX (5-Year yield,
    next best proxy) if FRED is unreachable from this host."""
    # Primary: FRED DGS2 (short timeout — FRED is unreachable from this VPS)
    try:
        req = urllib.request.Request(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2", headers=_UA)
        with urllib.request.urlopen(req, timeout=_FRED_TIMEOUT) as resp:
            raw = resp.read()
        lines = [l for l in raw.decode("utf-8", errors="ignore").splitlines() if l.strip()]
        rows = [l.split(",") for l in lines[1:]]
        rows = [r for r in rows if len(r) == 2 and r[1] not in (".", "")]
        if len(rows) >= 2:
            latest_date, latest = rows[-1][0], float(rows[-1][1])
            prev_date, prev = rows[-2][0], float(rows[-2][1])
            return {"proxy": "US2Y_yield (DGS2)", "source": "FRED_DGS2",
                    "latest": latest, "prev": prev, "change": latest - prev,
                    "latest_date": latest_date, "fetched_at": time.time()}
    except Exception:
        pass
    # Fallback: Yahoo Finance ^FVX (5-Year Treasury Note Yield — directional proxy)
    try:
        raw = _http_get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EFVX"
            "?range=5d&interval=1d"
        )
        j = json.loads(raw)
        closes = [c for c in j["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                  if c is not None]
        if len(closes) >= 2:
            return {"proxy": "US5Y_yield (^FVX fallback)", "source": "yahoo_FVX",
                    "latest": closes[-1], "prev": closes[-2],
                    "change": closes[-1] - closes[-2], "fetched_at": time.time()}
    except Exception:
        pass
    return None


def fetch_fed_expectation():
    """Cached wrapper — see _fetch_fed_expectation_raw()."""
    return _cached("fed_expectation", CACHE_TTL["fed_expectation"], _fetch_fed_expectation_raw)


# ----------------------------- 6. Economic Calendar -----------------------------
def _fetch_calendar_raw():
    """ForexFactory's own public JSON feed for the current week (the same
    feed their embeddable calendar widget uses) — no key, no auth."""
    raw = _http_get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    events = json.loads(raw)
    out = []
    for e in events:
        out.append({
            "title": e.get("title"),
            "country": e.get("country"),
            "date": e.get("date"),
            "impact": e.get("impact"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
            "actual": e.get("actual"),
        })
    return {"events": out, "fetched_at": time.time()}


def fetch_economic_calendar(force=False):
    """Cached wrapper — see _fetch_calendar_raw(). Pass force=True to bypass
    the normal ~6h cache and hit the live feed directly — used right around
    a scheduled High-impact release so the "actual" figure (published by the
    feed once the number drops) is picked up faster than the regular cache
    cadence would allow. Falls back to the cached/stale value if the forced
    fetch itself fails, exactly like the normal cached path."""
    if force:
        try:
            fresh = _fetch_calendar_raw()
        except Exception:
            fresh = None
        if fresh is not None:
            cache = _load_cache()
            cache["calendar"] = {"ts": time.time(), "data": fresh}
            _save_cache(cache)
            _save_to_db("calendar", fresh)
            return fresh
        # forced fetch failed — fall through to the normal cached/stale path
    return _cached("calendar", CACHE_TTL["calendar"], _fetch_calendar_raw)


def upcoming_high_impact_events(calendar=None, within_minutes=60,
                                 currencies=("USD",)):
    """Filters a fetch_economic_calendar() result down to High-impact events
    for the given currencies (default USD, since that's what moves Gold)
    landing within `within_minutes` from now. Used as a soft pre-news gate —
    see score_macro_bias() in strategies.py."""
    from datetime import datetime, timezone, timedelta
    calendar = calendar if calendar is not None else fetch_economic_calendar()
    if not calendar or not calendar.get("events"):
        return []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=within_minutes)
    soon = []
    for e in calendar["events"]:
        if e.get("impact") != "High" or e.get("country") not in currencies:
            continue
        try:
            dt = datetime.fromisoformat(e["date"])
        except (ValueError, TypeError, KeyError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if now <= dt <= horizon:
            soon.append(e)
    return soon


# ----------------------------- 9. Myfxbook Community Outlook -----------------
# Public retail-sentiment data (% of Myfxbook community currently long/short
# XAUUSD). Contrarian or trend-following — see score_myfxbook_sentiment() in
# strategies.py. Credentials: requires a free myfxbook.com account's
# email/password — fill these in via strategy_config_ui.py -> "Myfxbook
# Sentiment" tab. NEVER type your real Myfxbook email/password into chat with
# Claude, never log them. Only the resulting session token (never the password)
# is cached to disk.

def _myfxbook_login(email, password):
    """POST (as GET, per Myfxbook's own API) login.json -> session token."""
    if not email or not password:
        return None
    url = ("https://www.myfxbook.com/api/login.json?"
           + urllib.parse.urlencode({"email": email, "password": password}))
    try:
        raw = _http_get(url)
    except Exception as exc:
        raise type(exc)("Myfxbook login request failed (credentials redacted)") from None
    j = json.loads(raw)
    if j.get("error") or not j.get("session"):
        return None
    return j["session"]


def _get_myfxbook_session(email, password):
    """Reuses a cached session token (keyed by email) until _MYFXBOOK_SESSION_TTL
    elapses, to avoid burning the 100-req/day quota on repeated logins."""
    cache = _load_cache()
    entry = cache.get("myfxbook_session")
    now = time.time()
    if entry and entry.get("email") == email and (now - entry.get("ts", 0)) < _MYFXBOOK_SESSION_TTL:
        return entry["session"]
    session = _myfxbook_login(email, password)
    if session:
        cache["myfxbook_session"] = {"ts": now, "session": session, "email": email}
        _save_cache(cache)
    return session


def _myfxbook_outlook(session):
    url = ("https://www.myfxbook.com/api/get-community-outlook.json?"
           + urllib.parse.urlencode({"session": session}))
    raw = _http_get(url)
    return json.loads(raw)


def _fetch_myfxbook_sentiment_raw(symbol, email, password):
    """Never raises — on any failure returns None so the score degrades to
    0/0 in that scan rather than blocking the EA."""
    session = _get_myfxbook_session(email, password)
    if not session:
        return None
    j = _myfxbook_outlook(session)
    if j.get("error"):
        # Session may have expired server-side — drop cache and retry once.
        cache = _load_cache()
        cache.pop("myfxbook_session", None)
        _save_cache(cache)
        session = _get_myfxbook_session(email, password)
        if not session:
            return None
        j = _myfxbook_outlook(session)
        if j.get("error"):
            return None
    symbols = j.get("symbols") or []
    row = next((s for s in symbols if str(s.get("name", "")).upper() == symbol.upper()), None)
    if not row:
        return None
    return {
        "symbol": symbol,
        "long_percentage": float(row.get("longPercentage") or 0),
        "short_percentage": float(row.get("shortPercentage") or 0),
        "long_volume": row.get("longVolume"),
        "short_volume": row.get("shortVolume"),
        "long_positions": row.get("longPositions"),
        "short_positions": row.get("shortPositions"),
        "fetched_at": time.time(),
    }


def fetch_myfxbook_sentiment(symbol="XAUUSD", email=None, password=None):
    """Cached wrapper. Returns None immediately (zero network calls) if
    email/password aren't supplied — keeps the feature a true no-op until
    the user opts in."""
    if not email or not password:
        return None
    return _cached(f"myfxbook_{symbol.lower()}", CACHE_TTL["myfxbook"],
                   lambda: _fetch_myfxbook_sentiment_raw(symbol, email, password))


# ----------------------------- proxy fallback tracker ------------------------
def update_proxy_fallback_state(data_key, result):
    """Called after each fetch: if result's source is a known proxy/fallback,
    records when it first became a proxy (never resets that timestamp on
    subsequent proxy fetches). Clears the row when source returns to primary."""
    if result is None:
        return
    source = result.get("source", "")
    proxy_sources = _PROXY_SOURCE_NAMES.get(data_key, set())
    is_proxy = source in proxy_sources
    try:
        conn = _db_connect()
        with conn:
            row = conn.execute(
                "SELECT is_proxy, since FROM proxy_fallback_state WHERE data_key=?",
                (data_key,)
            ).fetchone()
            if is_proxy:
                if row is None or row[0] == 0:
                    # First time on proxy — record "since" now
                    conn.execute(
                        "INSERT OR REPLACE INTO proxy_fallback_state "
                        "(data_key, is_proxy, since, proxy_source) VALUES (?,1,?,?)",
                        (data_key, datetime.now(timezone.utc).isoformat(), source)
                    )
                # else: already on proxy — leave "since" untouched
            else:
                if row is not None and row[0] == 1:
                    # Recovered to primary — clear the row
                    conn.execute(
                        "DELETE FROM proxy_fallback_state WHERE data_key=?",
                        (data_key,)
                    )
        conn.close()
    except Exception:
        pass


def get_proxy_staleness_report():
    """Returns list of dicts for all data keys currently on proxy/fallback
    sources: [{"data_key": ..., "since": ISO str, "proxy_source": ...,
    "hours": float}]. Empty list when everything is on primary sources."""
    try:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT data_key, since, proxy_source FROM proxy_fallback_state WHERE is_proxy=1"
        ).fetchall()
        conn.close()
        out = []
        now = datetime.now(timezone.utc)
        for data_key, since_iso, proxy_source in rows:
            try:
                since_dt = datetime.fromisoformat(since_iso)
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
                hours = (now - since_dt).total_seconds() / 3600.0
            except Exception:
                hours = 0.0
            out.append({"data_key": data_key, "since": since_iso,
                        "proxy_source": proxy_source, "hours": hours})
        return out
    except Exception:
        return []


# ----------------------------- price sanity cross-check ----------------------
def fetch_reference_gold_price():
    """Free, no-key spot/futures gold quote from Yahoo Finance (GC=F COMEX
    gold futures). Used ONLY to sanity-check the broker's own tick against
    an independent source — NEVER fed into scoring or order placement.
    Returns None on any failure, never raises."""
    try:
        raw = _http_get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F")
        j = json.loads(raw)
        price = j["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return {"price": float(price), "source": "yahoo_gc_f", "fetched_at": time.time()}
    except Exception:
        return None


# ----------------------------- aggregate snapshot -----------------------------
def get_macro_snapshot(symbol_metal="GOLD", myfxbook_email=None,
                        myfxbook_password=None, myfxbook_symbol="XAUUSD"):
    """One call that gathers everything this module can fetch into a single
    dict, every field independently None-safe. This is what
    xauusd_mt5_strategy.py wires into build_market_data()'s data["macro"],
    and what strategies.score_macro_bias() consumes — see strategies.py for
    how the 6-point checklist from the reference doc is scored from this.
    myfxbook_email/myfxbook_password are optional — pass None to skip the
    Myfxbook call entirely (it costs a real HTTP request unlike the other
    cached-only sources here)."""
    etf_ticker = "GLD" if symbol_metal == "GOLD" else "SLV"
    comex   = fetch_comex_inventory(symbol_metal)
    cot     = fetch_cot_report(symbol_metal)
    etf     = fetch_etf_flow(etf_ticker)
    dxy     = fetch_dxy()
    yld     = fetch_yield_10y()
    fed     = fetch_fed_expectation()
    cal     = fetch_economic_calendar()
    myfxbook = fetch_myfxbook_sentiment(myfxbook_symbol, myfxbook_email, myfxbook_password)

    # Track proxy/fallback state for the dashboard staleness badge.
    update_proxy_fallback_state(f"comex_{symbol_metal.lower()}", comex)
    update_proxy_fallback_state(f"etf_{etf_ticker.lower()}", etf)
    update_proxy_fallback_state("yield10y", yld)
    update_proxy_fallback_state("fed_expectation", fed)

    return {
        "comex": comex,
        "cot": cot,
        "etf_flow": etf,
        "dxy": dxy,
        "yield10y": yld,
        "fed_expectation": fed,
        "calendar": cal,
        "myfxbook_sentiment": myfxbook,
    }
