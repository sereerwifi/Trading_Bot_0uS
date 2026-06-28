# Prompt for Claude Code — fix double daily-filter veto + dead proxy-staleness registry (verify-before-apply)

Paste this into Claude Code inside this folder (`RoBotTrading man 0 USV9` —
the primary, git-tracked copy on the VPS; see `CLAUDE.md`). These two
findings come from an audit done on 2026-06-28 against a Mac-side mirror of
this repo (`logs/xauusd_ea.log`, `bot_state.json`, `strategy_config.json`,
and the uncommitted working changes to `macro_data.py` / `xauusd_mt5_strategy.py`
/ `telegram_alert.py` / `generate_dashboard.py`). **Neither finding is
hypothetical** — each is backed by a specific log line or file state quoted
below. The fix for both has already been written and verified on the Mac
mirror; this prompt's job is to confirm the same state exists on the VPS
copy (it may differ — the two copies are not continuously synced) and apply
the equivalent fix here.

## Ground rule for this whole session

1. Read the actual current code/config here first — don't assume the
   mirror's state matches the VPS exactly; confirm independently.
2. Make each change as a small, isolated diff.
3. Run `python -m py_compile` on every file you touch.
4. Run the synthetic test described in Item 2 before calling it done.
5. **Do NOT restart the live trading bot.** Apply and verify, then stop —
   the config change (Item 1) hot-reloads on its own (the bot already
   polls `strategy_config.json` for changes — confirm via the log line
   `"strategy_config.json changed on disk — reloading settings live"`).
   The code change (Item 2) does NOT hot-reload and needs a restart to
   take effect; let the user choose that moment.
6. Do NOT change `MIN_STRATEGY_SCORE`, `MIN_AGREEING_STRATEGIES`, lot
   sizing, or any other risk parameter as a side effect of these fixes.
7. Never print, log, or paste the Telegram `bot_token`/`chat_id` or
   Myfxbook `email`/`password` from `strategy_config.json` anywhere
   outside this VPS.

---

## Item 1 — `logic_groups_apply_daily_filter` is re-introducing the already-fixed double-veto bug

**Finding:** `strategy_config.json`'s `confluence.logic_groups_apply_daily_filter`
was found set to `true` on the Mac mirror. `xauusd_mt5_strategy.py` (around
line 2394-2399) explicitly documents why this must default to `false`:

```
# The group's own Step-1 bias cascade already served as this trade's
# trend filter (see _build_group_signal/get_group_bias). The global D1
# Daily Filter is a second, independent trend filter and is OFF by
# default here (LOGIC_GROUPS_APPLY_DAILY_FILTER) to avoid double-gating —
if DAILY_FILTER_ENABLED and LOGIC_GROUPS_APPLY_DAILY_FILTER:
    daily_bias = get_daily_bias()
    if daily_bias == "neutral":
        logger.info(f"Daily filter: Day trend is neutral/choppy — {group_label} signal vetoed.")
```

The log shows exactly this failure mode: `"Daily filter: Day trend is
neutral/choppy — Day Trade signal vetoed."` appears **362 times** between
2026-06-27 03:33 and 2026-06-28 13:42 with **zero** trades opened in that
window (no `"order placed"` / `"opened position"` lines at all), right
after the last clean signal at 2026-06-26 ~23:08 (`"both groups fired this
scan (Day Trade=long, Scalping Trade=long) — taking Day Trade"`). That's
roughly 34 hours of the bot silently not trading because both the group's
own bias cascade AND the global D1 filter must agree and be non-neutral at
once — the exact regression this flag exists to prevent.

**What to do:**

1. Confirm: `grep -n "logic_groups_apply_daily_filter" strategy_config.json
   xauusd_mt5_strategy.py` and check the current live value.
2. If it's `true`, change it to `false` in `strategy_config.json`
   (`confluence.logic_groups_apply_daily_filter`). This is a one-line JSON
   edit, not a code change.
3. Confirm the running bot picks it up live — watch the log for
   `"strategy_config.json changed on disk — reloading settings live"`
   followed by an `"Entry mode: logic_groups"` line. No restart needed for
   this one.
4. Also check whether the Scalping Trade group fired/veto-logged at all in
   the same 2026-06-27 03:33 → 2026-06-28 13:42 window — the mirror's log
   showed **zero** `"Scalping Trade signal vetoed"` lines in that period
   (only Day Trade), which is worth a second look once Day Trade is
   unblocked: confirm Scalping Trade is actually being evaluated each scan
   and not silently skipped for an unrelated reason (e.g.
   `LOGIC_GROUP_SELECTION` momentarily not `"both"`, or a Scalping-specific
   bias cascade returning neutral for a different reason). Report what you
   find — don't fix anything here unless you find a second real bug.

