# Prompt for Claude Code — macro_bias proxy-data staleness notice

Run this on the VPS, in the live bot folder (wherever `origin/main` is
currently checked out there — likely this same `RoBotTrading man 0 USV9`
path, but confirm with `git log -1` first since a Mac-side mirror of this
folder may be behind the VPS by several commits; don't assume the two are
in sync). This is Item 5's follow-up from the 2026-06-25 audit
(`PROMPT_fix_audit_findings_2026-06-25.md`) — purely additive, a
visibility feature, not a logic change to scoring.

## Ground rule — verify before applying, same as last time

1. Read the actual current code first (`macro_data.py`'s `fetch_comex_gold`
   / `_fetch_comex_via_cot_proxy`, `fetch_etf_flow` / its SPDR+Yahoo
   fallback, and however `get_macro_snapshot()` assembles the combined
   dict) — confirm the `"source"` field naming (`cot_proxy`, `yahoo_proxy`,
   etc.) still matches what's described below; it may have shifted.
2. Make this one isolated, additive change. No edits to scoring weights,
   `score_macro_bias()`'s math, or any risk parameter.
3. `python -m py_compile` every file touched.
4. Run the synthetic test described below before calling it done.
5. Do NOT restart the live bot — apply, verify, report back, let the user
   choose when to restart.
6. Never print/log/paste Telegram `bot_token`/`chat_id` or Myfxbook
   credentials anywhere outside the VPS.

## What this is

`macro_data.py` already tags each fetched data point with a `"source"`
field that's honest about whether it came from the primary feed (CME
COMEX, SPDR GLD) or a fallback proxy (`cot_proxy`, `yahoo_proxy`). Right
now that's only visible in the log. The audit found CME and SPDR both
currently blocking the bot's requests (403 / bot-wall), meaning
`macro_bias` — the single highest-weighted strategy (weight 1.2) — has
been silently running on proxy data with no persistent visibility outside
grepping the log.

Build a notice that fires once Telegram + shows continuously on the
dashboard when ANY macro data point feeding `macro_bias` has been on a
proxy/fallback source for more than a configurable threshold (default
24h), per the user's choice: **both** a one-time Telegram alert AND a
persistent dashboard badge.

## Step 1 — track "since when has this been on a proxy" per data point

In `macro_data.py`, wherever the combined macro snapshot dict is built
(`get_macro_snapshot()` or equivalent), each sub-fetch result already
carries a `"source"` key. Add a small persistent tracker — reuse the
existing SQLite history DB (`macro_data_history.db`) if there's a
sensible table already, or add a tiny new one, e.g.:

```sql
CREATE TABLE IF NOT EXISTS proxy_fallback_state (
    data_key TEXT PRIMARY KEY,      -- e.g. "comex_gold", "etf_gld"
    is_proxy INTEGER NOT NULL,      -- 1 if source is a known proxy/fallback name
    since TEXT NOT NULL             -- ISO timestamp of when it FIRST became a proxy
                                      -- (do not reset this on every fetch — only
                                      -- reset/clear when it goes back to primary)
);
```

Logic on each fetch cycle, per tracked key (`comex_gold`, `etf_gld`, and
any other macro input with a known proxy fallback — check
`fetch_fed_rate_expectation()`'s FRED→Yahoo `^FVX` fallback too, it has
the same pattern):

- If the new result's `"source"` is a known proxy name (`cot_proxy`,
  `yahoo_proxy`, `yahoo_FVX`, or whatever the actual fallback source tags
  are after Step 1's code-read) AND there's no existing row (or the
  existing row says `is_proxy=0`): insert/update with `is_proxy=1,
  since=now`.
- If the new result's `"source"` is a primary source: if there's an
  existing row with `is_proxy=1`, clear it (`is_proxy=0`, or delete the
  row) — this is the "back to normal" transition.
- If still on proxy and a row already exists with `is_proxy=1`: leave
  `since` untouched (don't reset the clock just because it fetched again
  on the same proxy).

## Step 2 — one-time Telegram alert at the threshold

Add a config field `macro_data.proxy_staleness_alert_hours` (default
`24`) and `macro_data.proxy_staleness_alert_enabled` (default `True`),
read the same way other config sections are loaded in
`xauusd_mt5_strategy.py`'s config-reload block.

Add a check (called once per macro refresh cycle, alongside however
`check_macro_update_notify()` is already invoked — follow that existing
function's one-time-per-transition pattern exactly, including a
module-level `_LAST_PROXY_ALERT_SENT` dict keyed by `data_key` so each
data point only alerts once until it recovers, not once per data point
per scan):

- For each tracked key where `is_proxy=1` and
  `(now - since) >= proxy_staleness_alert_hours` and no alert already
  sent for this `since` value: send a Telegram message via a new
  `telegram_alert.format_proxy_staleness_alert(data_key, hours_on_proxy,
  proxy_source_name, symbol=SYMBOL)` function (add it next to the
  existing `format_market_closed_alert` / `format_market_reopened_alert`
  for a consistent style), then record that the alert was sent for this
  `since` value.
- When a key recovers to primary (Step 1's "clear" branch), also clear
  its sent-alert record so a FUTURE proxy episode can alert again.

## Step 3 — persistent dashboard badge

In `generate_dashboard.py`, read the same `proxy_fallback_state` table (or
equivalent) and render a small badge near wherever `macro_bias` /
"Big Data" panel already lives — something like:

```
⚠ macro_bias: comex_gold on proxy data (cot_proxy) for 26h
```

Only render the badge for keys currently `is_proxy=1` — no badge at all
when everything is on primary sources (don't add visual noise when
healthy). If multiple keys are on proxy simultaneously, stack multiple
small badge lines rather than collapsing them into one ambiguous line.

## Verification before calling this done

1. `python -m py_compile macro_data.py xauusd_mt5_strategy.py
   telegram_alert.py generate_dashboard.py`.
2. Synthetic test against the tracker logic directly (no live MT5/network
   needed): feed it a sequence of fake fetch results —
   (a) primary → proxy → confirm a row is created with `since` = that
   moment; (b) proxy → proxy again 1 hour later → confirm `since` is
   UNCHANGED (this is the bug this design explicitly avoids — don't let
   it silently reset); (c) advance past the threshold → confirm the
   one-time-alert condition fires exactly once, not on every subsequent
   scan; (d) proxy → primary → confirm the row clears and the
   sent-alert record resets so a later relapse can alert again.
3. Regenerate the dashboard with mock data showing one key on proxy past
   threshold and confirm the badge renders; regenerate again with all keys
   on primary and confirm no badge appears at all.
4. Confirm `score_macro_bias()`'s actual scoring math is untouched — this
   feature only reads/displays the existing `"source"` tags, it must not
   change what `macro_bias` votes.
5. Do NOT restart the live bot.

## Report back

State which data keys you found with proxy fallbacks (confirm against the
current code — the audit only directly observed `comex_gold` and
`etf_gld`, but check `fetch_fed_rate_expectation()` and any other macro
input for the same pattern), what the actual `"source"` string values are
for each, and the result of the synthetic test in Step 2.
