# Backtest engine decision

## Decision

Keep the Pandas-free Polars/NumPy research engine as the production model-search
path. Do not add NautilusTrader or PennyLane to the base Windows environment in
this release.

This is a scope and correctness decision, not a rejection of event-driven
software. The current engine is optimized for thousands of preregistered tests
over synchronized, executable multi-leg futures packages. It calculates the
indicators once, carries a compact worker frame, runs spread partitions in
parallel, fills at the next bar open, and calculates P&L from actual component
contract values and costs.

## Current walk-forward design

- 15-minute completed bars; 26 bars per normal session.
- 18 calendar months retained locally.
- 180-session expanding development window.
- 30-session validation block before each OOS sequence.
- Five explicit embargo sessions followed by 15 actual OOS test sessions in
  every fold.
- Final 30 completed sessions are an immutable evaluation lockbox.
- Nine strategies are tested independently against every model-enabled
  structure. Every zero-trade combination remains in the trial count.
- The lockbox cannot change strategy status, ranking, parameters, gates, expert
  weights, or the selected strategy.
- A separate portfolio audit limits new entries to three per session and one
  per algebraic group.

`dist/technical_backtest_windows.csv` is the machine-readable fold and lockbox
receipt. `dist/technical_strategy_library.csv` describes the nine strategies.
`dist/technical_portfolio_lockbox_trades.csv` shows the capped portfolio audit.

## NautilusTrader assessment

NautilusTrader provides a strong Rust-backed event, execution, portfolio, risk,
and matching engine with Python bindings. It is appropriate when recorded
quotes, trades, or order-book data are available and when the same component
order workflow will be deployed live.

It is not the base engine here because:

1. Nautilus synthetic instruments are analytical and cannot currently be
   traded directly. This system must execute and cost every HO, CL, CO, and QS
   component of a synthetic package.
2. Standard Bloomberg BDIB supplies bars, not historical queue state. An event
   engine cannot reconstruct L2/L3 execution from 15-minute bars.
3. There is no Bloomberg Desktop API adapter in the documented official adapter
   set, so the existing bounded XBBG pull and normalization layer would remain.
4. The high-level external-data examples introduce a catalog/wrangling layer
   and commonly use Pandas, conflicting with the lightweight base environment.
5. Official Windows testing targets Windows Server 2022. The user's personal
   Windows workstation may work, but is outside that stated support boundary.
6. NautilusTrader is under active development and its documentation warns that
   breaking API changes may occur.

Primary documentation:

- https://nautilustrader.io/docs/latest/concepts/backtesting/
- https://nautilustrader.io/docs/latest/concepts/synthetics/
- https://nautilustrader.io/docs/latest/concepts/backtesting/data-and-venues/
- https://nautilustrader.io/docs/latest/getting_started/installation/

## PennyLane assessment

PennyLane is a quantum-computing and quantum-machine-learning framework. Its
optimizers train parameterized quantum circuits; it is not a market replay,
portfolio accounting, matching, fill, or futures-spread backtest engine.
Adding it would materially increase dependency and model-mining risk without
solving the execution problem.

Documentation: https://docs.pennylane.ai/

## Safe future use of NautilusTrader

Add an optional, isolated shadow environment only after timestamped component
quotes or broker fills are available. The first adapter should replay the
already-frozen top three portfolio entries by submitting the actual leg orders,
then reconcile fills, partials, slippage, margin, and roll handling against the
current ledger. It must not reselect indicators or use the lockbox to tune
parameters. Promotion requires exact economic parity and a separate Windows
dependency gate.
