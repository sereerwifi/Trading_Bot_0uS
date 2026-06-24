# Prompt for Claude Code on the VPS — sync the Myfxbook Retail Sentiment feature (25th strategy)

Paste everything below into Claude Code in the VPS project folder (`...USV9`).
It explains exactly what changed in the local copy (`RoBotTrading man 0 US`,
no V9 suffix) and what to replicate here, file by file, with the exact code.

---

## Context

The local (non-VPS) copy of this bot added a 25th confluence strategy:
**Myfxbook Retail Sentiment** — pulls the public Community Outlook (%
long/short of Myfxbook's user base) for XAUUSD and votes contrarian
(fade the crowd) by default. This is a *new, additive* feature — nothing
existing was removed or restructured. It is OFF by default and does zero
network calls unless a real Myfxbook email/password is entered.

You (Claude Code on the VPS) have NOT seen this change yet. Apply it to
the 6 files below, matching the existing code conventions already in this
repo (the fetch-pattern in `macro_data.py`, the scoring-function pattern
in `strategies.py`, the global+hot-reload pattern in
`xauusd_mt5_strategy.py`, the Tkinter tab pattern in
`strategy_config_ui.py`).

**IMPORTANT constraint from the user**: the Myfxbook strategy's weight
must stay LOWER than the existing "Big Data" macro strategy
(`macro_bias`, weight `1.2`). Use weight `0.8` for `myfxbook_sentiment`
— Myfxbook retail sentiment is a secondary/confirming vote, not a
primary signal, and should never be allowed to outweigh the institutional
macro_bias strategy. Do not raise it above `1.2` even if asked later
without the user explicitly re-confirming this tradeoff.

---

## 1. `macro_data.py`

### 1a. Module docstring — add item 9 to the data-source list at the top:

```
9. Myfxbook Community Outlook (retail sentiment)  -> Myfxbook's free public
   API (login.json + get-community-outlook.json), requires a free Myfxbook
   account's email/password, 100 requests/day limit -- see
   fetch_myfxbook_sentiment()
```

### 1b. Imports — add:
```python
import urllib.parse
```

### 1c. `CACHE_TTL` dict — add:
```python
"myfxbook": 30 * 60,          # 100 req/day free-tier limit -> 30min keeps us well under it
```

### 1d. New module-level constant (near CACHE_TTL):
```python
# Myfxbook login sessions are reused across calls (don't re-login every scan --
# that burns the daily request quota). We don't know the exact session TTL on
# Myfxbook's side, but we'd rather re-login a bit early than get a stale-session
# error mid-scan.
_MYFXBOOK_SESSION_TTL = 50 * 60
```

### 1e. New functions — insert just before the "aggregate snapshot" section
(`get_macro_snapshot`):

```python
# ----------------------------- 9. Myfxbook Community Outlook -----------------
# Public retail-sentiment data (% of Myfxbook community currently long/short
# XAUUSD). This is NOT a copy of any proprietary Myfxbook strategy from
# myfxbook.com/strategy-ai (declined -- survivorship bias, unauditable,
# IP concerns) -- it's just the free public aggregate sentiment number,
# used here as one contrarian (or trend) vote among 25.
#
# Credentials: requires a free myfxbook.com account's email/password -- fill
# these in yourself via strategy_config_ui.py -> "Myfxbook Sentiment" tab (or
# directly in strategy_config.json under "myfxbook"). Same rule as the
# Telegram bot_token elsewhere in this file: NEVER type your real
# Myfxbook email/password into chat with Claude, never log them. This module
# only ever sends them to myfxbook.com's own login endpoint, and only the
# resulting session token (never the password) is cached to disk.

def _myfxbook_login(email, password):
    """POST (as GET, per Myfxbook's own API) login.json -> {"error":false,
    "session": "<token>"} on success."""
    if not email or not password:
        return None
    url = ("https://www.myfxbook.com/api/login.json?"
           + urllib.parse.urlencode({"email": email, "password": password}))
    raw = _http_get(url)
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
    url = "https://www.myfxbook.com/api/get-community-outlook.json?" + urllib.parse.urlencode({"session": session})
    raw = _http_get(url)
    return json.loads(raw)


def _fetch_myfxbook_sentiment_raw(symbol, email, password):
    """Never raises -- on any failure (bad creds, expired session, symbol not
    found, network error) returns None, so the caller's _cached() wrapper and
    score_myfxbook_sentiment() in strategies.py degrades to a 0/0 vote in
    that scan instead of blocking the EA."""
    session = _get_myfxbook_session(email, password)
    if not session:
        return None
    j = _myfxbook_outlook(session)
    if j.get("error"):
        # session may have expired server-side -- drop the cached one and retry once
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
    """Cached wrapper -- see _fetch_myfxbook_sentiment_raw(). Returns None
    immediately (zero network calls) if email/password aren't supplied --
    this keeps the feature a true no-op until the user opts in."""
    if not email or not password:
        return None
    return _cached(f"myfxbook_{symbol.lower()}", CACHE_TTL["myfxbook"],
                    lambda: _fetch_myfxbook_sentiment_raw(symbol, email, password))
```

### 1f. `get_macro_snapshot()` — change signature and add the new key:

```python
def get_macro_snapshot(symbol_metal="GOLD", myfxbook_email=None, myfxbook_password=None,
                        myfxbook_symbol="XAUUSD"):
    """
    ...(keep existing docstring)...
    myfxbook_email/myfxbook_password are optional -- pass None to skip the
    Myfxbook call entirely (it costs a real HTTP request, unlike the other
    cached-only sources here).
    """
    etf_ticker = "GLD" if symbol_metal == "GOLD" else "SLV"
    return {
        "comex": fetch_comex_inventory(symbol_metal),
        "cot": fetch_cot_report(symbol_metal),
        "etf_flow": fetch_etf_flow(etf_ticker),
        "dxy": fetch_dxy(),
        "yield10y": fetch_yield_10y(),
        "fed_expectation": fetch_fed_expectation(),
        "calendar": fetch_economic_calendar(),
        "myfxbook_sentiment": fetch_myfxbook_sentiment(myfxbook_symbol, myfxbook_email, myfxbook_password),
    }
```

(Keep every existing key/line exactly as it already is on the VPS — only
add the `myfxbook_sentiment` line and the two new parameters.)

---

## 2. `strategies.py`

### 2a. Add this function right before `STRATEGY_REGISTRY`:

```python
def score_myfxbook_sentiment(data):
    """25th strategy -- Myfxbook public Community Outlook (retail long/short %
    for XAUUSD). Reads data["macro"]["myfxbook_sentiment"] (see
    macro_data.py: fetch_myfxbook_sentiment()). Degrades gracefully to a
    0/0 "unavailable" vote if Myfxbook credentials aren't configured or the
    fetch failed -- never blocks the EA.

    Two modes, switchable via data["myfxbook_contrarian"] (default True,
    set from MYFXBOOK_CONTRARIAN in xauusd_mt5_strategy.py / the UI):
      - contrarian=True  (default): fade the crowd -- if most retail is
        long, that's a SHORT vote, and vice versa.
      - contrarian=False: trend-following -- vote with the crowd.

    NOTE: weight for this strategy in strategy_config.json should stay
    LOWER than macro_bias ("Big Data", weight 1.2) -- this is a secondary
    confirming signal from a single retail-broker sample, not an
    institutional-grade data source. Default weight is 0.8.
    """
    macro = data.get("macro")
    if macro is None:
        return {"long": 0.0, "short": 0.0, "note": "macro data unavailable"}

    sentiment = macro.get("myfxbook_sentiment")
    if not sentiment:
        return {"long": 0.0, "short": 0.0,
                 "note": "Myfxbook sentiment unavailable (not configured, or fetch pending/failed)"}

    long_pct = sentiment.get("long_percentage", 0.0) or 0.0
    short_pct = sentiment.get("short_percentage", 0.0) or 0.0
    if long_pct == 0 and short_pct == 0:
        return {"long": 0.0, "short": 0.0, "note": "Myfxbook sentiment returned no data for this symbol"}

    contrarian = data.get("myfxbook_contrarian", True)
    if contrarian:
        long_score, short_score = short_pct, long_pct
        mode_label = "contrarian — fading the crowd"
    else:
        long_score, short_score = long_pct, short_pct
        mode_label = "trend-following — with the crowd"

    note = (f"Myfxbook retail sentiment: {long_pct:.0f}% long / {short_pct:.0f}% short "
            f"({mode_label})")
    return {"long": _clip(long_score), "short": _clip(short_score), "note": note}
```

(`_clip` is whatever the existing clipping helper in this file is already
called — match it, don't introduce a new one.)

### 2b. `STRATEGY_REGISTRY` — add as the last entry:

```python
    # ---- 25th: Myfxbook public Community Outlook (retail sentiment).
    # Reads data["macro"]["myfxbook_sentiment"] (see macro_data.py) -- scores
    # 0/0 gracefully until Myfxbook credentials are configured in the UI.
    "myfxbook_sentiment": ("Myfxbook Retail Sentiment", score_myfxbook_sentiment),
```

---

## 3. `xauusd_mt5_strategy.py`

### 3a. New globals block — add right after the existing Telegram secrets
block (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`):

```python
# --- Myfxbook Sentiment (25th strategy) ---------------------------------------
# Fill these in yourself via strategy_config_ui.py -> "Myfxbook Sentiment" tab
# (or directly in strategy_config.json under "myfxbook") -- this script never
# asks for or transmits these anywhere except straight to myfxbook.com's own
# login endpoint (see macro_data.py). Only the resulting session token, never the
# password, is cached to disk by macro_data.py. Disabled by default -- score_myfxbook_
# sentiment() scores 0/0 until this is turned on with valid credentials.
MYFXBOOK_ENABLED = False
MYFXBOOK_EMAIL = ""
MYFXBOOK_PASSWORD = ""
# True = contrarian (fade the retail crowd, the recommended default).
# False = trend-following ("with the crowd"). See score_myfxbook_sentiment().
MYFXBOOK_CONTRARIAN = True
```

### 3b. `load_ui_config()` — add to the `global` declaration line and add
the load logic (match wherever the Telegram globals are loaded):

```python
global MYFXBOOK_ENABLED, MYFXBOOK_EMAIL, MYFXBOOK_PASSWORD, MYFXBOOK_CONTRARIAN
...
mfb_cfg = cfg.get("myfxbook", {})
MYFXBOOK_ENABLED = bool(mfb_cfg.get("enabled", MYFXBOOK_ENABLED))
MYFXBOOK_EMAIL = mfb_cfg.get("email", MYFXBOOK_EMAIL)
MYFXBOOK_PASSWORD = mfb_cfg.get("password", MYFXBOOK_PASSWORD)
MYFXBOOK_CONTRARIAN = bool(mfb_cfg.get("contrarian", MYFXBOOK_CONTRARIAN))
```

### 3c. `get_macro_snapshot_safe()` — pass credentials through only when enabled:

```python
def get_macro_snapshot_safe():
    try:
        metal = "GOLD" if SYMBOL.upper().startswith(("GOLD", "XAU")) else "SILVER"
        mfb_symbol = "XAUUSD" if metal == "GOLD" else "XAGUSD"
        return macro_data.get_macro_snapshot(
            metal,
            myfxbook_email=(MYFXBOOK_EMAIL if MYFXBOOK_ENABLED else None),
            myfxbook_password=(MYFXBOOK_PASSWORD if MYFXBOOK_ENABLED else None),
            myfxbook_symbol=mfb_symbol,
        )
    except Exception:
        logger.exception("macro_data.get_macro_snapshot() failed — macro_bias strategy will score 0/0 this scan.")
        return None
```

(Keep the existing function name/structure on the VPS — only add the two
new `myfxbook_*` kwargs to the call.)

### 3d. `build_market_data()` — add the contrarian flag to the returned dict:

Find the `return {...}` at the end of `build_market_data()` and add
`"myfxbook_contrarian": MYFXBOOK_CONTRARIAN` as the last key, e.g.:

```python
    return {"d1": df_d1, "h4": df_h4, "h1": df_h1, "m15": df_m15, "m5": df_m5, "m1": df_m1,
            "now": datetime.now(), "dom": dom, "macro": macro,
            "myfxbook_contrarian": MYFXBOOK_CONTRARIAN}
```

(Don't remove any existing keys — just append this one.)

---

## 4. `strategy_config_ui.py`

### 4a. `DEFAULT_CONFIG["confluence"]["strategies"]` — add as the last entry
in that nested dict:

```python
            # 25th: Myfxbook public Community Outlook (retail sentiment).
            # Scores 0/0 gracefully until Myfxbook is enabled + credentials
            # are set in the "Myfxbook Sentiment" tab below.
            "myfxbook_sentiment": {"enabled": True, "weight": 0.8},
```

**Weight must stay at `0.8` — strictly lower than `macro_bias`'s `1.2`.**
Confirm `macro_bias` is `{"enabled": True, "weight": 1.2}` on the VPS
copy too; if for some reason it differs, set `myfxbook_sentiment`'s
weight to whatever keeps it below `macro_bias`, and flag the discrepancy
back to the user rather than silently picking a number.

### 4b. `DEFAULT_CONFIG` top level — add a new `"myfxbook"` section, right
after the existing `"telegram"` section:

```python
    "myfxbook": {
        "enabled": False,
        "email": "",
        "password": "",
        "contrarian": True,
    },
```

### 4c. `STRATEGY13_LABELS` (or whatever the display-name lookup dict is
called on this VPS copy) — add:

```python
    "myfxbook_sentiment": "25. Myfxbook Retail Sentiment (Community Outlook)",
```

### 4d. Notebook tab setup — add right after wherever the Telegram tab is
added:

```python
self.tab_myfxbook = ttk.Frame(nb)
nb.add(self.tab_myfxbook, text="Myfxbook Sentiment")
...
self.build_myfxbook_tab(self.tab_myfxbook)
```

### 4e. New method — add right after `build_telegram_tab` (or wherever the
Telegram tab builder lives):

```python
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
             "⚠️ อย่าแชร์อีเมล/รหัสผ่านนี้ให้ใครเห็น (รวมถึงไม่ต้องพิมพ์ใส่ในแชทกับ Claude) — \n"
             "ระบบจะส่งไปที่ myfxbook.com โดยตรงเท่านั้น และเก็บแค่ session token ไว้ในเครื่อง ไม่เก็บรหัสผ่าน",
        wraplength=680, justify="left",
    ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
    self.reg_entry(frame, s, "email", "Myfxbook Email:", row=2, numeric_type=str, width=30)
    self.reg_entry(frame, s, "password", "Myfxbook Password:", row=3, numeric_type=str, width=20, show="*")
    self.reg_bool(
        frame, s, "contrarian",
        "Contrarian (Fade the crowd) — ติ๊กออกถ้าต้องการแบบ Trend-following (ตามฝูงชน) แทน",
        row=4,
    )
```

---

## 5. `generate_dashboard.py`

Cosmetic only — bump the strategy count text from "(24)" to "(25)":
- the comment `# ---- confluence multi-strategy (24) scores ----` → `(25)`
- the heading `<h2>Multi-Strategy Confluence (24) — Live Scores</h2>` → `(25)`

No other changes needed — the score table already iterates
`strategy_scores.json`'s `scores` dict directly with no hardcoded
strategy list, so the 25th strategy appears automatically once the bot
scores it.

---

## 6. `CLAUDE.md` (VPS copy)

Add this section right before "## Hard rules":

```markdown
## Strategies (25 total)

24 price/order-flow/macro strategies plus a 25th: **Myfxbook Retail
Sentiment** (`score_myfxbook_sentiment` in `strategies.py`, fetched by
`fetch_myfxbook_sentiment()` in `macro_data.py`). Reads `data["macro"]
["myfxbook_sentiment"]` — scores 0/0 gracefully until enabled with valid
Myfxbook credentials in `strategy_config.json` under `"myfxbook"` (or the UI's
"Myfxbook Sentiment" tab). Contrarian by default (`MYFXBOOK_CONTRARIAN`/
`"contrarian"` flag) — fades the crowd rather than following it; flip to
trend-following via the same flag. **Weight (0.8) is intentionally kept
below `macro_bias`'s weight (1.2)** — retail sentiment from one broker is
a secondary confirming vote, not a primary signal; do not raise it above
macro_bias without the user explicitly approving that tradeoff.
```

And update the existing hard rule from:
> Never print, log, or transmit the Telegram `bot_token` / `chat_id` from
> `strategy_config.json` beyond this VPS.

to:
> Never print, log, or transmit the Telegram `bot_token` / `chat_id`, or
> the Myfxbook `email` / `password`, from `strategy_config.json` beyond
> this VPS.

---

## After applying all 6 changes

1. Run a syntax check on all 5 edited Python files (`python -m py_compile`
   or `ast.parse` on each).
2. Confirm `strategy_config.json` on the VPS still loads cleanly through
   whatever deep-merge/default-fill logic `strategy_config_ui.py` already
   uses, so existing VPS settings aren't lost — the new `"myfxbook"`
   section and `"myfxbook_sentiment"` strategy entry should be filled in
   from defaults automatically, not require a manual edit.
3. Confirm `macro_bias` weight is still `1.2` and `myfxbook_sentiment`
   weight is `0.8` in the final merged config — do not let any later
   change push myfxbook's weight to equal or exceed macro_bias's.
4. Leave `MYFXBOOK_ENABLED` / `"myfxbook"."enabled"` as `False` — do NOT
   turn this on or enter any credentials yourself. The user will fill in
   their own Myfxbook email/password via the UI when ready.
5. Do not restart the live trading bot process as part of this sync
   unless the user explicitly asks for that — applying config/code
   changes and confirming syntax is enough for this task.
