# AI development handoff - Bloomberg Technicals

This file is the starting context for any future AI developer. Read it together
with `README.md`, `docs/TECHNICAL_METHODOLOGY.md`,
`docs/ADVANCED_INDICATORS.md`, and `docs/BLOOMBERG_WINDOWS_SETUP.md` before
changing the system.

## Mission

Build a Windows-first, Bloomberg-backed technical decision-support system for
distillate and crude structures. The system must provide current levels, entry
thresholds, fair-value targets, risk stops, dynamic multi-indicator patterns,
liquidity/expiry gates, out-of-sample evidence, and an untouched latest
30-session evaluation. It is not an autonomous order router.

## User-facing outputs

- `output/pdf/Technical_Product_Report.pdf`: the primary clean deliverable,
  split into HO, CL, CO, and QS product chapters.
- `output/csv/Technical_Trade_Levels.csv`: broker-facing trade codes, current,
  buy/sell/fair/stop levels, exact gasoil/HOGO conversions, evidence, and the
  three-entry portfolio priority.
- `dist/technical_signal_dashboard.html`: interactive current board, trade
  brief, model evidence, diagnostics, and methodology.
- `Technical_Trading_System.xlsx`: 21-sheet audit workbook.
- `dist/technical_live_signals.csv`: current machine-readable decision board.
- `dist/technical_structure_summaries.csv`: current targets plus selected OOS
  and latest-30-session evidence for every structure.
- `dist/technical_release_validation.json`: independent release gate.
- `dist/bloomberg_preflight.json`: live workstation BDP/BDH/BDIB and
  subscription receipt.

## Architecture and source of truth

```text
config/technical_system.toml
  -> app/technical_config.py          validated settings and instruments
  -> app/technical_data.py            Bloomberg pull, normalization, storage
  -> app/technical_analytics.py       causal spread and indicator calculations
  -> app/technical_backtest.py        experts, adaptive learner, walk-forward
  -> app/model_artifact.py            frozen model contract
  -> app/technical_summary.py         per-structure evidence and descriptions
  -> app/technical_reporting.py       CSV, Parquet, JSON, dashboard
  -> scripts/build_technical_pdf.py   product-split PDF
  -> scripts/build_technical_workbook.py
  -> scripts/validate_technical_release.py
```

Windows orchestration is `scripts/run_technical_windows.ps1`. It owns the
managed environment, Bloomberg SDK installation, model rollback, artifact
rollback, train/score routing, PDF/workbook generation, and final validator.

## Dynamic pattern engine

The model does not merely stack indicators. Seven fixed, interpretable experts
combine different information sets:

1. Robust mean reversion - median/MAD z, RSI, efficiency and stability.
2. Trend breakout - Donchian close break, MACD, efficiency and relative volume.
3. Volatility squeeze - Bollinger compression, breakout, volume and PVO.
4. Session VWAP reversion - VWAP deviation, volatility and time slot.
5. Seasonal/error correction - prior-year move, support and confidence.
6. Stability reversion - robust z, variance ratio and reversion stability.
7. Flow divergence - robust z, signed-volume proxy and effort-versus-result.

Each expert emits long, short, or neutral. The adaptive learner updates expert
weights once per completed session after a delayed 26-bar (one-session)
cost-aware outcome.
Weights are learned by family and tenor group, shrunk toward uniform, capped,
and frozen before the final 30-session lockbox. Score-only runs load those
weights unchanged.

The current `pattern_state` is a reporting surface over this frozen combination:

- `BULLISH_CONSENSUS` / `BEARISH_CONSENSUS`: the mature adaptive vote fired;
- `BULLISH_CLUSTER` / `BEARISH_CLUSTER`: multiple same-side experts, but no
  adaptive entry;
- `BULLISH_FRAGMENT` / `BEARISH_FRAGMENT`: only one expert;
- `MIXED_PATTERN`: conflicting experts;
- `STRUCTURAL_BREAK`: change-point alarm;
- `WARMUP`, `MODEL_STALE`, `ANALYTIC_ONLY`, or `NO_PATTERN` as applicable.

`pattern_state`, `pattern_strength`, `pattern_agreement`, and
`pattern_components` cannot alter direction, confidence, status, or the trial
count. Promotion remains controlled by validated selected-strategy evidence and
the production gates.

## Non-negotiable quantitative invariants

- Completed close only; any simulated entry occurs at the next bar open.
- No look-ahead in seasonal, time-of-day, volume, adaptive, or expiry fields.
- Final 30 sessions are a frozen lockbox and may not select formulas,
  thresholds, experts, or gates.
- Each fold uses 180 expanding training sessions, 30 validation sessions, a
  five-session embargo, and 15 actual OOS sessions before the final lockbox.
