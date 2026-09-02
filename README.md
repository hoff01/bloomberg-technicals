# Bloomberg Distillate Technical Trading System

This package builds a 15-minute, executable-contract technical and backtesting
workflow for **HO, CL, CO, and QS only**. It resolves delivery months before it
constructs cracks, applies volume/depth and expiry gates, runs nine separately
reported strategies, and publishes a self-contained decision dashboard. The
fourth-generation engine covers M1–M16, uses daily settles from 2022 for
point-in-time seasonality, and causally reweights a fixed set of transparent
experts without changing their rules. Training and current-price scoring are
separate: strategy status, ranking, and expert weights use development/OOS data
only; the final 30 completed sessions are evaluation-only. Routine scoring never
retrains the artifact.

## First use on the Bloomberg workstation

1. Keep Bloomberg Terminal open and logged in.
2. Run **`SETUP_AND_CHECK_BLOOMBERG.bat`** once and require BDP, BDH, and BDIB
   to pass.
3. Seed at least 300 complete sessions with a licensed 18-month backfill, then
   run **`TRAIN_AND_SCORE.bat`** after 14:45 New York time. During the session,
   use **`SCORE_CURRENT.bat`**; training intentionally rejects an open final day
   or a root-specific holiday without one common NYMEX/ICE core session.
4. For routine use, run **`UPDATE_TECHNICALS_AND_OPEN.bat`** or
   **`SCORE_CURRENT.bat`**; these reuse the frozen model.

The installer creates a user-local Python 3.12/3.13 environment under
`%USERPROFILE%\Pyenvs\bbg_technical_builder`, installs the pinned XBBG/Polars
stack, and installs BLPAPI from Bloomberg's official package index only when a
compatible local copy is absent. Live launchers update the atomic Parquet store,
validate data coverage, and preserve the prior frozen model if any training,
workbook, or release gate fails.

Dedicated launchers are also available:

- **`INSTALL_BLOOMBERG.bat`** — install or repair the dedicated technical environment.
- **`SETUP_AND_CHECK_BLOOMBERG.bat`** — install and immediately run the live API proof.
- **`CHECK_BLOOMBERG_READY.bat`** — exercise live BDP, BDH, BDIB, and subscription access before training.
- **`TRAIN_AND_SCORE.bat`** — explicitly rebuild backtests and freeze a new model.
- **`SCORE_CURRENT.bat`** — update prices and score with the last frozen model.
- **`RUN_TECHNICAL_DEMO.bat`** — deterministic Bloomberg-free training proof.
- **`SCORE_TECHNICAL_DEMO.bat`** — Bloomberg-free frozen-model scoring proof.
- **`OPEN_TECHNICAL_RESULTS.bat`** — open the latest dashboard without pulling or calculating.

The launcher self-repairs missing pip with the selected interpreter, rebuilds
only its managed environment if repair fails, validates dependencies with
`python -m pip check`, and never writes an environment into the repository.

To validate the whole workflow without Bloomberg, double-click
**`RUN_TECHNICAL_DEMO.bat`**. Demo output is prominently marked synthetic and is
stored separately from live data, so it cannot contaminate the live store.

## What is produced

- `dist\technical_signal_dashboard.html` — current targets, diagnostics,
  backtests, expiry controls, and methodology in one portable file.
- `dist\technical_live_signals.csv` — current BUY/SELL/WATCH/FLAT/blocked board.
- `dist\technical_strategy_scorecard.csv` — one row per spread/strategy.
- `dist\technical_backtest_trades.csv` — auditable next-bar-open trade ledger.
- `dist\technical_fold_metrics.csv` — walk-forward fold results.
- `dist\technical_backtest_windows.csv` — every train/validation/embargo/OOS fold and the hard lockbox boundary.
- `dist\technical_portfolio_lockbox_trades.csv` — selected-strategy lockbox audit after the three-entry daily cap.
- `dist\technical_features.parquet` — full calculated feature table.
- `dist\technical_data_quality.csv` — blocking and non-blocking data checks.
- `dist\technical_expiry_calendar.csv` — exact risk, D-4 exit, and D-3 dates.
- `dist\technical_current_indicators.csv` — one current advanced-TA and risk-regime row per structure.
- `dist\technical_spread_legs.csv` — auditable normalized and executable leg economics.
- `dist\technical_parameter_catalog.csv` — run, liquidity, indicator, and cost settings.
- `dist\technical_daily_spread_history.csv` — 120-session end-of-window history for charting.
- `dist\technical_daily_settle_spreads.parquet` — exact-month daily package history from 2022.
- `dist\technical_seasonality_profiles.csv` — prior-year-only seasonal expected moves and support.
- `dist\technical_adaptive_weight_history.csv` — causal expert weights and learning state.
- `dist\technical_indicator_library_audit.csv` — native Polars versus lightweight `polars-talis`; the Pandas-dependent `polars-ta` comparison is optional.
- `dist\technical_structure_coverage.csv` — every registered package, including unavailable far legs.
- `dist\technical_structure_summaries.csv` — one current trade brief per structure with target levels, selected OOS evidence, and latest-30-session results.
- `output\pdf\Technical_Product_Report.pdf` — primary clean report, split into HO, CL, CO, and QS product chapters.
- `output\csv\Technical_Trade_Levels.csv` — broker-facing codes, buy/sell/fair levels, conversions, evidence, and portfolio priority.
- `dist\technical_model_summary.json` — overall model-creation and frozen-lockbox summary.
- `dist\technical_release_validation.json` — independent post-run proof receipt.
- `dist\technical_scoring_manifest.json` — frozen-model score-only receipt and timings.
- `models\live\latest_model.json` — selected per-structure evidence and frozen expert weights.

