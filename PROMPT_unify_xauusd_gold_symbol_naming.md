# Prompt for Claude Code — unify "XAUUSD" and "GOLD" as one instrument

Paste this into Claude Code in the bot project folder (works on either copy —
the local Mac folder `RoBotTrading man 0 US` or the VPS copy; if you run it on
one, flag that the other still needs the same patch per the existing
CLAUDE.md sync rule).

## Why

The bot already treats gold under two different spellings depending on the
data source, with the mapping done ad hoc in several places instead of one
shared place:

- **Broker/MT5 symbol** — `SYMBOL = "GOLD"` in `xauusd_mt5_strategy.py`
  (because the XM broker's Market Watch lists it as `"GOLD"`, not
  `"XAUUSD"`). Other brokers use `"XAUUSDm"`, `"GOLD#"`, `"XAU/USD"`, etc.
- **Myfxbook sentiment** — always queried as `"XAUUSD"`
  (`fetch_myfxbook_sentiment(symbol="XAUUSD", ...)` in `macro_data.py`).
- **COT report / COMEX inventory** — keyed by `"GOLD"`
  (`fetch_cot_report(commodity="GOLD")`, `fetch_comex_inventory(metal="GOLD")`).
- **UI default config** — `strategy_config_ui.py` line ~101 defaults
  `"symbol": "XAUUSD"`, while the EA's own hardcoded default is `"GOLD"` —
  two different defaults for the same instrument in two different files.
- **Dashboard / Telegram labels** — just print whatever raw string `SYMBOL`
  happens to be, so the dashboard and Telegram messages can show "GOLD" even
  when the user is cross-referencing news/sentiment sites that always say
  "XAUUSD" (or vice versa) — easy to misread as two different things.

There's already one manual patch for this
(`xauusd_mt5_strategy.py` line ~1459: `metal = "GOLD" if
SYMBOL.upper().startswith(("GOLD", "XAU")) else "SILVER"`), but it's
duplicated logic, not a single source of truth, and doesn't help the
dashboard/Telegram/UI side at all.

## Goal

One small shared module that knows every spelling of gold the bot might
encounter (from broker symbol, Myfxbook, COT/COMEX naming, or a manually
typed custom symbol like `"XAUUSDm"`) and normalizes them all to the same
canonical identity — so matching logic, scoring, and anything that reads
news/sentiment by name treats them as the same instrument, and so the
dashboard/Telegram always display gold the same way regardless of which
spelling the broker happens to use.

This must be purely additive — it does not change the EA's actual MT5 order
symbol (that still has to be the exact string the broker uses, unchanged),
it only adds a normalization/display layer on top.

## Step 1 — create `symbol_normalize.py`

New file, no dependencies beyond stdlib (so it's usable from every other
file, including the tkinter UI and the MT5 EA, without adding import
weight):

