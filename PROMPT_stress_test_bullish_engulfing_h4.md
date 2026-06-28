# Prompt for Claude Code — stress-test the bullish_engulfing/H4 finding before wiring it in

Don't implement a new strategy yet. The previous run of
`analyze_candlestick_patterns.py --live` reported `bullish_engulfing` on
H4 at 55.2% win rate (n=245) as the strongest finding, with
`three_white_soldiers` at 51.5% (n=618) called "marginal," and a note
that "gold's long-term bullish bias in this period" may explain why all
bearish patterns scored near-zero. That last observation is exactly the
confound that needs ruling out before trusting the 55.2% number: if gold
simply trended up across whatever window was tested, ANY bullish-leaning
pattern — real or fake — would show a win rate above 50%, including one
with zero actual predictive content. The report as given doesn't
distinguish "this pattern has edge" from "this dataset went up and the
pattern happened to be bullish."

## Ground rule

Read-only analysis, same as before. No changes to `strategies.py`,
`xauusd_mt5_strategy.py`, config, or the live bot in this pass. Output is
a report back to the user — implementation is a separate, later, explicit
step.

## Add to `analyze_candlestick_patterns.py` (or a new sibling script,
whichever is cleaner against the current code) and run these four checks
against the SAME H4 dataset already pulled:

**1. Baseline drift control.** Compute the win rate of simply going long
on EVERY bar (not just pattern bars) over the same forward horizons
(4, 8, 24 bars) and the same 0.25-ATR win threshold used elsewhere in
this script. If "always long" already wins ~53-55% of the time on this
window, `bullish_engulfing`'s 55.2% is not a usable edge over the naive
baseline — it's just the dataset's drift. Report `bullish_engulfing`'s
win rate MINUS the always-long baseline win rate as the real number that
matters, not the raw 55.2%.

**2. Per-horizon breakdown.** Report `bullish_engulfing`'s win rate
separately for 4-bar, 8-bar, and 24-bar forward windows (the analyzer's
`evaluate_pattern()` already computes this per-horizon internally — the
prior run only surfaced the averaged number). If the edge only shows up
at one horizon and is flat/negative at the others, that's a weaker,
more fragile finding than a edge that holds across all three.

**3. Split-sample stability.** Sort the 245 `bullish_engulfing`
occurrences chronologically, split into first half vs second half, and
report the win rate for each half separately. If one half carries
the entire edge and the other half is ~50% or worse, the "55.2% overall"
figure is hiding a regime-dependent result, not a stable pattern — flag
this explicitly either way.

**4. State the actual date range** the 4000 (or however many) H4 bars
covered (print `df.index.min()`/`df.index.max()` or the bar timestamps
if available from `mt5.copy_rates_from_pos` — MT5 returns a `time` epoch
column dropped by the current `fetch_live()`; re-add it for this check
only, read-only, no need to change the production fetch function).
Roughly how many months/years of data is 245 occurrences spread across?
A genuinely robust finding should span multiple distinct market regimes,
not one continuous trending leg.

## Report back

State plainly: after subtracting the always-long baseline, after
checking per-horizon consistency, and after the split-sample check, is
`bullish_engulfing` on H4 still a real, stable, horizon-consistent edge —
or does it collapse into "gold went up during the tested window"? Answer
this as a yes/no with the actual numbers, not a qualitative impression.
Do the same lighter check for `three_white_soldiers` only if
`bullish_engulfing` survives (no need to stress-test a pattern that's
already weaker on its face). Don't propose implementing anything in this
pass — that's the next, separate, explicit step only if something here
actually survives.
