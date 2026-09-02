# Performance and release benchmark

Measured September 1, 2026 on the reference Apple Silicon workstation with
Python 3.12.12, Polars 1.44.1, PyArrow 19.0.1, and two backtest workers.
Synthetic demo inputs are deterministic; timings exclude Bloomberg network
latency unless explicitly stated.

| Workflow | Current release |
|---|---:|
| Exhaustive 15-minute train analysis | 45.55 s |
| Frozen score analysis | 4.17 s |
| 21-sheet workbook | 16.50 s |
| 30-page product PDF | 1.64 s |
| Dashboard payload | ~9.4 MB |

Current train stages were approximately: seasonality 10.16 s, backtests 14.73 s,
spread construction 4.63 s, indicators 7.16 s, adaptive learning 2.85 s,
model/signals 3.34 s, and reporting 2.46 s. Score-only used 1.68 s for recent
spreads, 1.25 s for indicators/frozen-model scoring, 0.67 s for signals, and
0.45 s for reporting.

The performance changes preserve the quantitative contract:

- 337 structures, 331 model-enabled structures, and 2,979 preregistered trials.
- 16,022 auditable trades and 3,434,704 calculated spread-feature rows.
- Exact train/score parity: 337 rows, zero status or null mismatches, maximum
  numeric delta 0.0.
- Serial and two-worker backtests produce identical trades, scorecards, folds,
  and equity.
- Workbook output retains the 21-sheet topology and existing style/table/chart
  contract while adding the advanced risk fields to the trade brief.

Low-risk changes include compact 41-column Polars worker frames, worker
initialization once per spawned Windows process, cached invariant simulation
inputs, direct strategy-vote lookup with null guards, native Polars roll and
business-day expressions, fused seasonality quantiles, expiry-DTE caching,
root-frame reuse, latest-session-only depth discovery, daily-plus-current
intraday dashboard history, shared immutable openpyxl styles, and grouped native
Polars risk windows with Float32 outputs.

## Windows timing estimate

On a modern 8-core Windows workstation, allow approximately 60–120 seconds for
model calculations, 20–35 seconds for the workbook, and 5–10 seconds for frozen
scoring after data are local. Use 32 GB RAM for the default two workers. On a
16 GB machine, set `backtest.parallel_workers = 1`; expect lower peak memory and
a longer exhaustive run.

Bloomberg is the long pole. A fresh pull can require about 14 reference batches,
16 daily batches, and 1,380 bounded BDIB calls; allow roughly 8–30 minutes plus
the 12-second depth capture, depending on Terminal, entitlements, proxy, and
throttling. An incremental five-day refresh is about 225 BDIB calls and is more
likely to take 2–8 minutes. These network ranges are estimates, not a service
guarantee. `CHECK_BLOOMBERG_READY.bat` is the authoritative workstation proof.

The base dependency set was independently resolved as binary-only Windows wheels
for CPython 3.12/cp312 and 3.13/cp313 on `win_amd64`. BLPAPI 3.26.7.1 resolved
from Bloomberg's official package index for both targets.
