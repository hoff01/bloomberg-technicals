# Bloomberg API Windows setup and certification

## What is automated

`SETUP_AND_CHECK_BLOOMBERG.bat` is the single entry point on the licensed
Windows workstation. It performs two bounded operations:

1. `INSTALL_BLOOMBERG.bat` creates or repairs the 64-bit Python 3.12/3.13
   environment at `%USERPROFILE%\Pyenvs\bbg_technical_builder`, installs the
   pinned Pandas-free runtime, and installs `blpapi` from Bloomberg's official
   package index.
2. `CHECK_BLOOMBERG_READY.bat` opens a real Desktop API session on
   `localhost:8194` and exercises the exact data surfaces required by the
   technical system.

The setup never installs into the repository and never uses a repo-local
virtual environment on Windows.

## Prerequisites

- 64-bit Windows with 64-bit Python 3.12 or 3.13.
- Bloomberg Terminal installed, open, and logged in as the licensed user.
- Desktop API access for the configured HO, CL, CO, and QS futures.
- Permission to request reference data, daily history, and 15-minute intraday
  bars. Current subscription and Level 2 access depend on entitlement.
- A licensed 18-month historical backfill with at least 300 complete sessions
  before the first production training run.

## What the readiness check proves

The live preflight fails unless every configured root returns:

- BDP-style dated-futures reference data with a usable last-trade or
  tradeable-date field;
- BDH-style daily history;
- BDIB-style 15-minute trade bars; and
- valid normalized root, security, session, and bar records.

The receipt `dist\bloomberg_preflight.json` records:

- Python bitness and versions of Python, BLPAPI Python/C++ and XBBG;
- host, port, backend, timeout, and retry configuration;
- exact reference, history, intraday, and top-of-book fields;
- each dated ticker used for the BDP check;
- daily and intraday row counts by root and session range; and
- current subscription/depth source, row count, and warnings.

BDP, BDH, and BDIB are mandatory. A subscription warning does not claim true
Level 2. `BPIPE_L2`, `TOP_OF_BOOK`, and `BAR_PROXY_ONLY` remain distinct output
labels, and confidence is capped when true depth is unavailable.

## First production run

1. Extract the transfer ZIP to a normal local folder.
2. Open and log in to Bloomberg Terminal.
3. Double-click `SETUP_AND_CHECK_BLOOMBERG.bat`.
4. Review `dist\bloomberg_preflight.json`. Do not proceed if BDP, BDH, or BDIB
   failed.
5. After 14:45 New York time, run:

   ```bat
   TRAIN_AND_SCORE.bat "D:\licensed_data\intraday_backfill.parquet"
   ```

6. Use `SCORE_CURRENT.bat` for routine intraday refreshes. It reuses the frozen
   model and does not rerun the model search.

Every successful train or score refreshes the product PDF at
`output\pdf\Technical_Product_Report.pdf`.

## What cannot be certified off-workstation

Import and demo tests prove package compatibility and calculation behavior, but
they cannot prove a particular Terminal login, entitlement, proxy, or B-PIPE
permission. Only a successful `SETUP_AND_CHECK_BLOOMBERG.bat` receipt produced
on the intended Windows workstation is live certification for that machine.
