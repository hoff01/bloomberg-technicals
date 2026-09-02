# Advanced indicator pack

This pack extends the existing causal spread engine without adding another
directional strategy or expanding the 2,979-trial family. Every feature is
calculated at a completed 15-minute close and can only affect the next-bar-open
decision. Final numerical outputs are Float32; zero denominators, constant
windows, NaNs, and infinities become null rather than false evidence.

## Production diagnostics

### Time-of-day normalized shock

For each spread and bar slot, the scale is the shifted median absolute price
change across the prior 20 sessions:

\[
s_{tod,t}=1.4826\,\operatorname{median}_{20\ prior\ same-slot}(|\Delta P|),
\qquad z_{tod,t}=\Delta P_t/s_{tod,t}.
\]

This removes the recurring open, EIA-window, and close heteroskedasticity that a
pooled rolling volatility cannot distinguish. The current bar is excluded from
the reference scale. `abs(z_tod) >= 4.0` is reported as a candidate risk gate,
but does not suppress or create BUY/SELL direction in this release.

### Volatility regime and tail recurrence

`vol_regime_ratio_1d_20d` is 26-bar price-change volatility divided by 520-bar
volatility. `tail_event_rate_20d` is the 520-bar frequency of
`abs(z_tod) > 2.5`, with unavailable warm-up observations retained as null.
Together they distinguish a single jump, persistent tail clustering, volatility
expansion, and compression.

### Robust volume surprise and price-impact stress

Volume surprise standardizes `log1p(package_volume_capacity)` against its shifted
same-slot median. Its robust scale is the shifted rolling median of prior
one-step absolute same-slot median forecast errors, multiplied by 1.4826. This
causal, vectorized estimator is deliberately described as a forecast-error
scale, not the exact MAD of one fixed historical sample. A value at or below
-2.5 is an unusually dry candidate execution-risk state. It is diagnostic in
this release; high or low volume never manufactures direction.

`liquidity_stress_ratio` divides the existing Amihud-style price-impact proxy by
its shifted same-slot median. It is diagnostic until sufficient forward evidence
supports a joint impact/quoted-width gate.

### Higher moments, volatility-of-volatility, trend quality, and choppiness

- Five-session change skew and excess kurtosis describe asymmetric and fat-tail
  behavior not captured by semivariance or bipower jump share.
- Realized volatility-of-volatility is the coefficient of variation of the
  one-session return-volatility series over five sessions. It avoids the
  near-zero price-level instability of Bollinger-width dispersion.
- `trend_hac_t_stat_3d` is a 78-bar Newey-West/Bartlett t-statistic with six
  autocovariance lags, capped at +/-12. It is diagnostic because MACD, Donchian,
  and efficiency ratio already span the directional trend thesis.
- Five-session close-path choppiness compares the sum of 129 absolute changes
  with the 130-close range and maps it to the standard 0-100 logarithmic scale.
  It adds path structure without fabricating spread highs or lows from
  asynchronous legs.

`advanced_risk_regime` summarizes the diagnostics into transparent states such
as normal, volatility expansion, tail clustering, impact stress, volume
dryness, or compound stress.
The label is explanatory and not a fitted hidden-state model. It remains
`WARMUP` until every input used by the regime classifier is available, so a
missing risk input can never be silently labelled normal.

## Multi-leg scope

`leg_return_correlation` and `lead_lag_score` use the first two legs and are now
explicitly available only for two-leg packages. Flies, condors, boxes, and other
multi-leg structures are labelled `MULTI_LEG_DIAGNOSTIC_UNAVAILABLE` rather than
presenting a two-leg calculation as whole-package relationship health.

## Promotion policy

The reproducible preregistered ablation for the two candidate entry gates
removed 15 of 4,198 OOS trades (0.36%). Aggregate trial net P&L declined by
$1,134 and 2x-cost net declined by $184. Changed fold net P&L improved in 11
folds and worsened in 19, failing the required strict majority. Maximum
drawdown improved in 9 of 11 changed folds and three economic families met the
drawdown breadth test, but those secondary results cannot override the failed
net-performance criterion. The lockbox also declined by $994 and, regardless
of sign, is structurally excluded from the promotion function. The gates
therefore remain diagnostic in this release. Run
`python scripts/run_advanced_gate_ablation.py`; the machine-readable receipt is
`dist/advanced_gate_ablation.json`.

New diagnostics must accumulate at least 60 new completed sessions before any
directional promotion is considered. Formula, window, threshold, and missing
value behavior must be preregistered. A proposed gate must improve 2x-cost
drawdown or MAE in at least 60% of folds across multiple economic families and
must not reduce 2x-cost net performance by more than 10%. The untouched
30-session lockbox cannot be used to choose the feature or threshold. A new
directional expert additionally requires incremental ablation evidence and a new
multiple-testing count.

## Deliberately excluded

ATR, ADX, stochastic, CCI, Williams %R, Supertrend, Ichimoku, candlesticks, CMF,
and MFI require genuine synchronized spread high/low/volume fields that do not
exist for asynchronous multi-leg packages. VPIN and true order-flow imbalance
require aggressor-side trade data. Dynamic hedge ratios alter the executable
economic package. HMMs, wavelets, dominant-cycle searches, and unconstrained
deep ensembles add material mining and endpoint risk. None are represented as
evidence in this release.

## Methodology sources

- [Newey and West, HAC covariance estimation](https://www.nber.org/papers/t0055)
- [Andersen and Bollerslev, intraday periodicity](https://doi.org/10.1016/S0927-5398(97)00004-2)
- [Amihud, illiquidity and expected returns](https://doi.org/10.1016/S1386-4181(01)00024-6)
- [Barndorff-Nielsen and Shephard, power and bipower variation](https://shephard.scholars.harvard.edu/sites/g/files/omnuum7741/files/power.pdf)
- [Bollerslev, realized semivariation review](https://academic.oup.com/jfec/article-abstract/20/2/219/6432504)
- [Harvey, Liu and Zhu, multiple-testing hurdles](https://doi.org/10.1093/rfs/hhv059)
