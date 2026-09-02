#!/usr/bin/env python
"""Generate a deterministic, clearly labelled paper-data set for validation.

This is not a market-data substitute.  It exists so installation, expiry,
volume, depth, spread construction, backtesting, and reporting can be tested on
any machine before the Bloomberg-enabled update is run.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.technical_config import (  # noqa: E402
    ContractDefinition,
    TechnicalConfig,
    add_months,
    load_technical_config,
)
from app.technical_data import (  # noqa: E402
    BAR_COLUMNS,
    DataPaths,
    TechnicalStore,
    normalize_contract_registry,
)


ROOT_PHASE = {"HO": 0.6, "CL": 0.0, "CO": 0.2, "QS": 1.1}
ROOT_VOLUME = {"HO": 1800.0, "CL": 6200.0, "CO": 3400.0, "QS": 1700.0}
GASOIL_BBL_PER_MT = 7.45
GASOIL_USD_BBL_FACTOR = 1.0 / GASOIL_BBL_PER_MT


def _sessions(start: date, end: date) -> list[date]:
    result: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def _latent_prices(
    session_index: int, slot: int, bars_per_session: int
) -> dict[str, float]:
    t = session_index + slot / float(bars_per_session)
    annual = math.sin(2 * math.pi * t / 252.0)
    slow = math.sin(2 * math.pi * t / 83.0)
    intraday = math.sin(2 * math.pi * (slot + 1) / float(bars_per_session))
    cl = 72.0 + 4.2 * annual + 1.5 * slow + 0.35 * intraday
    co = cl + 3.1 + 0.65 * math.sin(2 * math.pi * t / 55.0)
    ho_crack = (
        25.0
        + 5.6 * math.cos(2 * math.pi * (t - 42) / 252.0)
        + 1.7 * math.sin(2 * math.pi * t / 17.0)
        + 0.55 * intraday
    )
    qs_crack = (
        18.5
        + 4.2 * math.cos(2 * math.pi * (t - 24) / 252.0)
        + 1.4 * math.sin(2 * math.pi * t / 21.0)
        - 0.25 * intraday
    )
    return {
        "CL": cl,
        "CO": co,
        "HO": (cl + ho_crack) / 42.0,
        "QS": (co + qs_crack) / GASOIL_USD_BBL_FACTOR,
    }


def _native_curve_adjustment(root: str, delivery_month: date, session: date) -> float:
    month_distance = (delivery_month.year - session.year) * 12 + delivery_month.month - session.month
    seasonal_curve = math.sin(2 * math.pi * session.timetuple().tm_yday / 365.25)
    usd_bbl = -0.32 * month_distance + 0.18 * month_distance * seasonal_curve
    if root == "HO":
        return usd_bbl / 42.0
    if root == "QS":
        return usd_bbl / GASOIL_USD_BBL_FACTOR
    return usd_bbl


def _contract_registry(
    config: TechnicalConfig, start: date, end: date
) -> tuple[tuple[ContractDefinition, ...], pl.DataFrame]:
    definitions = config.build_contract_universe(
        start,
        end,
        history_buffer_months=7,
        forward_months=config.system.forward_curve_months + 2,
    )
    raw = pl.DataFrame(
        {
            "security": [item.ticker for item in definitions],
            "FUT_LAST_TRADE_DT": [item.fallback_expiry for item in definitions],
        }
    )
    registry = normalize_contract_registry(definitions, raw)
    return definitions, registry


def _eligible_by_session(
    definitions: tuple[ContractDefinition, ...], registry: pl.DataFrame
) -> dict[str, list[tuple[ContractDefinition, date]]]:
    registry_dates = {
        str(row["security"]): row["blackout_start"] for row in registry.to_dicts()
    }
    result: dict[str, list[tuple[ContractDefinition, date]]] = {}
    for definition in definitions:
        result.setdefault(definition.root, []).append(
            (definition, registry_dates[definition.ticker])
        )
    for rows in result.values():
        rows.sort(key=lambda item: item[0].delivery_month)
    return result


def generate_demo_frames(
    config: TechnicalConfig,
    *,
    as_of: date,
    seed: int = 73021,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    intraday_start = add_months(
        date(as_of.year, as_of.month, 1),
        -config.system.rolling_intraday_months,
    )
    daily_start = config.system.daily_history_start
    definitions, registry = _contract_registry(config, daily_start, as_of)
    eligible = _eligible_by_session(definitions, registry)
    tz = ZoneInfo(config.system.timezone)
    utc = ZoneInfo("UTC")

    daily_sessions = _sessions(daily_start, as_of)
    daily_rows: list[dict[str, object]] = []
    for session_index, session in enumerate(daily_sessions):
        latent = _latent_prices(
            session_index,
            config.system.complete_bars_per_session - 1,
            config.system.complete_bars_per_session,
        )
        for root, candidates in eligible.items():
            active = [item for item in candidates if session < item[1]][
                : config.system.forward_curve_months + 2
            ]
            for rank, (definition, _blackout) in enumerate(active, start=1):
                noise = rng.normal(0.0, {"HO": 0.0025, "QS": 0.8}.get(root, 0.12))
                close = max(
                    0.01,
                    latent[root]
                    + _native_curve_adjustment(root, definition.delivery_month, session)
                    + noise,
                )
                volume = ROOT_VOLUME[root] * (0.82 ** (rank - 1)) * rng.lognormal(0.0, 0.16)
                open_interest = ROOT_VOLUME[root] * 18.0 * (0.78 ** (rank - 1)) * rng.lognormal(0.0, 0.08)
                daily_rows.append(
                    {
                        "session_date": session,
                        "root": root,
                        "security": definition.ticker,
                        "delivery_month": definition.delivery_month,
                        "close": close,
                        "settle": close,
                        "last_price": close,
                        "settle_source_field": "PX_SETTLE",
                        "volume_contracts": float(volume),
                        "open_interest": float(open_interest),
                        "source": "DEMO_SYNTHETIC_NOT_MARKET_DATA",
                    }
                )

    intraday_sessions = [item for item in daily_sessions if item >= intraday_start]
    daily_session_index = {
        session: index for index, session in enumerate(daily_sessions)
    }
    bar_rows: list[dict[str, object]] = []
    last_prices: dict[str, float] = {}
    interval_scale = math.sqrt(config.system.bar_interval_minutes / 30.0)
    center_reversion = 1.0 - (1.0 - 0.24) ** (
        config.system.bar_interval_minutes / 30.0
    )
    middle_slot = (config.system.complete_bars_per_session - 1) / 2.0
    eia_slot = int(
        (10 * 60 + 30 - (8 * 60)) / config.system.bar_interval_minutes
    )
    eia_width = 1.7 * 30.0 / config.system.bar_interval_minutes
    for session in intraday_sessions:
        session_index = daily_session_index[session]
        for slot in range(config.system.complete_bars_per_session):
            start_et = datetime.combine(session, time(8, 0), tzinfo=tz) + timedelta(
                minutes=slot * config.system.bar_interval_minutes
            )
            latent = _latent_prices(
                session_index, slot, config.system.complete_bars_per_session
            )
            tod_shape = (
                0.72
                + 0.50 * abs(slot - middle_slot) / max(middle_slot, 1.0)
                + 0.24 * math.exp(-((slot - eia_slot) / eia_width) ** 2)
            )
            for root, candidates in eligible.items():
                active = [item for item in candidates if session < item[1]][
                    : config.system.forward_curve_months + 2
                ]
                root_spec = config.roots[root]
                for rank, (definition, _blackout) in enumerate(active, start=1):
                    center = latent[root] + _native_curve_adjustment(
                        root, definition.delivery_month, session
                    )
                    sigma = (
                        {"HO": 0.0016, "CL": 0.075, "CO": 0.070, "QS": 0.55}[root]
                        * interval_scale
                    )
                    previous = last_prices.get(definition.ticker, center)
                    close = max(
                        0.01,
                        previous
                        + center_reversion * (center - previous)
                        + rng.normal(0.0, sigma),
                    )
                    open_price = previous
                    high = max(open_price, close) + abs(rng.normal(0.0, sigma * 0.55))
                    low = min(open_price, close) - abs(rng.normal(0.0, sigma * 0.55))
                    last_prices[definition.ticker] = close
                    volume = (
                        ROOT_VOLUME[root]
                        / config.system.complete_bars_per_session
                        * tod_shape
                        * (0.76 ** (rank - 1))
                        * rng.lognormal(0.0, 0.24)
                    )
                    num_events = max(1.0, volume / rng.uniform(2.0, 7.0))
                    tick = root_spec.tick_size_native
                    width_ticks = 1.0 + rank * 0.35 + max(0.0, 0.8 - tod_shape) * 2.0
                    half_width = tick * width_ticks / 2.0
                    common = {
                        "timestamp_utc": start_et.astimezone(utc),
                        "bar_start_et": start_et,
                        "bar_end_et": start_et
                        + timedelta(minutes=config.system.bar_interval_minutes),
                        "session_date": session,
                        "bar_slot": slot,
                        "root": root,
                        "security": definition.ticker,
                        "delivery_month": definition.delivery_month,
                        "source": "DEMO_SYNTHETIC_NOT_MARKET_DATA",
                        "pulled_at_utc": datetime.combine(as_of, time(20, 0), tzinfo=utc),
                    }
                    bar_rows.append(
                        {
                            **common,
                            "event_type": "TRADE",
                            "open": open_price,
                            "high": high,
                            "low": low,
                            "close": close,
                            "volume_contracts": float(volume),
                            "num_events": float(num_events),
                            "value": float(close * volume),
                        }
                    )
                    for event_type, quote in (
                        ("BID", close - half_width),
                        ("ASK", close + half_width),
                    ):
                        bar_rows.append(
                            {
                                **common,
                                "event_type": event_type,
                                "open": quote,
                                "high": quote,
                                "low": quote,
                                "close": quote,
                                "volume_contracts": None,
                                "num_events": None,
                                "value": None,
                            }
                        )

    bars = pl.DataFrame(bar_rows).with_columns(
        pl.col("timestamp_utc").cast(pl.Datetime("us", "UTC")),
        pl.col("bar_start_et").cast(pl.Datetime("us", config.system.timezone)),
        pl.col("bar_end_et").cast(pl.Datetime("us", config.system.timezone)),
        pl.col("pulled_at_utc").cast(pl.Datetime("us", "UTC")),
        pl.col("session_date").cast(pl.Date),
        pl.col("delivery_month").cast(pl.Date),
        pl.col("bar_slot").cast(pl.Int16),
        pl.col("volume_contracts").cast(pl.Float64),
        pl.col("num_events").cast(pl.Float64),
        pl.col("value").cast(pl.Float64),
    ).select(BAR_COLUMNS).sort(["timestamp_utc", "security", "event_type"])
    daily = pl.DataFrame(daily_rows).with_columns(
        pl.col("session_date").cast(pl.Date),
        pl.col("delivery_month").cast(pl.Date),
    ).sort(["session_date", "root", "delivery_month"])

    latest_trade = bars.filter(pl.col("event_type") == "TRADE").sort("timestamp_utc")
    latest = latest_trade.group_by("security", maintain_order=True).tail(1)
    top_rows: list[dict[str, object]] = []
    depth_rows: list[dict[str, object]] = []
    for row in latest.to_dicts():
        root_spec = config.roots[str(row["root"])]
        tick = root_spec.tick_size_native
        top_rows.append(
            {
                "time": row["timestamp_utc"],
                "security": row["security"],
                "bid": float(row["close"]) - tick,
                "ask": float(row["close"]) + tick,
                "bid_size": 18.0,
                "ask_size": 16.0,
                "source": "DEMO_TOP_OF_BOOK",
            }
        )
        for level in range(1, 6):
            depth_rows.append(
                {
                    "time": row["timestamp_utc"],
                    "security": row["security"],
                    "level": level,
                    "bid": float(row["close"]) - tick * level,
                    "bid_size": float(22 - 2 * level),
                    "ask": float(row["close"]) + tick * level,
                    "ask_size": float(20 - 2 * level),
                    "source": "DEMO_L2",
                }
            )
    return bars, daily, registry, pl.DataFrame(top_rows), pl.DataFrame(depth_rows)


def write_demo(project_root: Path, as_of: date, seed: int = 73021) -> None:
    config = load_technical_config(project_root / "config" / "technical_system.toml")
    bars, daily, registry, top, depth = generate_demo_frames(
        config, as_of=as_of, seed=seed
    )
    store = TechnicalStore(DataPaths.under(project_root, dataset="demo"), config)
    store.replace_bars(bars)
    store.replace_daily(daily)
    store.write_contracts(registry)
    store.write_liquidity(top, l2=False)
    store.write_liquidity(depth, l2=True)
    print(
        f"Demo data written: {bars.height:,} bars, {daily.height:,} daily rows, "
        f"{registry.height:,} contracts through {as_of}."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--seed", type=int, default=73021)
    return parser


if __name__ == "__main__":
    args = _parser().parse_args()
    write_demo(args.project_root.resolve(), args.as_of, args.seed)
