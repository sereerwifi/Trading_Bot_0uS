# Prompt for Claude Code — real market-open/closed detection + price freshness/accuracy checks

Paste this into Claude Code in the bot project folder (local copy or VPS
copy — flag the other one for sync afterward per the existing CLAUDE.md
rule). Purely additive: new checks/notifications only, no change to entry
logic, risk, lot sizing, or the broker order symbol.

## What the user asked for (context, in their words)

ตรวจสอบเวลาเปิด-ปิดของตลาด XAUUSD/GOLD จริง ๆ เพื่อให้บอทหยุดทำงานตอนตลาดปิด
(ไม่ใช่แค่ "ช่วงเวลาที่อยากเทรด" ที่มีอยู่แล้ว) และให้ราคา/ข้อมูลที่บอทใช้
แม่นยำที่สุด อัปเดตใกล้เคียงราคาตลาดจริงที่สุด.

## Why this is a NEW feature, not already covered

`xauusd_mt5_strategy.py` already has `TRADING_HOURS_FILTER_ENABLED` /
`ALLOWED_SESSIONS` / `is_within_trading_hours()` (around line 154-176 and
1354+) — but that is a **user-preference window** ("only trade during the
London/NY overlap"), purely based on this machine's local clock, with zero
awareness of whether the market is actually open. Default is `"all_day"`,
so right now the bot has no real check at all for: weekends, broker
holidays, or the broker's own feed going stale/disconnected while the
process is still technically "running." This task adds that missing,
broker-ground-truth check, separate from (and on top of) the existing
preference filter.

## Step 1 — `xauusd_mt5_strategy.py`: real market-open detection

Add this new section right after the existing `TRADING_SESSIONS` /
`ALLOWED_SESSIONS` block (after line ~177):

```python
# --- Real market-open/closed detection (broker ground truth, NOT the
# user-preference session filter above). TRADING_HOURS_FILTER_ENABLED /
# ALLOWED_SESSIONS answer "do I WANT to trade right now"; the settings
# below answer "CAN I trade right now at all" -- weekends, broker
# holidays, or a stalled/disconnected price feed. Both gates are checked
# independently; either one can block a new entry.
MARKET_HOURS_CHECK_ENABLED = True
MARKET_CLOSED_MAX_TICK_AGE_SEC = 180   # if the broker's last tick for SYMBOL
                                         # is older than this during what
                                         # should be live trading, treat the
                                         # feed as stale/closed rather than
                                         # scoring strategies on frozen price.
MARKET_CLOSED_NOTIFY = True             # one Telegram alert when the market
                                         # transitions open->closed, and one
                                         # when it transitions closed->open
                                         # (never spammed every loop tick).
```

Add the detection function near `is_within_trading_hours()` (after it, so
both live together around line ~1380):

```python
_LAST_MARKET_OPEN_STATE = None  # None = unknown yet (first check this run);
                                  # True/False afterward, used only to fire
                                  # the open<->closed Telegram alert once per
                                  # transition, never every loop tick.


def is_market_open(max_tick_age_sec=None):
    """Broker ground-truth check for whether SYMBOL can actually be traded
    right now -- weekends, broker holidays, and a stalled/disconnected
    price feed all show up here, independent of is_within_trading_hours()'s
    user-preference window. Returns (open: bool, reason: str).

    Two independent checks, either one can mark the market closed:
      1. mt5.symbol_info(SYMBOL).trade_mode -- the broker's own flag for
         "this symbol cannot be traded right now" (SYMBOL_TRADE_MODE_DISABLED).
         This is what actually reflects broker holidays/maintenance, not
         just the standard weekend.
      2. Tick freshness -- if the last tick for SYMBOL is older than
         `max_tick_age_sec`, the feed is either disconnected or the market
         is closed (during a real weekend gap, MT5 simply stops sending new
         ticks, so the last one ages indefinitely). This also catches the
         broker silently dropping the data connection while still reporting
         "connected" at the terminal level.
    """
    if not MARKET_HOURS_CHECK_ENABLED:
        return True, "market-hours check disabled"

    max_age = max_tick_age_sec if max_tick_age_sec is not None else MARKET_CLOSED_MAX_TICK_AGE_SEC

    sym_info = mt5.symbol_info(SYMBOL)
    if sym_info is None:
        return False, f"symbol_info({SYMBOL}) unavailable -- not subscribed / not found"
    if sym_info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        return False, f"broker reports {SYMBOL} trading disabled (trade_mode=DISABLED) -- market closed/holiday"

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None or not tick.time:
        return False, f"no tick data for {SYMBOL} -- feed disconnected or market closed"

    tick_age = time.time() - tick.time
    if tick_age > max_age:
        return False, (f"last tick for {SYMBOL} is {tick_age:.0f}s old "
                        f"(> {max_age}s threshold) -- feed stale or market closed")

    return True, f"market open (last tick {tick_age:.0f}s ago)"


def check_market_hours_and_notify():
    """Wraps is_market_open() with the one-time-per-transition Telegram
    alert. Call this once per main-loop iteration, before any new-entry
    scan. Returns the same (open, reason) tuple is_market_open() would --
    callers gate new entries on the `open` value exactly like they already
    gate on is_within_trading_hours()."""
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
        logger.info(f"Market status changed -> {'OPEN' if is_open else 'CLOSED'} ({reason})")
    _LAST_MARKET_OPEN_STATE = is_open
    return is_open, reason
```

## Step 2 — wire it into the main loop

In `main()`'s `while True:` block, find the section right before the
new-entry scan dispatch (around line ~2536, just above the
`scan_interval = ...` line) and add the market-hours gate. Change:

```python
                # New-entry scan. ENTRY_MODE selects which engine runs and on
                # what cadence: ...
                scan_interval = SCAN_INTERVAL_SECONDS if ENTRY_MODE in ("confluence13", "logic_groups") else POLL_SECONDS
                if now - last_signal_scan >= scan_interval:
```

to:

```python
                # Real market-open check (broker ground truth) -- separate
                # from is_within_trading_hours()'s user-preference window.
                # Runs every loop tick (cheap: one symbol_info + one tick
                # read) so a weekend close / feed outage is caught within
                # one TRAILING_CHECK_SECONDS cycle, not just at scan time.
                market_open, market_reason = check_market_hours_and_notify()

                # New-entry scan. ENTRY_MODE selects which engine runs and on
                # what cadence: ...
                scan_interval = SCAN_INTERVAL_SECONDS if ENTRY_MODE in ("confluence13", "logic_groups") else POLL_SECONDS
                if not market_open:
                    logger.debug(f"Market closed/feed stale ({market_reason}) -- no new-entry scan this tick.")
                elif now - last_signal_scan >= scan_interval:
```

Note this only skips the **new-entry scan** — `manage_breakeven()`,
`manage_trailing_stops()`, and `check_basket_close()` (just above it in the
loop) keep running unconditionally, exactly as they do today. That's
intentional: if there's an existing open position when the market closes,
those management functions should keep trying every tick so they act
immediately the moment the market reopens and ticks resume, rather than
being gated by the same flag.

## Step 3 — `telegram_alert.py`: two new alert formatters

Add alongside the existing `format_*_alert` functions:

```python
def format_market_closed_alert(symbol, reason):
    return (f"⏸️ <b>{symbol_normalize.display_label(symbol) if 'symbol_normalize' in globals() else symbol} — Market Closed</b>\n"
            f"New-entry scanning is paused.\n"
            f"Reason: {reason}\n"
            f"Open positions (if any) keep being managed (trailing/basket-close) "
            f"as normal.")


def format_market_reopened_alert(symbol, reason):
    return (f"▶️ <b>{symbol_normalize.display_label(symbol) if 'symbol_normalize' in globals() else symbol} — Market Reopened</b>\n"
            f"New-entry scanning has resumed.\n"
            f"Status: {reason}")
```

(The `symbol_normalize` reference is defensive — if you've already applied
the earlier `PROMPT_unify_xauusd_gold_symbol_naming.md` patch, this will
show the unified "GOLD (XAUUSD)"-style label; if not, it just falls back to
the plain symbol string. No hard dependency either way.)

## Step 4 — `strategy_config_ui.py`: expose the new settings

Add a small new section (its own tab or a panel inside the existing
"ช่วงเวลาเทรด" tab — follow whatever the existing tab-creation helper pattern
is) labelled **"ตรวจสอบตลาดเปิด-ปิด / ความแม่นยำราคา"** with:

- Checkbox: "เปิดใช้การตรวจสอบตลาดเปิด-ปิดจริง (MARKET_HOURS_CHECK_ENABLED)"
- Number field: "อายุ tick สูงสุดก่อนถือว่าตลาดปิด/feed ค้าง (วินาที)" bound to
  `MARKET_CLOSED_MAX_TICK_AGE_SEC`, default `180`
- Checkbox: "แจ้งเตือน Telegram ตอนตลาดปิด/เปิดใหม่ (MARKET_CLOSED_NOTIFY)"
- Checkbox: "เปิดใช้การตรวจสอบราคาเทียบแหล่งอ้างอิงภายนอก (MARKET_PRICE_SANITY_CHECK_ENABLED)" (see Step 5)
- Number field: "ค่าเบี่ยงเบนสูงสุดที่ยอมรับได้ (%) (MARKET_PRICE_SANITY_TOLERANCE_PCT)", default `1.0`

Add matching keys to `DEFAULT_CONFIG` (a new top-level
`"market_hours"` dict, mirroring how `"trading_hours"` is already
structured) and to whatever `maybe_reload_config()`-style section reads it
back in `xauusd_mt5_strategy.py` — follow the exact existing pattern used
for `TRADING_HOURS_FILTER_ENABLED` / `ALLOWED_SESSIONS` (see
`load_ui_config()` around line ~591-595) so this hot-reloads the same way,
no restart needed:

```python
    mh = cfg.get("market_hours", {})
    MARKET_HOURS_CHECK_ENABLED = bool(mh.get("enabled", MARKET_HOURS_CHECK_ENABLED))
    MARKET_CLOSED_MAX_TICK_AGE_SEC = float(mh.get("max_tick_age_sec", MARKET_CLOSED_MAX_TICK_AGE_SEC))
    MARKET_CLOSED_NOTIFY = bool(mh.get("notify", MARKET_CLOSED_NOTIFY))
    MARKET_PRICE_SANITY_CHECK_ENABLED = bool(mh.get("price_sanity_enabled", False))
    MARKET_PRICE_SANITY_TOLERANCE_PCT = float(mh.get("price_sanity_tolerance_pct", 1.0))
```

(Add the corresponding `global` declarations alongside the existing
`TRADING_HOURS_FILTER_ENABLED, ALLOWED_SESSIONS` globals at the top of that
function.)

## Step 5 — price accuracy: free reference cross-check (sanity-log only, never used for trading)

This guards against the broker's own feed being technically "alive" (passes
Step 1's tick-freshness check) but quietly wrong or badly lagging —
something tick-age alone can't catch. Add to `macro_data.py`, next to the
other free/no-key fetchers (same `_http_get` helper they all use):

```python
# ----------------------------- price sanity cross-check -----------------------
def fetch_reference_gold_price():
    """Free, no-key spot/futures gold quote from Yahoo Finance (GC=F COMEX
    gold futures -- close enough to spot for a sanity check, same no-auth
    pattern as fetch_economic_calendar()'s ForexFactory feed). Used ONLY to
    sanity-check the broker's own tick against an independent source --
    NEVER fed into scoring or order placement. Returns None on any failure,
    never raises, same convention as every other fetcher here."""
    try:
        raw = _http_get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F")
        j = json.loads(raw)
        price = j["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return {"price": float(price), "source": "yahoo_gc_f", "fetched_at": time.time()}
    except Exception:
        return None
```

Add the check in `xauusd_mt5_strategy.py`, as a new function near
`is_market_open()`:

```python
MARKET_PRICE_SANITY_CHECK_ENABLED = False   # off by default -- adds one extra
                                              # HTTP call per check; opt in via UI.
MARKET_PRICE_SANITY_TOLERANCE_PCT = 1.0


def check_price_sanity():
    """Cross-checks the broker's current tick against a free independent
    reference price (macro_data.fetch_reference_gold_price()). Logs a
    warning if they diverge by more than MARKET_PRICE_SANITY_TOLERANCE_PCT
    -- this is a LOGGING/ALERTING aid only, it never overrides the broker
    price used for actual trading decisions or order placement. Returns
    True if the check passed (or was skipped/unavailable), False only if
    both prices were available AND diverged beyond tolerance."""
    if not MARKET_PRICE_SANITY_CHECK_ENABLED:
        return True
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return True  # nothing to compare -- Step 1 already flags this case
    ref = macro_data.fetch_reference_gold_price()
    if ref is None or not ref.get("price"):
        return True  # reference unavailable -- don't block on a 3rd-party outage
    broker_mid = (tick.bid + tick.ask) / 2.0
    diff_pct = abs(broker_mid - ref["price"]) / ref["price"] * 100.0
    if diff_pct > MARKET_PRICE_SANITY_TOLERANCE_PCT:
        logger.warning(
            f"Price sanity check: broker mid {broker_mid:.2f} vs reference "
            f"{ref['price']:.2f} ({ref['source']}) diverge by {diff_pct:.2f}% "
            f"(> {MARKET_PRICE_SANITY_TOLERANCE_PCT}% tolerance) -- broker feed "
            f"may be stale, mispriced, or quoting a different contract month."
        )
        return False
    return True
```

Call `check_price_sanity()` once per loop tick alongside
`check_market_hours_and_notify()` (same spot in `main()`'s loop) — log only,
do not gate new entries on it by default (it's informational; the user can
later decide to also block entries on failure once they've watched it run
for a while and trust the tolerance setting).

## Step 6 — `generate_dashboard.py`: market-status badge

Find the dashboard header (the same `<h1>...XAUUSD MT5 EA...</h1>` line
touched by the symbol-naming prompt, if already applied) and add a status
badge next to it, sourced from whatever the EA last wrote about market
state. Simplest approach matching the existing pattern (the EA already
writes `strategy_scores.json` / `bot_state.json` every tick) — add one more
field, `"market_open"` and `"market_reason"`, to whatever JSON snapshot the
EA writes each loop (wherever `record_bot_start()` / the per-tick state
write already happens), then in `generate_dashboard.py` render:

```python
market_badge = '🟢 OPEN' if snap.get('market_open') else '🔴 CLOSED/PAUSED'
```

and place `{market_badge}` next to the existing uptime/EA-status display.

## Verification before calling this done

1. `python -m py_compile xauusd_mt5_strategy.py telegram_alert.py
   strategy_config_ui.py macro_data.py generate_dashboard.py`.
2. Cannot fully test `is_market_open()` / `check_price_sanity()` without a
   live MT5 connection — at minimum, mock `mt5.symbol_info`,
   `mt5.symbol_info_tick`, and `time.time()` to confirm: (a) a fresh tick +
   enabled trade_mode returns `(True, ...)`; (b)
   `trade_mode == SYMBOL_TRADE_MODE_DISABLED` returns `(False, ...)`
   regardless of tick age; (c) a tick older than
   `MARKET_CLOSED_MAX_TICK_AGE_SEC` returns `(False, ...)` even with
   trading enabled.
3. Confirm the closed->open and open->closed Telegram alerts only fire
   once per transition (not every loop tick) by simulating a few
   consecutive `check_market_hours_and_notify()` calls with the mocked
   state flipping only once.
4. Confirm `manage_breakeven()` / `manage_trailing_stops()` /
   `check_basket_close()` are NOT gated by the new `market_open` flag (only
   the new-entry scan dispatch is) — re-read the diff in `main()`'s loop to
   make sure the gate was added in the right place.
5. Confirm `MARKET_HOURS_CHECK_ENABLED = False` fully restores today's
   behavior (no market-hours gating at all) — this should be the same as
   not having applied this patch.
6. Do NOT change `SYMBOL`, risk/lot parameters, or anything in the actual
   entry-signal scoring logic.
7. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running.