```python
"""Canonical instrument identity for gold, used everywhere the bot needs to
recognize that "XAUUSD", "GOLD", and broker-specific spellings like
"XAUUSDm" or "GOLD#" all refer to the SAME instrument — regardless of
whether that string came from the MT5 broker symbol, Myfxbook, a COT/COMEX
label, a manually typed config value, or (in the future) a news feed.

This does NOT change what gets sent to MT5 for order placement — the EA's
configured `SYMBOL` must still be the exact string the broker registered
(see xauusd_mt5_strategy.py's SYMBOL comment). This module only answers
"is this string referring to gold?" and "what should I show a human / pass
to a non-broker API (Myfxbook, news matching, etc.) for this instrument?".
"""
import re

# Every known way gold shows up across brokers, data providers, and free
# text. Add new aliases here as you discover them -- this is the ONLY
# place that should ever need editing when a new broker/source spelling
# turns up.
_GOLD_ALIASES = {
    "GOLD", "XAUUSD", "XAUUSDM", "XAUUSDC", "XAUUSD.", "XAUUSD#",
    "GOLD#", "GOLDM", "GOLDSPOT", "XAU/USD", "XAU-USD", "XAU_USD", "XAU",
}

# The name each external, non-broker source expects when you query it for
# gold -- centralizing these means macro_data.py / strategies.py never
# need to hardcode "XAUUSD" or "GOLD" themselves again.
CANONICAL_DISPLAY = "XAUUSD"     # the name news/sentiment sites use
CANONICAL_COMMODITY = "GOLD"     # the name COT/COMEX/CME feeds use
MYFXBOOK_SYMBOL = "XAUUSD"       # Myfxbook's community-outlook table key
ETF_PROXY_TICKER = "GLD"         # SPDR Gold Shares, used for ETF-flow proxy


def _clean(raw):
    """Upper-case and strip everything except letters/digits/slash, so
    "Gold#", "xauusd ", "XAU/USD" etc. all collapse to a comparable form."""
    if not raw:
        return ""
    return re.sub(r"[^A-Z0-9/]", "", str(raw).upper())


def is_gold(raw_symbol):
    """True if `raw_symbol` (any broker/source spelling) refers to gold."""
    cleaned = _clean(raw_symbol)
    if cleaned in _GOLD_ALIASES:
        return True
    # catch broker suffix/prefix variants not explicitly listed, e.g.
    # "XAUUSD.a", "#GOLD.cash", "GOLDcfd" -- anything that's clearly built
    # around the GOLD or XAUUSD root.
    return cleaned.startswith(("GOLD", "XAUUSD", "XAU"))


def canonical_display(raw_symbol):
    """What to show a human, or pass to a source that expects the
    'XAUUSD' spelling (Myfxbook, most news/sentiment sites). Returns the
    input unchanged if it isn't recognized as gold (so this is safe to
    call on a silver/other-instrument symbol too)."""
    return CANONICAL_DISPLAY if is_gold(raw_symbol) else raw_symbol


def canonical_commodity(raw_symbol):
    """What to pass to COT/COMEX-style feeds that expect 'GOLD'/'SILVER'
    naming. Returns 'SILVER' for the bot's silver symbols, else the input
    unchanged for anything else."""
    if is_gold(raw_symbol):
        return CANONICAL_COMMODITY
    cleaned = _clean(raw_symbol)
    if cleaned.startswith(("SILVER", "XAGUSD", "XAG")):
        return "SILVER"
    return raw_symbol


def display_label(raw_symbol):
    """Unified label for dashboard/Telegram/UI: shows BOTH spellings
    together so it's unambiguous to a human cross-referencing news or a
    sentiment site that uses the other spelling, e.g. 'GOLD (XAUUSD)' if
    the broker symbol is 'GOLD', or 'XAUUSDm (XAUUSD)' if it's a
    broker-suffixed variant. Falls back to the raw symbol if it isn't
    gold at all."""
    if not is_gold(raw_symbol):
        return str(raw_symbol)
    disp = canonical_display(raw_symbol)
    if _clean(raw_symbol) == _clean(disp):
        return disp
    return f"{raw_symbol} ({disp})"
```

## Step 2 — `xauusd_mt5_strategy.py`

Find this block (around line 1459):

```python
        metal = "GOLD" if SYMBOL.upper().startswith(("GOLD", "XAU")) else "SILVER"
        mfb_symbol = "XAUUSD" if metal == "GOLD" else "XAGUSD"
```

Replace with:

```python
        metal = symbol_normalize.canonical_commodity(SYMBOL)
        mfb_symbol = symbol_normalize.canonical_display(SYMBOL) if metal == "GOLD" else "XAGUSD"
```

Add the import near the top with the other local module imports (next to
`import strategies` / `import macro_data`, whatever the existing pattern is):

```python
import symbol_normalize
```

This removes the only duplicated alias-guessing logic in the EA and routes
it through the shared module instead — same behavior for the existing
`"GOLD"` / `"XAUUSD"` cases, but now also correctly handles suffixed
variants like `"XAUUSDm"` or `"GOLD#"` without another code change.

## Step 3 — `strategy_config_ui.py`

Near the symbol field (the dropdown around line ~1121:
`["GOLD", "XAUUSD"], row=0, editable=True`), add a live label next to it
that shows the unified name as the user types, so the UI itself confirms
the two spellings are being treated as one instrument. Import the module
at the top:

```python
import symbol_normalize
```

Then wherever the symbol entry widget is created, add a small read-only
label bound to it (exact widget wiring depends on the existing helper used
for that row — follow whatever pattern the dropdown/editable-combo helper
already uses for adjacent fields, and update the label's text via the same
callback that already fires on edit/keystroke, calling
`symbol_normalize.display_label(current_value)`). If there's no existing
on-change hook for that field, add one — this should update live, not just
on save, so the user sees immediately that `"GOLD"` and `"XAUUSDm"` and
`"XAUUSD"` all resolve to the same `"GOLD (XAUUSD)"`-style confirmation.