All CSV and Parquet outputs are rebuilt by the training workflow. Training also
refreshes the 21-sheet `Technical_Trading_System.xlsx`; use
`EXPORT_TECHNICAL_WORKBOOK.bat` for an explicit workbook refresh. Every
successful training or score-only run refreshes the product PDF. Use
`EXPORT_TECHNICAL_PDF.bat` to rebuild only that PDF from current validated
outputs. Score-only leaves the heavier workbook export optional so current-price
scoring stays fast.

## Data scope

- Roots: `HO`, `CL`, `CO`, `QS`.
- Interval: 15 minutes.
- New York window: bar starts from 08:00 through 14:15; every bar must finish by
  14:30. There are 26 complete bars in a normal session.
- Intraday retention: rolling 18 calendar months in local Parquet.
- Daily history: contract-level `PX_SETTLE`, labeled `PX_LAST` fallback, volume,
  and lagged open interest from 2022-01-01 for point-in-time seasonality.
- Forward curve: M1–M16 outputs plus two extra requested contracts per root for
  exact-delivery alignment at the far edge.
- XBBG: pinned `xbbg[polars]==1.4.6`, native Polars backend, bounded async
  concurrency, `gapFillInitialBar=False`.

Standard Bloomberg DAPI intraday history may be limited to roughly 140 calendar
days, while production requires at least 300 complete
trading sessions. A fresh workstation can verify Bloomberg and begin the local
archive, but `TRAIN_AND_SCORE.bat` intentionally fails closed rather than publish
an under-covered model. Seed a licensed 18-month CSV/Parquet archive:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_technical_windows.ps1 `
  -Mode live -Backfill "D:\licensed_data\intraday_backfill.parquet"
```

Coverage is visible in the dashboard and data-quality file. The pipeline never
pretends XBBG can bypass the underlying Bloomberg retention.

## Spread library

The package includes 337 registered structures. It retains the 39 hand-curated
nearby packages, expands the systematic curve through M16, and removes duplicate
economic identities. Three hundred thirty-one structures are model-enabled;
six algebraically dependent or deliberately diagnostic rows cannot produce an
actionable signal. The registry includes:

- Same-delivery HO/WTI: `42 × HO_M − CL_M` for every M1–M16 cohort.
- Expiry-aligned HO/Brent: `42 × HO_M − CO_(M+1)`.
- QS/Brent and QS/WTI: `QS_M / 7.45 − crude_M`, executed as four QS versus
  three crude contracts where that package is used.
- Gasoil conversion audit: `USD/bbl = USD/MT / 7.45`; `cpg = USD/MT / 7.45 / 0.42`.
- Quote convention: every crack level and target is USD/bbl; HO-only calendars,
  flies, and condors are cpg. Internal calculation fields remain normalized
  USD/bbl and are explicitly separated from the user-facing display fields.
- Sixty adjacent calendars, 56 single-product flies, 14 HOGO relative flies,
  and 52 equal-wing condors.
- HO/WTI and gasoil/Brent cracks, HO/QS and Brent/WTI relative-value pairs,
  crack-calendar boxes, curve boxes, and transatlantic crack boxes through M16.
- HOGO outrights through M16, adjacent HOGO boxes through M15, and HOGO flies
  through M14, plus crude-basis boxes and relative crack-curve diagnostics.

`HO1` means the first *complete executable package* whose exact delivery-month
legs remain position-eligible. It does not mean subtracting independent
Bloomberg generic-1 series after one market has rolled.

Spread definitions are editable in:

- `config\spread_library.csv`
- `config\spread_legs.csv`
- `config\technical_system.toml`

## Expiry rule

The earliest verified Bloomberg risk date among all legs controls the spread.
The engine:

- allows no new entry on the last common session before D-3;
- liquidates an existing package at the final 14:15 bar open on that session (D-4);
- asserts the position is flat before the first 08:00 bar on D-3;
- remains blocked through expiry/roll; and
- closes and reopens around a roll—no artificial continuous-series P&L.