- At most three new entries may be selected per session, with no duplicate
  algebra group; candidate ranking cannot inspect the trade outcome.
- All package legs, ratios, conversions, commissions, and slippage are charged.
- Gasoil is `USD/MT / 7.45` in USD/bbl and `USD/MT / 7.45 / 0.42` in cpg;
  HOGO display levels use the same cpg basis.
- Every crack display level and target is USD/bbl. Every HO-only calendar, fly,
  and condor display level and target is cpg; internal normalized fields remain
  separately labelled USD/bbl calculations.
- Synthetic spread volume is `min(leg volume / required contracts)`, never a
  sum of leg volumes.
- No synthetic high/low from asynchronous legs.
- Earliest leg risk date controls; mandatory exit is D-4; flat before D-3.
- Missing Bloomberg expiry or blocking data fails closed.
- Multiple-testing count includes every model-enabled spread/strategy trial,
  including zero-trade trials.
- A new directional expert requires incremental walk-forward evidence and an
  increased trial count.
- Candidate gates may not be promoted from lockbox performance.

## Standard workflows

Windows:

```bat
SETUP_AND_CHECK_BLOOMBERG.bat
TRAIN_AND_SCORE.bat "D:\licensed_data\intraday_backfill.parquet"
SCORE_CURRENT.bat
OPEN_TECHNICAL_RESULTS.bat
```

Cross-platform demo development:

```text
python scripts/run_technical_system.py --mode demo --workflow train --no-open
python scripts/build_technical_workbook.py --mode demo
python scripts/build_technical_pdf.py --mode demo
python scripts/run_technical_system.py --mode demo --workflow score --reuse-demo --no-open
python scripts/validate_technical_release.py --mode demo --workflow score
python -m pytest -q tests
node --test tests/app_var.test.js tests/trade_math.test.js
```

## Testing contract

After a calculation change:

1. Add causal boundary, null/zero, and cross-spread isolation tests.
2. Run serial and two-worker parity tests.
3. Regenerate train artifacts and confirm 2,979-trial completeness, next-bar
   execution, D-4 exits, adaptive bounds, and model identity.
4. Run score-only and require exact train/score target parity.
5. Build the workbook and PDF.
6. Render the PDF with Poppler and visually inspect representative pages from
   the cover, each product chapter, long tables, and appendices.
7. Run the Windows launcher workflow in GitHub Actions. A macOS import test is
   not a live Bloomberg or native Windows certification.

## Bloomberg contract

The live connection is local DAPI on `localhost:8194` with XBBG's Polars
backend. BLPAPI is installed from Bloomberg's official package index. Preflight
must exercise reference, historical, and intraday requests for all four roots.
Current subscriptions and true Level 2 are separate entitlements and must never
be inferred from ordinary bar data.

## Safe development priorities

1. Calibrate realized execution costs from actual fills without changing the
   executable package definition.
2. Add live drift monitoring for expert weights, feature distributions, source
   latency, and model age.
3. Evaluate true B-PIPE depth and order-book features only when the entitlement
   and historical data exist.
4. Add conformal or empirical forecast intervals around targets without turning
   them into certainty claims.
5. Extend the existing three-entry portfolio audit only after structure-level
   signals are live validated; preserve the one-entry-per-algebra-group rule.
6. Add fundamental context as a separately labelled overlay; do not let revised
   data contaminate causal technical backtests.

Avoid adding textbook indicators solely to increase feature count. ATR, ADX,
stochastic, CCI, candlesticks, CMF, MFI, VPIN, and true order-flow imbalance
require fields the current asynchronous spread dataset does not reliably have.
Opaque HMM/deep ensembles require a preregistered incremental test and a clear
endpoint/dependency review.

## Known limitations

- Demo data prove workflow behavior, not market profitability.
- Live Bloomberg operation requires the intended Windows login and
  entitlements.
- Historical BDIB is not historical Level 2.
- The optional Kronos sidecar is diagnostic and action-disabled until it has
  sufficient forward evaluation.
- No result should be called hedge-fund-grade merely because the software is
  fast; operational monitoring, independent model validation, and live fill
  reconciliation remain necessary.

## Prompt for a future AI session

```text
Read docs/AI_DEVELOPMENT_HANDOFF.md, README.md,
docs/TECHNICAL_METHODOLOGY.md, docs/ADVANCED_INDICATORS.md, and
docs/BLOOMBERG_WINDOWS_SETUP.md. Preserve every non-negotiable invariant.
Inspect the current git diff and generated release receipts before editing.
Implement the requested change with Pandas-free Polars, add focused causal and
Windows contract tests, rerun demo train and frozen score, require exact parity,
render and inspect the product PDF, then create a new checksummed Windows ZIP.
Do not claim live Bloomberg certification unless the Windows preflight receipt
from the intended workstation passes.
```