## Step 4 — `macro_data.py`

No structural change needed — `get_macro_snapshot()` already accepts
`symbol_metal` / `myfxbook_symbol` as separate params precisely so the
caller controls the naming. The fix is just to make sure the EA (Step 2)
is the only place deriving those two values, via `symbol_normalize`,
instead of any other file independently guessing. Add one line to the
module's docstring/header noting this:

```python
# NOTE: symbol_metal / myfxbook_symbol should always be derived from the
# broker's configured SYMBOL via symbol_normalize.canonical_commodity() /
# canonical_display() (see xauusd_mt5_strategy.py) -- never hardcode
# "GOLD" or "XAUUSD" again in a new caller. This keeps every alias
# ("GOLD", "XAUUSD", "XAUUSDm", "GOLD#", ...) resolving to the same
# instrument everywhere in the bot.
```

## Step 5 — `generate_dashboard.py`

Find:

```python
    <h1>XAUUSD MT5 EA — Monitoring Dashboard <span class="symbol-tag">{esc(snap['symbol'])}</span></h1>
```

Change to:

```python
    <h1>XAUUSD MT5 EA — Monitoring Dashboard <span class="symbol-tag">{esc(symbol_normalize.display_label(snap['symbol']))}</span></h1>
```

Add the import at the top with the other local imports:

```python
import symbol_normalize
```

Now the dashboard header always shows something like `GOLD (XAUUSD)`
regardless of which spelling the broker uses, so it's unambiguous against
any news/sentiment source the user is cross-checking.

## Step 6 — `telegram_alert.py`

The six hardcoded `"XAUUSD EA"` headers (order-open, order-close, started,
signal-found, macro-update, daily-status — all currently hardcoded literal
`"XAUUSD"` regardless of what the broker symbol actually is) should use the
unified label instead, so a Telegram message matches whatever the dashboard
just showed. Each function already receives or has access to the live
config/symbol (check how each is called from `xauusd_mt5_strategy.py` — if
the symbol isn't already passed in, add a `symbol=SYMBOL` argument at each
call site rather than re-reading config inside `telegram_alert.py`). Then
change e.g.:

```python
        f"<b>XAUUSD EA — New {direction} order</b>",
```

to:

```python
        f"<b>{symbol_normalize.display_label(symbol)} EA — New {direction} order</b>",
```

...and the same pattern for the other five headers. Add
`import symbol_normalize` at the top of `telegram_alert.py`.

## Verification before calling this done

1. `python -m py_compile symbol_normalize.py xauusd_mt5_strategy.py
   strategy_config_ui.py generate_dashboard.py telegram_alert.py macro_data.py`.
2. Quick standalone logic check (no MT5/tkinter needed — `symbol_normalize.py`
   has zero dependencies):
   ```python
   import symbol_normalize as sn
   assert sn.is_gold("GOLD") and sn.is_gold("XAUUSD") and sn.is_gold("XAUUSDm") and sn.is_gold("GOLD#")
   assert not sn.is_gold("EURUSD")
   assert sn.canonical_display("GOLD") == "XAUUSD"
   assert sn.canonical_commodity("XAUUSDm") == "GOLD"
   assert sn.display_label("GOLD") == "GOLD (XAUUSD)"
   assert sn.display_label("XAUUSD") == "XAUUSD"
   print("symbol_normalize OK")
   ```
3. Confirm `xauusd_mt5_strategy.py`'s `metal` / `mfb_symbol` derivation at
   the old line ~1459 still produces `"GOLD"` / `"XAUUSD"` for the current
   default `SYMBOL = "GOLD"` — i.e. no behavior change for the existing
   default, only an added safety net for other broker spellings.
4. Visually confirm (mock data is fine) the dashboard header and one
   Telegram message render the new `"GOLD (XAUUSD)"`-style label correctly.
5. Do NOT change the actual `SYMBOL` value sent to MT5 for order placement —
   this task is display/matching-layer only, never the broker order symbol
   itself.
6. Do NOT restart the live trading bot without the user's go-ahead if it's
   currently running.