Missing expiry metadata fails closed. Calendar approximations are used only in
demo/preflight output and are explicitly labelled unverified.

## Volume and market depth

Volume is a hard gate. The system keeps every leg's contracts, barrel-equivalent
volume, event count, average-trade-size proxy, time-of-day relative volume, PVO,
OBV proxy, and prior-session open interest. Executable package capacity is the
minimum of `leg volume / required contracts`; leg volume is never summed into a
fictional “spread volume.”

The advanced guardrail pack adds a robust same-time-of-day price shock,
one-day/20-day volatility regime ratio, liquidity-stress ratio, 20-day tail
rate, robust volume surprise, five-day skew and excess kurtosis, realized
volatility-of-volatility, a three-day HAC trend t-statistic, and five-day path
choppiness. The dashboard and workbook show the resulting risk regime and the
scope of available multi-leg relationship evidence for every structure.

Extreme time-of-day-normalized moves and robust volume dryness are labelled as
candidate risk gates without creating a long/short opinion. A preregistered
ablation did not meet the promotion standard, so they remain diagnostic and do
do not create a directional rule. The expanded registry contains nine
strategies and 2,979 preregistered trials. The full
disabled-versus-enabled experiment is reproducible with
`python scripts\run_advanced_gate_ablation.py`; its audit receipt is
`dist\advanced_gate_ablation.json`, and the lockbox is excluded from the
promotion decision.

The depth hierarchy is:

1. `BPIPE_L2` — true current Level 2 when B-PIPE and entitlements are present.
2. `TOP_OF_BOOK` — Terminal bid, ask, and displayed sizes.
3. `BAR_PROXY_ONLY` — bid/ask bar width, volume, events, and open interest.

Standard BDIB does not contain historical Level 2. Backtests therefore label and
use historical liquidity proxies. Live confidence is capped without true L2 and
can be configured to require it.

## Runtime and performance

The operating environment is Pandas-free. Core ingestion and calculations use
XBBG's native Polars backend, Polars expressions, Arrow/Parquet, and bounded
NumPy only where the statistical calculation requires it. SciPy, scikit-learn,
Numba, and Pandas are not installed in the base environment.

On the reference Apple Silicon validation machine, the final 15-minute,
18-month demo completes exhaustive analysis in about 45.6 seconds. Frozen-model
score-only takes about 4.2 seconds and reproduces all 337 current rows with zero
status, null, or numeric differences. The 21-sheet workbook exports in about
16.5 seconds; the 30-page product PDF takes about 1.6 seconds, and the
self-contained dashboard is about 9.4 MB. Peak resident memory was about
7.8 GiB for training and 2.4 GiB for scoring; use a 32 GB workstation for the
default two workers or set one worker on a 16 GB machine.
`backtest.parallel_workers` defaults to 2; set it to 1 on a memory-constrained
Windows machine.

The exhaustive run calculates 3,434,704 spread-feature rows once and reuses the
compact result across all strategies. Score-only limits its calculation frame
to 80 recent sessions while loading the frozen model and seasonality state.

See [docs/PERFORMANCE_BENCHMARK.md](docs/PERFORMANCE_BENCHMARK.md) for exact
stage timings, parity evidence, and Windows/Bloomberg timing expectations.

## Optional Kronos forecast expert

`NeoQuasar/Kronos-base` is isolated from the operating environment because its
PyTorch stack and pinned model/tokenizer files are materially larger. To enable
the optional diagnostic:

1. Double-click **`INSTALL_KRONOS_OPTIONAL.bat`**. It creates
   `%USERPROFILE%\Pyenvs\bbg_technical_kronos` and caches exact Hugging Face
   revisions under `%LOCALAPPDATA%\BloombergTechnicals\huggingface`.
2. Set `enabled = true` in `config\kronos.toml`.
3. Run the normal train or score launcher. The isolated sidecar is invoked
   automatically, or use **`RUN_KRONOS_FORECAST.bat`** manually.

The adapter is Pandas-free: real contract OHLCV moves through Polars → NumPy →
Torch. Forecasts are made once per unique executable contract and then
recombined through registered spread leg price weights. It never fabricates
multi-leg high/low bars. Kronos remains `EXPERIMENTAL` and cannot affect an
actionable signal until it accumulates at least 30 untouched forward sessions
and a future release adds an explicit promotion gate.

## Strategies and validation

Nine preregistered strategies are tested separately: robust mean reversion,
liquidity-confirmed breakout, volatility squeeze, session VWAP reversion,
seasonal error-correction residual, stability-qualified reversion, executable
flow divergence, a fixed regime ensemble, and a causal fixed-share expert mix.

