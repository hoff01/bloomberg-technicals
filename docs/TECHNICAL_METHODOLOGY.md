# Technical methodology and research decisions

## 1. Bloomberg/XBBG acquisition

The live client uses XBBG 1.4.6's asynchronous `abdib` interface with
`backend="polars"`, `typ="TRADE"`, `interval=15`, IANA timezone
`America/New_York`, and `gapFillInitialBar=False`. XBBG returns fixed intraday
bar fields: ticker/time, OHLC, volume, number of events, and value. Separate BID
and ASK requests provide historical quoted-price proxies; they are not treated
as trade volume. Daily `bdh` requests use `PX_SETTLE`, `PX_LAST`, `PX_VOLUME`,
and `FUT_AGGTE_OPEN_INT` from 2022-01-01. `PX_SETTLE` is authoritative;
`PX_LAST` is retained only as an explicitly labelled fallback.

Useful references:

- [XBBG Python documentation](https://xbbg.org/python/)
- [XBBG quick start](https://xbbg.org/python/quickstart)
- [XBBG 1.4.6 on PyPI](https://pypi.org/project/xbbg/)
- [XBBG 1.4.6 intraday implementation](https://github.com/xbbg-org/xbbg/blob/v1.4.6/py-xbbg/src/xbbg/blp.py)

Standard Desktop/B-PIPE intraday requests are commonly limited to about 140
days. The operating design is therefore an incremental local archive: re-pull a
short overlap, upsert by security/event/timestamp, retain exactly 18 calendar
months, and allow an entitled backfill import. A first complete fold requires
260 sessions: 180 training,
30 validation, five embargo, 15 OOS test, and 30 untouched lockbox sessions.
Production coverage is targeted at 300 complete sessions; live training fails
closed below the mechanical fold requirement rather than publishing a
misleading model. Public descriptions of the
retention constraint include [MathWorks' Bloomberg B-PIPE timeseries
documentation](https://www.mathworks.com/help/datafeed/bloombergbpipe.timeseries.html)
and this reproduction of Bloomberg DAPI help text on [Quantitative
Finance](https://quant.stackexchange.com/questions/74136/intraday-non-tick-historical-data-bloomberg-python-api).

## 2. Contract identity and economics

Exact delivery month and verified risk metadata are carried on every row. The
critical Bloomberg fields are `FUT_CONTRACT_DATE`, `FUT_LAST_TRADE_DT` /
`LAST_TRADEABLE_DT`, `FUT_NOTICE_FIRST`, and `FUT_DLV_DT_FIRST`. Generic roots
are display concepts; the backtest trades specific contract tickers.

The published curve stops at M16, while acquisition requests M1–M18. The two
additional contracts are an alignment buffer: the sixteenth exact delivery-month
package can require a later generic rank after product-specific rolls. Missing
far-month legs produce an `INCOMPLETE_CURVE` row rather than a synthetic package.

Prices are normalized to USD/bbl for signals:

\[
HO_{bbl}=42\,HO_{gal},\quad QS_{bbl}=QS_{MT}/7.45,\quad CL_{bbl}=CL,\quad CO_{bbl}=CO.
\]

That normalization is internal. User-facing crack levels and targets are always
USD/bbl. HO-only calendars, flies, and condors are converted back to cpg as
`normalized USD/bbl / 0.42`, equivalent to native `USD/gal x 100`. HOGO is also
shown in cpg so both distillate legs share one quote basis.

P&L is never calculated from that displayed synthetic price. It is calculated
leg by leg with contracts, sign, native multiplier, and native price. Contract
economics follow [CME HO specifications](https://www.cmegroup.com/markets/energy/refined-products/heating-oil.contractSpecs.html),
[CME's crack-spread method](https://www.cmegroup.com/articles/2024/trading-crack-spreads.html),
[ICE Brent specifications](https://www.ice.com/products/219/Brent-Crude-Futures),
[ICE Low Sulphur Gasoil specifications](https://www.ice.com/products/34361119/Low-Sulphur-Gasoil-Futures),
and the [ICE gasoil/Brent crack specification](https://www.ice.com/products/3545365/low-sulphur-gasoil-brent-futures-crack).

## 3. Synthetic spread OHLC constraint

Independent 15-minute leg extrema are asynchronous. `A_high - B_low` is only a
bound; it is not an observed spread high. The system therefore does not invent
synthetic highs/lows and does not apply ATR, ADX, stochastic, CMF, or candlestick
rules to those nonexistent prices. It uses synchronized open/close, close-based
volatility, or leg-level flow. A future enhancement can build true spread OHLC
from synchronized one-minute/tick observations or an exchange-listed spread.

## 4. Indicator stack

Production features are transparent native Polars expressions. The base runtime
is Pandas-free and retains lightweight
[`polars-talis`](https://pypi.org/project/polars-talis/) as an independent
per-structure cross-check. The Pandas/Numba-dependent
[`polars-ta`](https://pypi.org/project/polars-ta/) comparison is optional and is
not installed in the operating environment. The similarly named
[`noahbclarkson/polars-ta`](https://github.com/noahbclarkson/polars-ta) is a Rust
crate with different EMA conventions, not the Python production engine.

Feature families include:

- Trend: EMA 26/78, MACD 26/78/26, shifted Donchian 78, efficiency ratio.
- Mean reversion: rolling median/MAD z, Bollinger percentage/width, RSI 26,
  rolling AR(1) and OU half-life diagnostic.
- Volatility: five-session realized volatility, EWMA absolute change,
  upside/downside semivolatility, jump score, volatility of volatility, and
  prior-only squeeze percentile.
- Stability and information: variance-ratio curve, Hurst proxy, zero-crossing
  rate, half-life stability, permutation entropy, and a CUSUM change-point alarm.
- Downside/jump risk: realized semivariance asymmetry, jump share, and
  volatility-of-volatility.
- Structure/seasonality: exact curve month, roll ID, days to earliest risk,
  calendar month, EIA window, and prior-year-only 14-day expected standardized
  package moves with empirical-Bayes shrinkage.
- Volume/liquidity: same-bar-of-day relative package volume, PVO, signed-volume
  OBV proxy, event counts, average trade size, lagged OI, quoted width, and
  package capacity. Additional diagnostics include Amihud impact, effort versus
  result, OI migration, leg-return correlation, and lead/lag score.
- Advanced regime and path risk: same-time-of-day normalized change,
  one-session versus 20-session volatility regime ratio, liquidity-stress
  ratio, 20-session tail-event rate, robust same-time-of-day volume surprise,
  five-session return skew and excess kurtosis, realized-volatility-of-volatility,
  a three-session heteroskedasticity/autocorrelation-consistent trend t-statistic,
  and five-session close-path choppiness. These features are aggregated into a
  deterministic `advanced_risk_regime`; `relationship_health_scope` states the
  observable multi-leg evidence scope rather than asserting structural causality.

The signed-volume fields are explicitly proxies. Fifteen-minute BDIB bars do not
contain aggressor-side trades or historical order-book events, so the system
does not label them order-flow imbalance, VPIN, or true market depth.

Intraday windows are expressed in trading bars: 13 = half a session, 26 = one,
78 = three, 130 = five, 260 = ten, and 520 = twenty sessions.

The advanced fields are production diagnostics and preregistered candidate
entry controls, not new directional experts. The candidate controls are
disabled in the default configuration because their ablation failed the
promotion standard. `tod_normalized_change` compares the current move with the
prior distribution for the same bar slot so the open, inventory windows, and
afternoon are not pooled indiscriminately. `robust_volume_surprise` uses a
shifted same-slot median and a robust scale built from prior one-step absolute
median forecast errors. This remains fully vectorized; it is not presented as
the exact MAD of one fixed historical sample. Distribution and path
statistics use trailing, backward-looking windows and require their configured
minimum support; unavailable observations remain null rather than being filled
with a benign value.

## 5. Volume and depth interpretation

A synthetic spread has no genuine volume. If a package requires four QS and
three Brent contracts, its bar capacity is:

\[
\min(V_{QS}/4,V_{CO}/3).
\]

This quantity drives relative-volume and participation limits. Each root's raw
volume remains separately available in the feature Parquet. Daily open interest
is shifted one full session to avoid publication-time look-ahead.

Two nondirectional candidate gates are tracked for unstable execution
conditions: an absolute same-time-of-day normalized move at or above
`indicators.extreme_tod_shock_z`, and a robust volume surprise at or below
`indicators.volume_dryness_z`. Their preregistered ablation did not meet the
promotion standard, so this release reports them but does not block entries or
add a strategy, expert, or multiple-testing trial.

`adepth` is attempted only for a current snapshot and requires B-PIPE plus the
applicable entitlement. Terminal top-of-book is the next tier. If neither is
available, bar proxies are used and confidence is capped. No report describes a
top-of-book snapshot as historical Level 2.

## 6. Expiry state machine

For each leg, risk date is the earliest available last-trade, first-notice, or
first-delivery date. For a mixed-exchange package, the earliest leg controls.

- D-4 (last common session before blackout): no new entries; liquidate at the
  final eligible 14:15 bar open.
- D-3 through expiry/roll: position must remain zero.
- Missing/unchecked expiry: entry fails closed.
- Roll change: the old package is closed and the new package is independent.

The research-only back-adjusted series may support momentum calculations. It is
anchored to the latest executable contract scale so displayed fair values and
targets remain comparable with the current raw quote. Raw contract P&L never
crosses the adjustment.

## 7. Backtest execution and validation

Every strategy observes bar `t` only after that close and may fill at bar
`t+1` open. A pending entry is cancelled if the exact contract package rolls,
expiry blocks, or data is missing. One-way cost equals per-leg commission plus
adverse tick slippage; 2x and 3x cost stress are reported. Backtests use one
economic package, while the live board reports a 1% leg-volume participation
capacity.

Chronological validation uses an expanding 180-session training window, 30
validation sessions, five explicit embargo sessions, and 15 actual OOS test
sessions per fold. The final 30 sessions are an immutable evaluation lockbox
outside every development fold. The fixed preregistered rules do not tune on
those windows, but every trade is assigned to the correct phase/fold for audit.
Metrics use daily aggregated P&L. A deflated-Sharpe-style trial-count penalty,
fold consistency, cost stress, minimum OOS trades, profit factor, and drawdown
classify each spread/strategy as `VALIDATED` or `RESEARCH_ONLY`. Lockbox trade
count and P&L are reported only and cannot change that classification or the
selected strategy.

The structure registry deliberately separates breadth from evidence. Calendar
flies, equal-wing condors, crack-curve combinations, and relative-value boxes
receive complexity tiers that raise their required OOS trade count. Every
model-enabled spread/strategy pair counts as a trial even when it produces zero
trades. Algebraically dependent identities and the most complex composite
diagnostics are shown for consistency monitoring but are excluded from model
selection and cannot emit BUY/SELL signals.

Nine strategies are reported. Seven interpretable rule experts feed both a
fixed regime ensemble and a causal Fixed-Share ensemble. Adaptive weights are
pooled by economic family and tenor bucket, update once per resolved session
after a 26-bar (one-session) cost-aware label is observable, shrink toward equal
weight, and are capped so no expert dominates. There is no update before 60 observations;
confidence remains capped until 120; and the final lockbox never changes the
weights. This is online expert weighting, not reinforcement learning or an
unconstrained self-modifying model.

The current `pattern_state` makes that combination observable. Consensus states
require the mature frozen adaptive vote; cluster, fragment, and mixed states
disclose partial or conflicting expert evidence; structural-break, warm-up,
stale, and analytic-only states fail closed. `pattern_strength` is the absolute
frozen adaptive score, `pattern_agreement` is the same-side expert share, and
`pattern_components` reports the long/short counts and top adaptive expert.
These are reporting fields only and cannot change direction, confidence,
status, or the multiple-testing ledger.

Training and scoring are separate operating states. Training runs the complete
trial ledger, freezes one evidence-ranked preregistered strategy per structure,
stores the last pre-lockbox expert weights, and records an engine/configuration
hash. Score-only uses the most recent 80 sessions for indicator warm-up, loads
the frozen prior-year seasonal profile, and never updates weights or reruns a
backtest. When source hashes are identical, the current signal snapshot must
match training exactly or scoring fails closed.

Every current structure is paired with its frozen selected-strategy evidence
and the final 30-session trade ledger. The user-facing brief keeps the live
spread level, buy/sell entry conditions, fair-value target, risk stops, OOS
metrics, lockbox metrics, dynamic pattern synthesis, and the current advanced
regime/risk diagnostics in separate fields. Model age is counted in
completed source sessions; after five new sessions without retraining, the
board changes model-enabled rows to `MODEL STALE` and blocks directional
promotion.

For context on the adaptive and validation design, see the
[Fixed-Share analysis](https://jmlr.org/papers/volume17/13-533/13-533.pdf),
[Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf),
and [Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

## 8. Structure generator

The registry contains 337 auditable structures, of which 331 are model-enabled.
The systematic basis spans 60 adjacent calendars, 56 consecutive flies, 52
equal-wing condors, 14 HOGO flies, M1–M16 HO/WTI and gasoil/Brent cracks,
HO/QS and Brent/WTI relative value, crack-calendar boxes, single-curve boxes,
and transatlantic crack
boxes. The hand-curated nearby packages remain in the same registry. Every
structure has an algebra group, normalized display formula, executable contract
ratios, complexity tier, and model-eligibility flag.

Exact delivery matching is mandatory. HO/WTI packages use 1:1 contracts and the
gasoil/Brent crack uses the exchange-standard four-gasoil/three-Brent ratio.
Prices are normalized for comparison, while P&L and costs always remain legwise.
Gasoil is displayed in USD/bbl as `USD/MT / 7.45` and in cpg as
`USD/MT / 7.45 / 0.42`. HOGO outrights, adjacent boxes, and flies use the same
cpg conversion so the HO and gasoil sides are dimensionally comparable.

## 9. Seasonality discipline

With daily history beginning in 2022, only a small number of independent years
are available. Seasonality is therefore a contextual prior, not a standalone
optimized strategy. For each as-of year, only earlier years contribute. The
engine estimates the expected one-session package move around the same calendar
and expiry-relative windows, shrinks it toward zero when support is thin, and
reports prior-year count and settlement quality. This prevents the current year
and future observations from leaking into the feature.

## 10. Optional Kronos diagnostic

The optional `NeoQuasar/Kronos-base` path is isolated in a second user-local
Windows environment. The model and tokenizer are pinned to exact Hugging Face
revisions and cached outside the repository. The adapter bypasses the upstream
Pandas convenience wrapper and calls tensor inference directly through
Polars → NumPy → Torch.

Kronos receives only real outright-contract OHLCV bars. Each unique current
contract is forecast once, after which predicted native close moves are
recombined using the registered leg sign, price weight, and unit conversion.
This preserves the synthetic-spread OHLC constraint. Its output is diagnostic,
never action-eligible, until at least 30 strictly forward sessions have been
accumulated and an explicit promotion test is implemented.

## 11. Backfill schema

The simplest canonical Parquet/CSV contains the columns in
`app.technical_data.BAR_COLUMNS`:

`timestamp_utc`, `bar_start_et`, `bar_end_et`, `session_date`, `bar_slot`,
`event_type`, `root`, `security`, `delivery_month`, `open`, `high`, `low`,
`close`, `volume_contracts`, `num_events`, `value`, `source`, and
`pulled_at_utc`.

An XBBG-like file with `ticker/security`, `time`, OHLC, volume, `numEvents`, and
value is also accepted if its security tickers match the configured contract
universe. Missing bars are not forward-filled.
