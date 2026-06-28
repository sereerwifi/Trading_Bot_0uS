# Prompt for Claude Code — run the candlestick pattern frequency/edge analysis for real

Run this in the live bot folder on the VPS. This is a read-only research
task, not a code change to the bot — it produces a report, nothing more,
unless Step 3 (optional) is explicitly requested afterward.

## Why this exists

The user asked for the most frequently recurring XAUUSD candlestick
patterns to use as bot entry points. `analyze_candlestick_patterns.py`
(already in this folder) implements 18 classic pattern detectors
(engulfing, hammer/shooting star, morning/evening star, three white
soldiers/black crows, piercing line/dark cloud cover, inside/outside bar,
tweezer top/bottom, marubozu) plus a forward win-rate test so frequency
isn't reported alone — a pattern that fires constantly but resolves at a
coin-flip rate afterward is not a usable signal.

**This script's detector logic was verified with synthetic hand-built
candle sequences (`--selftest`) and a random-walk smoke test in an
environment with no live MT5 connection and no internet access to price
data — it could NOT be run against real XAUUSD history there.** The
frequency/win-rate numbers only become real once you run `--live` here,
where MT5 has an actual connected account with real price history.

## Step 1 — sanity check first

```bash
python analyze_candlestick_patterns.py --selftest
```

Must print `ALL PASS`. If anything fails, the detector logic itself broke
(possibly a pandas version difference) — fix the failing detector
function before trusting any `--live` output, don't proceed past a
failure.

## Step 2 — run against real history

```bash
python analyze_candlestick_patterns.py --live --symbol XAUUSD --tf H1 --bars 8000
python analyze_candlestick_patterns.py --live --symbol XAUUSD --tf H4 --bars 4000
```

(Adjust `--symbol` to whatever `symbol_normalize.resolve()` /
`SYMBOL` resolves to in this account's Market Watch if `XAUUSD` isn't
recognized — same naming issue `symbol_normalize.py` already exists to
handle. `--bars 8000` H1 is roughly a year of data depending on broker
session hours; use whatever depth the terminal actually has downloaded —
the script will error clearly if there isn't enough history rather than
silently returning a short/misleading sample.)

This prints a ranked table (by `edge_score = max(win_rate - 0.5, 0) *
count`) and saves `candlestick_pattern_report_H1.csv` /
`candlestick_pattern_report_H4.csv` in this folder.

## Step 3 — report back, don't auto-implement

Paste the full ranked table for both timeframes back to the user. Call
out:
- Which patterns rank highest by `edge_score` (frequency AND real
  directional edge, not just raw count).
- Note `ambiguous_pattern=True` rows (doji, inside_bar) honestly — these
  don't have an inherent direction, the script tested both ways and kept
  the better-performing side, which is a weaker signal than a pattern
  with an inherent direction baked into its shape.
- Whether ANY pattern's `avg_win_rate` meaningfully clears 0.5 at
  reasonable sample size (`count` — treat anything under ~30-50
  occurrences as too thin to trust even with a good win rate). If nothing
  clears a real edge, say so plainly — don't round up a 0.51 win rate on
  40 samples into "this works."

**Do not wire any pattern into `strategies.py` / `xauusd_mt5_strategy.py`
as a new live strategy in this same session.** That's a separate,
deliberate step the user should request after seeing real numbers, not
something to bundle in automatically just because a pattern looked decent
in this report — match the project's existing pattern of "research →
show findings → user decides → then a separate additive implementation
pass," same as the climax_reversal_sr and myfxbook strategies were added
historically (see `CLAUDE.md`'s strategy history) and same as the
2026-06-25 audit findings were handled (one Claude Code prompt per
discrete change, never bundled).