The adaptive strategy is intentionally constrained. Seven fixed rule experts
are pooled by economic family and tenor bucket. Weights update once per completed
session only after a delayed 26-bar (one-session), cost-aware outcome is known. Learning does
not begin before 60 resolved observations, confidence is capped below 120, each
expert has a weight ceiling, and the final lockbox is frozen. It learns which
predeclared conditions are working; it does not invent indicators, retune the
rules, or train on the future.

Signals use only information through bar close and fill at the next bar open.
All-leg slippage and commissions are charged. Performance is aggregated to
daily P&L before Sharpe/Sortino. Each expanding walk-forward fold starts with
180 training sessions, uses 30 validation sessions, skips a five-session
embargo, and scores 15 genuinely OOS sessions. The final 30 sessions sit outside
every fold as an evaluation-only lockbox. The report
includes cost stress, drawdown, profit factor, fold consistency, long/short
results, and expiry exits. Complexity tiers raise the minimum OOS trade hurdle
for flies, boxes, and condors; the deflated-Sharpe trial penalty counts all 2,979
model-enabled spread/strategy trials, including zero-trade combinations.
Dependent identities never enter model selection. A strategy remains
`RESEARCH_ONLY` unless every configured hurdle passes; `NO TRADE` is an
expected result.

At the end of training, one preregistered strategy is selected per structure by
validated status, deflated-Sharpe probability, profitable-fold share, OOS
Sharpe, OOS net P&L, and OOS trade count. Its evidence and the pre-lockbox expert
weights are written to a versioned artifact. Score-only verifies the artifact's
configuration and engine-source hash, then fails closed if identical source data
does not reproduce the training signal snapshot exactly.

The **Trade brief** dashboard view provides current level, buy/sell thresholds,
fair-value target, long/short stops, confidence, expiry, liquidity, selected OOS
strategy, latest-30-session results, and a dedicated **Dynamic pattern & risk**
diagnostic panel in one place. The live decision board exposes the frozen
multi-expert pattern state, strength, agreement, shock, volatility, tail,
liquidity-stress, robust-volume, and relationship-scope fields. **Model results** reports
the complete trial count, selected strategy distribution, OOS metrics, and the
untouched 30-session lockbox. A frozen model older than five completed sessions
is marked `MODEL STALE` and cannot promote a BUY or SELL until retraining.

The same information is exported to a landscape PDF designed for printing and
handoff. Each product chapter includes a product snapshot, current decision
levels, dynamic pattern synthesis, and a complete structure sheet.

The system can make technical evidence the center of a repeatable workflow, but
it intentionally does not present technicals as a substitute for position
sizing, market fundamentals, event risk, or human execution controls.

See [docs/TECHNICAL_METHODOLOGY.md](docs/TECHNICAL_METHODOLOGY.md) for indicator,
execution, and source details.
The evaluated decision to retain the optimized Polars/NumPy research engine and
reserve NautilusTrader for a future component-fill shadow harness is documented
in [docs/BACKTEST_ENGINE_DECISION.md](docs/BACKTEST_ENGINE_DECISION.md).

For future AI development, start with
[docs/AI_DEVELOPMENT_HANDOFF.md](docs/AI_DEVELOPMENT_HANDOFF.md). Bloomberg
installation and live certification are documented in
[docs/BLOOMBERG_WINDOWS_SETUP.md](docs/BLOOMBERG_WINDOWS_SETUP.md).

## Verification

Every Windows run finishes with `technical_release_validation.json`. GitHub's
`Windows production smoke` workflow exercises the real demo `.bat` training and
score launchers on Windows PowerShell 5.1, then runs 113 Python tests and 25
JavaScript tests. Local release proof also verifies XBBG/BLPAPI native loading,
the 21-sheet workbook topology, exact train/score parity, all 2,979 trials,
next-bar fills, D-4 exits, adaptive bounds, file hashes, and the Pandas-free
runtime.

Before this release, every base dependency was also resolved with
`pip download --only-binary=:all:` for CPython 3.12 and 3.13 on
`win_amd64`; Bloomberg's official index supplied the matching BLPAPI Windows
wheel.

## Command-line examples

```powershell
# Full live training, lockbox evaluation, model freeze, and current scoring
python scripts\run_technical_system.py --mode live --workflow train

# Fast current-price scoring with the frozen model
python scripts\run_technical_system.py --mode live --workflow score

# Recalculate from the stored live snapshot without Bloomberg
python scripts\run_technical_system.py --mode live --workflow score --skip-pull --no-open

# Deterministic paper-data validation
python scripts\run_technical_system.py --mode demo --workflow train --no-open

# Reproduce the disabled-versus-enabled candidate risk-gate ablation
python scripts\run_advanced_gate_ablation.py
```

This is a research and decision-support system, not an autonomous order router
or investment recommendation. Bloomberg entitlements and data use remain
subject to the firm's Bloomberg agreement.
