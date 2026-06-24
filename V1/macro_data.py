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

import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_data_cache.json")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_data_history.db")
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_TIMEOUT = 12

# Cache lifetimes per source (seconds) — matched to real update cadence.
CACHE_TTL = {
    "cot": 12 * 3600,             # CFTC publishes once a week (Fridays)
    "calendar": 6 * 3600,         # ForexFactory refreshes their weekly feed a few times/day
    "yield10y": 6 * 3600,         # FRED updates once per business day
    "dxy": 3 * 3600,              # intraday-ish, but we only need it a few times/day
    "comex": 6 * 3600,            # CME publishes the stocks report once per business day
    "etf_flow": 12 * 3600,        # daily at best, and often unavailable (see module note)
    "fed_expectation": 6 * 3600,  # FRED 2Y yield, updates once per business day
}


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
        pass  # caching is best-effort; never let a disk error break a fetch


def _cached(key, ttl_seconds, fetch_fn):
    """Returns fetch_fn()'s result, but only calls it if the cached value for
    `key` is missing or older than ttl_seconds. On a fresh-fetch failure,
    falls back to whatever's in the cache (even if stale) rather than
    returning nothing — stale macro data is still more useful than none.
    Every FRESH successful fetch (not stale-cache fallbacks) is also
    permanently appended to macro_data_history.db — see _save_to_db()."""
    cache = _load_cache()
    entry = cache.get(key)
    now = time.time()
    if entry and (now - entry.get("ts", 0)) < ttl_seconds:
        return entry["data"]

    fresh = None
    try:
        fresh = fetch_fn()
    except Exception:
        fresh = None

    if fresh is not None:
        cache[key] = {"ts": now, "data": fresh}
        _save_cache(cache)
        _save_to_db(key, fresh)
        return fresh

    # fetch failed — serve stale cache if we have any, else None
    if entry:
        return entry["data"]
    return None


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
def _db_connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS macro_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        fetched_at REAL NOT NULL,
        fetched_at_iso TEXT NOT NULL,
        json_data TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_history_category "
                 "ON macro_history(category, fetched_at)")
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
def _fetch_comex_inventory_raw(metal="GOLD"):
    """Downloads CME's official daily metals stocks .xls report and pulls out
    Registered / Eligible / Total ounces for the given metal. Uses a
    keyword-based cell search (not hardcoded coordinates) so it tolerates
    minor layout changes CME makes to the report over time."""
    import pandas as pd
    url = f"https://www.cmegroup.com/delivery_reports/{'Gold' if metal == 'GOLD' else 'Silver'}_Stocks.xls"
    raw = _http_get(url)
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
        return None  # report layout didn't match what we expected

    if total is None and registered is not None and eligible is not None:
        total = registered + eligible

    return {
        "metal": metal,
        "registered_oz": registered,
        "eligible_oz": eligible,
        "total_oz": total,
        "report_date": report_date,
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
    q = (f"?$where=commodity_name='{commodity}'"
         f"&$order=report_date_as_yyyy_mm_dd DESC&$limit=2")
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
def _fetch_etf_flow_raw(ticker="GLD"):
    """Best-effort only — see module docstring. SPDR (GLD) and iShares (SLV)
    both front their holdings-CSV endpoints with bot detection that often
    serves an HTML page instead of CSV to non-browser requests; this may
    simply return None for you even though the code is correct. Treat any
    result here as a bonus signal, never a dependency."""
    if ticker == "GLD":
        url = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
    else:
        url = ("https://www.ishares.com/us/products/239855/ishares-silver-trust-fund/"
               "1467271812596.ajax?fileType=csv&fileName=SLV_holdings&dataType=fund")
    raw = _http_get(url)
    text = raw.decode("utf-8", errors="ignore")
    if "<html" in text.lower()[:200]:
        return None  # bot wall served the HTML shell instead of real data

    import csv
    import io
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        return None

    # GLD archive format: date, close, NAV-in-gold, ..., total ounces, tonnes
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
        return None

    latest_tonnes = numeric_cols_last_two_rows[-1][-1]
    prev_tonnes = numeric_cols_last_two_rows[-2][-1]
    return {
        "ticker": ticker,
        "latest_tonnes": latest_tonnes,
        "change_tonnes": latest_tonnes - prev_tonnes,
        "fetched_at": time.time(),
    }


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
    """FRED's DGS10 series — official daily 10-Year Treasury Constant
    Maturity Rate. Free, no API key needed for the CSV graph export."""
    raw = _http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10")
    lines = [l for l in raw.decode("utf-8", errors="ignore").splitlines() if l.strip()]
    rows = [l.split(",") for l in lines[1:]]
    rows = [r for r in rows if len(r) == 2 and r[1] not in (".", "")]
    if len(rows) < 2:
        return None
    latest_date, latest = rows[-1][0], float(rows[-1][1])
    prev_date, prev = rows[-2][0], float(rows[-2][1])
    return {"latest": latest, "prev": prev, "change": latest - prev,
            "latest_date": latest_date, "fetched_at": time.time()}


def fetch_yield_10y():
    """Cached wrapper — see _fetch_yield10y_raw()."""
    return _cached("yield10y", CACHE_TTL["yield10y"], _fetch_yield10y_raw)


# ----------------------------- 7. Fed Rate Expectation (proxy) -----------------
def _fetch_fed_expectation_raw():
    """APPROXIMATION — not the real CME FedWatch probabilities. CME's actual
    FedWatch tool (rate-cut/hike odds derived from Fed Funds futures) has no
    free structured API and its page is JavaScript-rendered, so it can't be
    reliably scraped with a plain HTTP request. As a free, durable proxy this
    uses FRED's 2-Year Treasury yield (DGS2) — the 2Y is highly sensitive to
    near-term Fed policy expectations: a falling 2Y yield means the market is
    pricing in rate cuts (bullish Gold), a rising 2Y yield means the market
    is pricing in a hold/hike (bearish Gold). Documented honestly as a
    directional stand-in, not the actual FedWatch percentage."""
    raw = _http_get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2")
    lines = [l for l in raw.decode("utf-8", errors="ignore").splitlines() if l.strip()]
    rows = [l.split(",") for l in lines[1:]]
    rows = [r for r in rows if len(r) == 2 and r[1] not in (".", "")]
    if len(rows) < 2:
        return None
    latest_date, latest = rows[-1][0], float(rows[-1][1])
    prev_date, prev = rows[-2][0], float(rows[-2][1])
    return {"proxy": "US2Y_yield (DGS2)", "latest": latest, "prev": prev,
            "change": latest - prev, "latest_date": latest_date, "fetched_at": time.time()}


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


# ----------------------------- aggregate snapshot -----------------------------
def get_macro_snapshot(symbol_metal="GOLD"):
    """One call that gathers everything this module can fetch into a single
    dict, every field independently None-safe. This is what
    xauusd_mt5_strategy.py wires into build_market_data()'s data["macro"],
    and what strategies.score_macro_bias() consumes — see strategies.py for
    how the 6-point checklist from the reference doc is scored from this."""
    etf_ticker = "GLD" if symbol_metal == "GOLD" else "SLV"
    return {
        "comex": fetch_comex_inventory(symbol_metal),
        "cot": fetch_cot_report(symbol_metal),
        "etf_flow": fetch_etf_flow(etf_ticker),
        "dxy": fetch_dxy(),
        "yield10y": fetch_yield_10y(),
        "fed_expectation": fetch_fed_expectation(),
        "calendar": fetch_economic_calendar(),
    }
