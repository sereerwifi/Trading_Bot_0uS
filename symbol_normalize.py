"""Canonical instrument identity for gold, used everywhere the bot needs to
recognize that "XAUUSD", "GOLD", and broker-specific spellings like
"XAUUSDm" or "GOLD#" all refer to the SAME instrument — regardless of
whether that string came from the MT5 broker symbol, Myfxbook, a COT/COMEX
label, a manually typed config value, or (in the future) a news feed.

This does NOT change what gets sent to MT5 for order placement — the EA's
configured SYMBOL must still be the exact string the broker registered
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
    "GOLD", "XAUUSD", "XAUUSDM", "XAUUSDC",
    "GOLD#", "GOLDM", "GOLDSPOT", "XAU/USD", "XAU-USD", "XAU_USD", "XAU",
    # Note: "XAUUSD." and "XAUUSD#" are intentionally excluded — _clean() strips
    # '.' and '#' before the set lookup, so they'd never match as written; they
    # are already covered by the startswith("XAUUSD") fallback in is_gold().
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
    """True if raw_symbol (any broker/source spelling) refers to gold."""
    cleaned = _clean(raw_symbol)
    if cleaned in _GOLD_ALIASES:
        return True
    return cleaned.startswith(("GOLD", "XAUUSD", "XAU"))


def canonical_display(raw_symbol):
    """What to show a human, or pass to a source that expects the
    'XAUUSD' spelling (Myfxbook, most news/sentiment sites). Returns the
    input unchanged if it isn't recognized as gold."""
    return CANONICAL_DISPLAY if is_gold(raw_symbol) else raw_symbol


def canonical_commodity(raw_symbol):
    """What to pass to COT/COMEX-style feeds that expect 'GOLD'/'SILVER'
    naming. Returns 'SILVER' for silver symbols, else the input unchanged."""
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
    the broker symbol is 'GOLD', or 'XAUUSDm (XAUUSD)' for a suffixed
    variant. Falls back to the raw symbol if it isn't gold at all."""
    if not is_gold(raw_symbol):
        return str(raw_symbol)
    disp = canonical_display(raw_symbol)
    if _clean(raw_symbol) == _clean(disp):
        return disp
    return f"{raw_symbol} ({disp})"