---

## Item 2 — `_PROXY_SOURCE_NAMES` is empty, so the proxy-staleness alert/badge (already built) never fires

**Finding:** The macro-proxy-staleness feature from
`PROMPT_macro_proxy_staleness_notice.md` is fully implemented end-to-end on
the Mac mirror — `update_proxy_fallback_state()` / `get_proxy_staleness_report()`
in `macro_data.py`, `check_proxy_staleness_notify()` in
`xauusd_mt5_strategy.py`, `format_proxy_staleness_alert()` in
`telegram_alert.py`, and the dashboard badge in `generate_dashboard.py` are
all correctly wired together. But `macro_data.py`'s `_PROXY_SOURCE_NAMES`
registry — the dict that's supposed to list each data key's actual
fallback/proxy source tags — was left as an empty dict:

```python
_PROXY_SOURCE_NAMES: "dict[str, set[str]]" = {}
```

with a comment reasoning that since the CME/SPDR/FRED blocks on this VPS
are permanent, "the warnings are not actionable." That directly contradicts
the original prompt's stated purpose: the feature exists *because* the
block is persistent and the user wants continuous dashboard visibility into
`macro_bias` (weight 1.2, the single highest-weighted strategy) running on
degraded data for as long as that lasts. With the registry empty,
`update_proxy_fallback_state()`'s `is_proxy = source in proxy_sources`
check is always `False` against an empty set — the alert and badge can
never fire, no matter how long a source has been on its fallback path. The
log confirms the bot is in fact on fallback sources right now:
`"comex_gold: CME blocked, using COT proxy"` and `"etf_gld: SPDR failed
(...bot-wall...), using Yahoo price proxy"` recur every macro refresh
cycle.

**What to do:**

1. Confirm the current `"source"` tag strings in this VPS copy of
   `macro_data.py` match what's below — re-read
   `_fetch_comex_via_cot_proxy`/`_fetch_comex_inventory_raw`,
   `_fetch_etf_flow_via_yahoo`/`_fetch_etf_flow_raw`, `_fetch_yield10y_raw`,
   and `_fetch_fed_expectation_raw` directly; do not assume the mirror's
   line numbers are current.
   - `comex_gold` / `comex_silver`: primary `"cme_xls"`, proxy `"cot_proxy"`
   - `etf_gld` / `etf_slv`: primary `"spdr_csv"`, proxy `"yahoo_proxy"`
   - `yield10y`: primary `"FRED_DGS10"`, proxy `"yahoo_TNX"`
   - `fed_expectation`: primary `"FRED_DGS2"`, proxy `"yahoo_FVX"`
2. Replace the empty `_PROXY_SOURCE_NAMES` dict with:
   ```python
   _PROXY_SOURCE_NAMES: "dict[str, set[str]]" = {
       "comex_gold":      {"cot_proxy"},
       "comex_silver":    {"cot_proxy"},
       "etf_gld":         {"yahoo_proxy"},
       "etf_slv":         {"yahoo_proxy"},
       "yield10y":        {"yahoo_TNX"},
       "fed_expectation": {"yahoo_FVX"},
   }
   ```
   (adjust if step 1 found different tag strings on this copy).
3. `python -m py_compile macro_data.py xauusd_mt5_strategy.py
   telegram_alert.py generate_dashboard.py`.
4. Synthetic test (same as the original prompt's Step 2 verification, no
   live MT5/network needed): call `update_proxy_fallback_state("comex_gold",
   {"source": "cot_proxy"})` twice an hour apart and confirm `since` in
   `proxy_fallback_state` stays fixed at the first call's timestamp, then
   call it once with `{"source": "cme_xls"}` and confirm the row clears.
   Then confirm `get_proxy_staleness_report()` returns the right entries
   before/after.
5. This is a code change — it needs a bot **restart** to take effect.
   Do NOT restart it yourself; report that it's ready and let the user
   choose when.
6. Confirm `score_macro_bias()`'s actual scoring math is untouched — this
   fix only populates a lookup table the existing tracker reads from; it
   must not change what `macro_bias` votes.

---

## Report back

For Item 1: confirm the live value before/after, confirm the hot-reload log
line appeared, and report what you found re: Scalping Trade's silence in
that window. For Item 2: confirm the actual current `"source"` tag strings
on this VPS copy (they may have drifted from the mirror), confirm the
synthetic test results, and confirm both files still compile.
