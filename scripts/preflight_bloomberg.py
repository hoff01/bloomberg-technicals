#!/usr/bin/env python
"""Exercise the licensed Bloomberg surfaces required by the technical system."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
from datetime import date, timedelta
import json
from pathlib import Path
import struct
import sys
from typing import Any

import polars as pl

EXPIRY_FIELDS = {"FUT_LAST_TRADE_DT", "LAST_TRADEABLE_DT"}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.technical_config import (  # noqa: E402
    ContractDefinition,
    exchange_session_offset,
    load_technical_config,
)
from app.technical_data import (  # noqa: E402
    TechnicalDataError,
    XbbgTechnicalClient,
    normalize_daily_frame,
)


def _usable_reference(frame: pl.DataFrame) -> bool:
    if frame.is_empty():
        return False
    field_column = next(
        (column for column in frame.columns if column.lower() == "field"), None
    )
    value_column = next(
        (column for column in frame.columns if column.lower() == "value"), None
    )
    if field_column and value_column:
        return not frame.filter(
            pl.col(field_column).cast(pl.Utf8).str.to_uppercase().is_in(EXPIRY_FIELDS)
            & pl.col(value_column).is_not_null()
        ).is_empty()
    columns_by_upper = {column.upper(): column for column in frame.columns}
    expiry_columns = [
        columns_by_upper[field]
        for field in EXPIRY_FIELDS
        if field in columns_by_upper
    ]
    return bool(expiry_columns) and frame.select(
        pl.any_horizontal(pl.col(expiry_columns).is_not_null()).any()
    ).item()


async def _run(project_root: Path, as_of: date) -> dict[str, Any]:
    import blpapi
    import xbbg

    base_config = load_technical_config(
        project_root / "config" / "technical_system.toml"
    )
    config = replace(
        base_config,
        bloomberg=replace(
            base_config.bloomberg,
            request_timeout_seconds=min(
                30, base_config.bloomberg.request_timeout_seconds
            ),
            retry_max_retries=0,
        ),
        liquidity=replace(base_config.liquidity, capture_seconds=3),
    )
    client = XbbgTechnicalClient(config)
    universe = config.build_contract_universe(
        as_of,
        as_of,
        history_buffer_months=0,
        forward_months=5,
    )
    selected: list[ContractDefinition] = []
    reference_checks: list[dict[str, Any]] = []
    for root in config.roots:
        candidates = [
            item
            for item in universe
            if item.root == root and item.fallback_expiry >= as_of + timedelta(days=7)
        ][:3]
        errors: list[str] = []
        for candidate in candidates:
            try:
                response = await client.fetch_reference((candidate,), batch_size=1)
                if _usable_reference(response):
                    selected.append(candidate)
                    reference_checks.append(
                        {"root": root, "ticker": candidate.ticker, "status": "PASS"}
                    )
                    print(f"BBG BDP PASS {root}: {candidate.ticker}", flush=True)
                    break
                errors.append(f"{candidate.ticker}: empty reference response")
            except Exception as exc:
                errors.append(f"{candidate.ticker}: {type(exc).__name__}: {exc}")
        else:
            raise TechnicalDataError(
                f"No configured {root} dated ticker passed BDP: " + "; ".join(errors)
            )

    history_start = as_of - timedelta(days=14)
    daily = await client.fetch_daily(selected, history_start, as_of, batch_size=4)
    normalized_daily = normalize_daily_frame(
        daily, {item.ticker: item for item in selected}
    )
    daily_checks = (
        normalized_daily.group_by("root")
        .agg(
            pl.len().alias("rows"),
            pl.col("session_date").min().alias("first_session"),
            pl.col("session_date").max().alias("last_session"),
        )
        .sort("root")
        .to_dicts()
    )
    daily_roots = set(normalized_daily["root"].unique().to_list())
    missing_daily_roots = sorted(set(config.roots) - daily_roots)
    if normalized_daily.is_empty() or missing_daily_roots:
        raise TechnicalDataError(
            "BDH preflight is missing usable roots: "
            + ", ".join(missing_daily_roots or sorted(config.roots))
        )
    print(f"BBG BDH PASS: {normalized_daily.height:,} normalized rows", flush=True)
    for check in daily_checks:
        print(
            f"BBG BDH ROOT {check['root']}: {check['rows']:,} rows "
            f"{check['first_session']} to {check['last_session']}",
            flush=True,
        )

    intraday_start = exchange_session_offset(as_of, -3, "HO")
    intraday, stats = await client.fetch_intraday(
        selected,
        intraday_start,
        as_of,
        event_types=(config.bloomberg.event_type,),
    )
    intraday_roots = set(intraday["root"].unique().to_list()) if not intraday.is_empty() else set()
    missing_intraday_roots = sorted(set(config.roots) - intraday_roots)
    if intraday.is_empty() or stats.trade_failed or missing_intraday_roots:
        raise TechnicalDataError(
            "BDIB preflight failed: "
            f"rows={intraday.height}, failed={stats.trade_failed}, "
            f"missing_roots={missing_intraday_roots}"
        )
    print(f"BBG BDIB PASS: {intraday.height:,} normalized bars", flush=True)
    intraday_checks = (
        intraday.group_by("root")
        .agg(
            pl.len().alias("rows"),
            pl.col("security").n_unique().alias("securities"),
            pl.col("session_date").min().alias("first_session"),
            pl.col("session_date").max().alias("last_session"),
        )
        .sort("root")
        .to_dicts()
    )
    for check in intraday_checks:
        print(
            f"BBG BDIB ROOT {check['root']}: {check['securities']} securities, "
            f"{check['rows']:,} bars {check['first_session']} to {check['last_session']}",
            flush=True,
        )

    tickers = [item.ticker for item in selected]
    liquidity, depth_source, depth_warnings = await client.capture_liquidity(tickers)
    subscription_status = "PASS" if not liquidity.is_empty() else "WARN"
    print(
        f"BBG SUBSCRIPTION {subscription_status}: {depth_source}, "
        f"rows={liquidity.height}",
        flush=True,
    )
    return {
        "status": (
            "PASS" if subscription_status == "PASS" else "PASS_WITH_WARNINGS"
        ),
        "as_of": as_of.isoformat(),
        "runtime": {
            "python": sys.version.split()[0],
            "python_bits": struct.calcsize("P") * 8,
            "blpapi_python": blpapi.__version__,
            "blpapi_cpp": blpapi.cpp_sdk_version(),
            "xbbg": xbbg.__version__,
            "xbbg_sdk": xbbg.get_sdk_info(),
        },
        "connection": {
            "host": config.bloomberg.host,
            "port": config.bloomberg.port,
            "backend": "polars",
            "request_timeout_seconds": config.bloomberg.request_timeout_seconds,
            "retry_max_retries": config.bloomberg.retry_max_retries,
        },
        "requested_fields": {
            "reference": list(config.bloomberg.reference_fields),
            "daily": list(config.bloomberg.daily_fields),
            "intraday_event_types": [config.bloomberg.event_type],
            "top_of_book": list(config.liquidity.top_of_book_fields),
        },
        "reference_checks": reference_checks,
        "daily_rows": normalized_daily.height,
        "daily_checks": daily_checks,
        "intraday_rows": intraday.height,
        "intraday_checks": intraday_checks,
        "intraday_stats": asdict(stats),
        "subscription_status": subscription_status,
        "depth_source": depth_source,
        "depth_rows": liquidity.height,
        "depth_warnings": list(depth_warnings),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    try:
        receipt = asyncio.run(_run(args.project_root.resolve(), args.as_of))
    except Exception as exc:
        print(f"ERROR: Bloomberg preflight failed: {type(exc).__name__}: {exc}")
        return 1
    output = args.project_root.resolve() / "dist" / "bloomberg_preflight.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
    print(f"Bloomberg preflight receipt: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
