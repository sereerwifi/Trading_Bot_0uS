# Prompt for Claude Code — proper validation before implementing bullish_engulfing/H4

Still read-only analysis. Do not touch `strategies.py`,
`xauusd_mt5_strategy.py`, or config in this pass.

The prior stress-test (always-long baseline, per-horizon breakdown,
split-sample) found bullish_engulfing/H4 with a net edge of +3.3%
average (peaking at +5.8% at 24-bar), called the split-sample result
"stable" because the gap between halves (57.3% vs 53.1%) was under an
arbitrary 5% threshold, and recommended implementation. Three problems
with that conclusion, each needs to be closed before this is implemented
for real money:

## Problem 1 — "stable" wasn't a real statistical test

n≈122 per half. Standard error of a proportion at that n is
`sqrt(0.5*0.5/122) ≈ 4.5%`. A 4.2% gap between halves is within ~1 SE —
not distinguishable from noise, and 53.1% on n=123 isn't distinguishable
from 50% either. Compute and report an actual 95% confidence interval
(normal approximation is fine: `p ± 1.96*sqrt(p(1-p)/n)`) for:
- the overall 55.2% (n=245) figure,
- each of the two split-sample halves,
- the per-horizon net-edge figures (4/8/24 bar).

State plainly whether the 95% CI for the overall win rate excludes 50%.
If it doesn't, this is not yet distinguishable from a coin flip at
conventional confidence, regardless of the point estimate.

## Problem 2 — the baseline is a strawman

"Always long every single bar" is too weak a control — of course a
bullish-leaning pattern beats unconditional long-every-bar during a
2.6-year uptrend. The question that actually matters: does
bullish_engulfing add anything OVER simple trend-following?

Add a second baseline: **long only when `close > SMA(50)` on H4** (a
plain trend filter, no pattern involved), measured with the identical
forward-window/ATR-win-threshold methodology already used for the
pattern and the always-long baseline. Compute this baseline's win rate
at the same 4/8/24-bar horizons, then compute
`bullish_engulfing_win_rate - trend_filter_win_rate` (in addition to the
already-computed `- always_long_win_rate`).

If bullish_engulfing's edge over the TREND-FILTER baseline collapses to
near zero, the honest conclusion is: this isn't a candlestick edge, it's
gold being in an uptrend, and a plain trend filter captures the same
thing more simply with no pattern-matching needed at all. Report this
number explicitly — don't bury it under the always-long comparison from
the previous pass.

## Problem 3 — sample independence

Candlestick patterns cluster in trends, and forward-looking windows can
overlap. Check:
- The distribution of GAPS (in bars) between consecutive
  `bullish_engulfing` signals. If many signals are within 24 bars of
  each other (the longest forward horizon tested), their evaluation
  windows overlap in calendar time — they are not 245 independent
  trials, they're a smaller number of independent trend episodes each
  re-counted multiple times.
- Re-run the edge calculation after de-duplicating: keep only the FIRST
  signal in any run of signals that are within 24 bars of each other,
  and report the resulting `n` and win rate. This is the more honest
  "effective sample size" figure — report it alongside the raw 245.

## Report back

Give the 95% CI on the headline number, the win rate net of the
trend-filter baseline (not just always-long), and the de-duplicated
effective-n win rate. Then answer plainly: after all three corrections,
does bullish_engulfing/H4 still show a real edge distinguishable from
(a) noise and (b) plain trend-following? If yes, with what actual
numbers — and only then is implementation worth discussing. If the edge
mostly disappears under any of these three checks, say so directly; a
clear negative result here is more useful than a hopeful one.
