# Prompt for Claude Code — fix 5 audit findings (verify-before-apply)

Paste this into Claude Code inside this folder (`RoBotTrading man 0 USV9` —
the primary, git-tracked copy; see `CLAUDE.md`). These come from a code
audit done on 2026-06-25 against the live log (`logs/xauusd_ea.log`),
`bot_state.json`, and `strategy_config.json`. **None of these are
hypothetical** — each one is backed by a specific log line or file state
quoted below, not a guess.

## Ground rule for this whole session

**Verify and test BEFORE touching the live bot code, every single item.**
For each item below:

1. Read the actual current code first — don't assume the line numbers or
   snippets here are still exactly right; the file may have moved since
   this audit.
2. Make the change as a small, isolated diff.
3. Run `python -m py_compile` on every file you touched.
4. Where a synthetic/unit test is described below, run it and show the
   output before moving to the next item.
5. Report back what you changed and what you verified — do NOT silently
   bundle multiple items into one untested commit.
6. **Do NOT restart the live trading bot.** Apply and verify each fix,
   then stop and let the user decide when to restart (the bot already
   has two prior fixes — `symbol_normalize.py` and `is_market_open()` —
   sitting on disk unused because the running process predates both
   commits; restarting is what activates all of this at once, so the
   user should choose that moment deliberately, not Claude Code).
7. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, lot
   sizing, or any other risk parameter as a side effect of these fixes.
8. Never print, log, or paste the Telegram `bot_token`/`chat_id` or
   Myfxbook credentials from `strategy_config.json` anywhere outside
   this VPS.

---

## Item 1 — Confirm restart status, don't fix (informational only)

**Finding:** `bot_state.json` shows the running process started at
`2026-06-25T01:22:14`. The commits for `symbol_normalize.py` (`29d5bd5`,
01:37:38) and `is_market_open()` (`ab4760c`, 01:44:44) landed *after* that
start time. Python doesn't hot-reload code, only config — so the live
process is currently running without either fix, even though both are on
disk. The 115 `retcode=10018 "Market closed"` order failures in the log
(04:00–05:01 the prior day) are exactly the failure mode `is_market_open()`
exists to prevent.

**What to do:** Nothing code-wise. Just re-confirm this is still true
(`cat bot_state.json`, `git log -3 --format="%h %ad %s" --date=iso`) and
report current status — if the user has since restarted the bot, this
item is already resolved and you can skip it. Do not restart it yourself.

---

## Item 2 — Day Trade and Scalping share one global trade-interval cooldown

**Finding:** `_LAST_ORDER_TIME` (module-level global, ~line 137) and
`check_trade_interval()` (~line 1628) are shared across `confluence13`,
the Day Trade group, and the Scalping Trade group. `MIN_TRADE_INTERVAL_MINUTES
= 20` means a Day Trade fill blocks Scalping Trade entries for 20 minutes,
and vice versa — defeating the purpose of having a separate fast-cadence
scalping group. Confirmed in the log: `news_fade` (Day Trade) sat in a
20-minute cooldown for ~27 minutes straight; any Scalping Trade signal in
that same window would have been silently blocked by the same timer.

**Fix approach (additive, config-flag-driven, matching existing pattern):**

- Replace the single `_LAST_ORDER_TIME` with a per-group dict, e.g.
  `_LAST_ORDER_TIME_BY_GROUP = {"confluence13": None, "day_trade": None,
  "scalping_trade": None}`.
- `check_trade_interval()` takes a `group` argument and reads/writes only
  that group's entry.
- Update every call site (`run_confluence_scan()`, the Day Trade branch
  and Scalping Trade branch inside `run_logic_groups_scan()`) to pass its
  own group name.
- Add a new config field, e.g. `risk.min_trade_interval_minutes_scalping`,
  defaulting to a SHORTER value than the Day Trade one (the user should
  decide the actual number — propose something like 5 minutes and ask,
  don't silently pick a value for a risk parameter) — keep
  `min_trade_interval_minutes` governing Day Trade/confluence13 as today,
  add the new one for Scalping Trade only. This is additive: if the new
  key is absent from `strategy_config.json`, default to the SAME 20-minute
  value as today, so behavior is unchanged until the user explicitly opts
  into a separate scalping cooldown via the UI.
- Add a matching field + UI control to `strategy_config_ui.py`'s
  Logic Groups or Scalping tab.

**Verification before calling this done:**
1. `python -m py_compile xauusd_mt5_strategy.py strategy_config_ui.py`.
2. Unit-test `check_trade_interval()` directly (no MT5 needed — it only
   touches the dict and `datetime.now()`): simulate an order at t=0 for
   `"day_trade"`, then call `check_trade_interval("scalping_trade")`
   immediately after and confirm it returns `(False, None)` (NOT blocked)
   — this is the bug being fixed, so this specific assertion must pass.
   Then confirm `check_trade_interval("day_trade")` immediately after IS
   blocked (existing behavior preserved for same-group calls).
3. Confirm `strategy_config.json` still loads with no new key present
   (backward-compat default test), and again with the new key present.
4. Do NOT change the existing 20-minute Day Trade/confluence13 value.

---

## Item 3 — Signal-detected Telegram alert has no de-duplication

**Finding:** `send_telegram(telegram_alert.format_signal_alert(...))` is
called (three call sites: ~line 1995, ~line 2269, ~line 2682) immediately
when a candidate signal qualifies, BEFORE `check_trade_interval()` is
checked a few lines later. Every scan tick (every `scan_interval_seconds`,
default 30s) that a persisting signal still qualifies, it fires again. Log
evidence: the same `news_fade` LONG setup logged ~17 times across 27
minutes, all but the first blocked by cooldown — with `NOTIFY_SIGNAL` on,
that's ~17 near-duplicate Telegram messages for one real signal. Every
other alert in this codebase (`check_market_hours_and_notify()`,
`check_macro_update_notify()`, `check_pre_news_notify()`) uses a
one-time-per-transition pattern; this one doesn't.

**Fix approach (match the existing one-time-per-transition pattern):**

- Add a small per-group "last alerted signal fingerprint" cache, e.g.
  `_LAST_SIGNAL_ALERT_KEY = {"confluence13": None, "day_trade": None,
  "scalping_trade": None}`, where the fingerprint is something stable
  across repeated scans of the SAME unfilled setup but distinct for a
  genuinely new one — e.g. `(strategy_key, direction, round(entry, 1))`
  or include a coarse time bucket so a setup that lingers for hours still
  re-alerts eventually (use your judgement on the bucket size, but default
  to something conservative like re-alert after 1 hour if still pending,
  so the user isn't permanently blind to a long-lived signal).
- Before calling `send_telegram(format_signal_alert(...))`, compare the
  new fingerprint to the cached one for that group; only send + update the
  cache if it changed (or the time bucket rolled over).
- Apply this identically at all three call sites.

**Verification before calling this done:**
1. `python -m py_compile xauusd_mt5_strategy.py`.
2. Synthetic test: call the alert-gating function/logic twice in a row
   with the identical fingerprint and confirm the second call does NOT
   trigger a send; then call it once more with a different direction or
   strategy key and confirm it DOES trigger a send.
3. Confirm a genuinely new signal (different strategy or direction) still
   alerts immediately — this fix must not silence real new signals, only
   collapse repeats of the same pending one.
4. Do NOT touch `format_signal_alert()`'s message content/formatting,
   only the call-gating around it.

---

## Item 4 — `climax_reversal_sr` missing from the persisted `strategy_config.json`

**Finding:** `strategies.STRATEGY_REGISTRY` and `STRATEGY13_LABELS` both
have 26 entries (confirmed via git log — `9b824e0`), but the live
`strategy_config.json`'s `confluence.strategies` dict has only 25 keys —
`climax_reversal_sr` is absent. The EA's config loader (~line 658-662)
silently backfills any registry key missing from the config file as
`enabled=True, weight=1.0`, so the strategy IS active at runtime — this is
not a crash — but the persisted file, and whatever the UI shows/saves, are
out of sync with the code, and a future UI save could write a different
implicit default than the recommended weight.

**Fix approach:**

- Confirm `strategy_config_ui.py`'s `DEFAULT_CONFIG["confluence"]["strategies"]`
  already has a `climax_reversal_sr` entry (per the original sync prompt,
  it should — `VPS_SYNC_CLIMAX_REVERSAL_SR_PROMPT.md`'s changes were
  applied per `git log`). If it's there, the simplest safe fix is to open
  the UI once, navigate to the strategies tab, and save — the existing
  `_deep_merge`-on-load pattern (same one that backfilled `myfxbook` before)
  should write the missing key into `strategy_config.json` on save. Verify
  this actually happens rather than assuming it.
- If `_deep_merge` does NOT backfill it correctly on save (test this
  first), then as a fallback, write a small one-off migration: read
  `strategy_config.json`, and if `climax_reversal_sr` is missing from
  `confluence.strategies`, add `{"enabled": true, "weight": 1.0}` and
  save — but only do this if the UI-save path doesn't already solve it,
  since the UI path is preferred (it's the existing, already-trusted
  mechanism).

**Verification before calling this done:**
1. After whichever fix path is used, `python -c "import json;
   c=json.load(open('strategy_config.json')); print('climax_reversal_sr'
   in c['confluence']['strategies'])"` must print `True`.
2. Confirm no OTHER strategy's enabled/weight value changed as a side
   effect (diff the file before/after, only one new key should appear).
3. Confirm the dashboard's strategy count still reads 26 after
   regenerating it (`python generate_dashboard.py`).

---

## Item 5 — Macro data sources degraded (informational + optional resilience improvement)

**Finding:** Log shows repeated `comex_gold: fetch failed (HTTPError: HTTP
Error 403: Forbidden)` (falls back to COT proxy) and `etf_gld: SPDR failed
(ValueError('bot-wall response...'))` (falls back to Yahoo proxy) in
`macro_data.py`. This isn't a code bug — the fallback logic is working as
designed — but it means `score_macro_bias` (weight 1.2, the single
highest-weighted strategy in the whole confluence stack) is currently
scoring off proxy data rather than the primary CME/SPDR sources, every
scan, until those endpoints stop blocking the bot's requests.

**What to do:** Don't "fix" this by trying to bypass CME's/SPDR's bot
protection (no scraping workarounds, no spoofed headers to evade a
bot-wall — that's the kind of fragile, possibly-ToS-violating change this
audit should NOT recommend). Instead:

- Confirm the existing fallback path is logged clearly enough that this is
  diagnosable at a glance (it already is, per the log — no change needed
  there).
- Optionally, add one thing only if the user wants it: a dashboard/Telegram
  note when macro_bias has been running on proxy data for an extended
  period (e.g. >24h), so degraded data quality is visible somewhere
  persistent, not just buried in the log. This is optional — ask the user
  before building it, don't add it unprompted.

---

## Minor — duplicate config-reload trigger (investigate only, don't fix blind)

**Finding:** Log shows two "strategy_config.json changed on disk —
reloading settings live" lines 3 seconds apart (22:19:31 and 22:19:34) for
what looks like a single UI save. Possibly a double filesystem-watch
event, or two near-simultaneous writes (e.g. an atomic-write temp-file
rename triggering two events).

**What to do:** Investigate `maybe_reload_config()`'s file-watch mechanism
(mtime check? hash check? watchdog library?) and `strategy_config_ui.py`'s
`save()` to see if save() does two writes (e.g. a backup write + the real
write) that could both trigger a reload. If you find a real double-write,
fix it. If it's just two near-simultaneous filesystem events for one
logical save with no actual harm (the reload is idempotent either way),
it's fine to leave as-is — report back which it is rather than changing
something that isn't actually broken.

---

## Summary checklist to report back

- [ ] Item 1: restart status re-confirmed (no code change)
- [ ] Item 2: per-group cooldown implemented + unit-tested
- [ ] Item 3: signal-alert de-dupe implemented + unit-tested
- [ ] Item 4: `climax_reversal_sr` persisted in `strategy_config.json`,
      verified, dashboard count re-confirmed at 26
- [ ] Item 5: no code change made (informational); flagged whether user
      wants the optional staleness notice
- [ ] Minor: root-caused the double-reload log lines, fixed only if it's
      an actual double-write
- [ ] All touched files pass `py_compile`
- [ ] No risk parameter changed except the new, additive, opt-in scalping
      cooldown field (which defaults to unchanged behavior)
- [ ] Live bot NOT restarted
