#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}


@dataclass(frozen=True)
class MarketSpec:
    clean_name: str
    exchange: str
    yellow_key: str
    native_unit: str
    bbl_per_mt: float
    carry_per_month: float
    base_volume: int
    price_noise: float
    volume_season_amp: float


MARKETS: dict[str, MarketSpec] = {
    "CL": MarketSpec(
        clean_name="WTI Crude Oil",
        exchange="NYMEX",
        yellow_key="Comdty",
        native_unit="$/bbl",
        bbl_per_mt=7.33,
        carry_per_month=0.08,
        base_volume=220_000,
        price_noise=0.22,
        volume_season_amp=0.08,
    ),
    "CO": MarketSpec(
        clean_name="Brent Crude Oil",
        exchange="ICE",
        yellow_key="Comdty",
        native_unit="$/bbl",
        bbl_per_mt=7.33,
        carry_per_month=0.06,
        base_volume=145_000,
        price_noise=0.24,
        volume_season_amp=0.08,
    ),
    "HO": MarketSpec(
        clean_name="ULSD Heating Oil",
        exchange="NYMEX",
        yellow_key="Comdty",
        native_unit="cpg",
        bbl_per_mt=7.45,
        carry_per_month=0.11,
        base_volume=76_000,
        price_noise=0.28,
        volume_season_amp=0.14,
    ),
    "XB": MarketSpec(
        clean_name="RBOB Gasoline",
        exchange="NYMEX",
        yellow_key="Comdty",
        native_unit="cpg",
        bbl_per_mt=8.33,
        carry_per_month=0.09,
        base_volume=71_000,
        price_noise=0.26,
        volume_season_amp=0.16,
    ),
    "QS": MarketSpec(
        clean_name="ICE Low Sulphur Gasoil",
        exchange="ICE",
        yellow_key="Comdty",
        native_unit="$/MT",
        bbl_per_mt=7.45,
        carry_per_month=0.07,
        base_volume=58_000,
        price_noise=0.27,
        volume_season_amp=0.13,
    ),
    "WU": MarketSpec(
        clean_name="GC Jet",
        exchange="NYMEX",
        yellow_key="Comdty",
        native_unit="cpg",
        bbl_per_mt=7.45,
        carry_per_month=0.10,
        base_volume=34_000,
        price_noise=0.25,
        volume_season_amp=0.12,
    ),
}


def _daterange(start: date, end: date) -> list[date]:
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _season_value(month: int, phase: float = 0.0) -> float:
    return math.sin((2.0 * math.pi * (month - 1)) / 12.0 + phase)


def _delivery_adjustment(root: str, month: int) -> float:
    if root == "XB":
        return 4.2 * _season_value(month, -math.pi / 2)
    if root in ("HO", "QS", "WU"):
        return 3.8 * _season_value(month, math.pi / 2)
    if root in ("CL", "CO"):
        return 1.1 * _season_value(month, -0.2)
    return 0.0


def _build_spot_curves(all_dates: list[date], rng: np.random.Generator) -> dict[str, np.ndarray]:
    n = len(all_dates)
    month_arr = np.array([d.month for d in all_dates], dtype=float)
    seasonal = np.sin((2.0 * np.pi * (month_arr - 1.0)) / 12.0)

    cl = np.empty(n, dtype=float)
    cl[0] = 72.0
    for idx in range(1, n):
        target = 71.0 + 5.3 * seasonal[idx]
        cl[idx] = max(35.0, cl[idx - 1] + 0.05 * (target - cl[idx - 1]) + rng.normal(0, 0.95))

    brent_spread = np.empty(n, dtype=float)
    brent_spread[0] = 3.1
    for idx in range(1, n):
        target = 3.0 + 0.7 * seasonal[idx]
        brent_spread[idx] = brent_spread[idx - 1] + 0.18 * (target - brent_spread[idx - 1]) + rng.normal(0, 0.18)
    co = np.maximum(cl + brent_spread, 38.0)

    ho_crack = np.empty(n, dtype=float)
    ho_crack[0] = 23.0
    for idx in range(1, n):
        target = 23.5 + 4.8 * math.cos((2.0 * math.pi * (month_arr[idx] - 1.0)) / 12.0)
        ho_crack[idx] = ho_crack[idx - 1] + 0.2 * (target - ho_crack[idx - 1]) + rng.normal(0, 0.6)
    ho = np.maximum(cl + ho_crack, 45.0)

    rb_crack = np.empty(n, dtype=float)
    rb_crack[0] = 18.0
    for idx in range(1, n):
        target = 18.5 + 5.4 * _season_value(int(month_arr[idx]), -math.pi / 2)
        rb_crack[idx] = rb_crack[idx - 1] + 0.22 * (target - rb_crack[idx - 1]) + rng.normal(0, 0.68)
    rb = np.maximum(cl + rb_crack, 42.0)

    qs_crack = np.empty(n, dtype=float)
    qs_crack[0] = 19.5
    for idx in range(1, n):
        target = 20.0 + 3.9 * math.cos((2.0 * math.pi * (month_arr[idx] - 1.0)) / 12.0)
        qs_crack[idx] = qs_crack[idx - 1] + 0.2 * (target - qs_crack[idx - 1]) + rng.normal(0, 0.62)
    qs = np.maximum(co + qs_crack, 44.0)

    jet_crack = np.empty(n, dtype=float)
    jet_crack[0] = 24.0
    for idx in range(1, n):
        target = 24.0 + 3.4 * math.cos((2.0 * math.pi * (month_arr[idx] - 1.0)) / 12.0)
        jet_crack[idx] = jet_crack[idx - 1] + 0.2 * (target - jet_crack[idx - 1]) + rng.normal(0, 0.55)
    wu = np.maximum(cl + jet_crack, 46.0)

    return {"CL": cl, "CO": co, "HO": ho, "XB": rb, "QS": qs, "WU": wu}


def _build_flat_rvo_curve(all_dates: list[date], seed: int) -> np.ndarray:
    """Create one deterministic daily cpg series used for every forward month."""

    rng = np.random.default_rng(seed + 17)
    values = np.empty(len(all_dates), dtype=float)
    values[0] = 18.0
    for idx in range(1, len(all_dates)):
        current = all_dates[idx]
        seasonal = 1.8 * _season_value(current.month, -0.4)
        target = 17.5 + seasonal
        values[idx] = max(
            1.0,
            values[idx - 1] + 0.08 * (target - values[idx - 1]) + rng.normal(0, 0.32),
        )
    return values


def _from_usd_per_bbl(value: float, spec: MarketSpec) -> float:
    """Convert the generated economic curve into each Bloomberg root's native quote unit."""
    if spec.native_unit == "$/bbl":
        return value
    if spec.native_unit == "$/gal":
        return value / 42.0
    if spec.native_unit == "cpg":
        return value / 42.0 * 100.0
    if spec.native_unit == "$/MT":
        return value * spec.bbl_per_mt
    raise ValueError(f"Unsupported native unit for {spec.clean_name}: {spec.native_unit}")


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - int(months)
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def _collision_safe_year_suffixes(contract_years: list[int]) -> dict[int, str]:
    extra_digits = {year: 0 for year in contract_years}
    while True:
        suffixes = {
            year: str(year)[-(1 + extra_digits[year]):]
            for year in contract_years
        }
        by_suffix: dict[str, list[int]] = {}
        for year, suffix in suffixes.items():
            by_suffix.setdefault(suffix, []).append(year)
        collisions = [group for group in by_suffix.values() if len(group) > 1]
        if not collisions:
            return suffixes
        for group in collisions:
            for year in group:
                extra_digits[year] += 1


def generate_dataset(
    start_contract_year: int,
    end_contract_year: int,
    seed: int,
    *,
    history_start: date = date(2015, 1, 1),
    history_months: int = 36,
) -> pl.DataFrame:
    if end_contract_year < start_contract_year:
        raise ValueError("end_contract_year must be >= start_contract_year")
    if history_months < 1:
        raise ValueError("history_months must be positive")

    start_date = history_start
    end_date = date(end_contract_year, 12, 31)
    all_dates = _daterange(start_date, end_date)
    date_to_idx = {d: idx for idx, d in enumerate(all_dates)}
    rng = np.random.default_rng(seed)

    spot_curves = _build_spot_curves(all_dates, rng)
    flat_rvo = _build_flat_rvo_curve(all_dates, seed)
    columns = {
        "date": [],
        "security_str": [],
        "FUT_CUR_GEN_TICKER": [],
        "security_prefix": [],
        "CLEAN_NAME": [],
        "exchange": [],
        "frequency": [],
        "reference": [],
        "month": [],
        "contract_month_yr": [],
        "contract_year": [],
        "PX_LAST": [],
        "PX_CLOSE": [],
        "PX_SETTLE": [],
        "PX_FAIR_1430": [],
        "PX_VOLUME": [],
        "year": [],
        "bbl_per_mt": [],
        "gal_per_bbl": [],
    }

    reference_depth = 3
    contract_years = list(range(start_contract_year, end_contract_year + 1))
    year_suffixes = _collision_safe_year_suffixes(contract_years)
    for root, spec in MARKETS.items():
        spot = spot_curves[root]
        for contract_year in contract_years:
            for month_idx, month_label in enumerate(MONTHS, start=1):
                month_code = MONTH_CODES[month_idx]
                month_yr = f"{month_code}{str(contract_year)[-2:]}"
                ticker = f"{root}{month_code}{year_suffixes[contract_year]} {spec.yellow_key}"
                delivery_start = date(contract_year, month_idx, 1)
                contract_start = max(
                    history_start,
                    _subtract_months(delivery_start, history_months),
                )
                contract_end = delivery_start - timedelta(days=1)
                contract_dates = _daterange(contract_start, contract_end)

                for current_date in contract_dates:
                    nearest_delivery_year = current_date.year + (
                        1 if current_date.month >= month_idx else 0
                    )
                    reference = contract_year - nearest_delivery_year + 1
                    if reference < 1 or reference > reference_depth:
                        continue
                    idx = date_to_idx[current_date]
                    months_to_delivery = (contract_year - current_date.year) * 12 + (month_idx - current_date.month)
                    tenor_months = max(0, months_to_delivery)
                    volume_phase = 0.6 if root in ("HO", "QS", "WU") else -0.5 if root == "XB" else 0.0
                    volume_season = 1.0 + spec.volume_season_amp * _season_value(current_date.month, volume_phase)
                    liquidity = math.exp(-0.12 * tenor_months)
                    delivery_adj = _delivery_adjustment(root, month_idx)
                    curve_px = spot[idx] + delivery_adj + spec.carry_per_month * tenor_months
                    settle_bbl = max(1.0, curve_px + rng.normal(0, spec.price_noise))
                    close_bbl = max(1.0, settle_bbl + rng.normal(0, spec.price_noise * 0.55))
                    last_bbl = max(1.0, close_bbl + rng.normal(0, spec.price_noise * 0.50))
                    fair_1430_bbl = max(1.0, settle_bbl + rng.normal(0, spec.price_noise * 0.40))
                    settle = _from_usd_per_bbl(settle_bbl, spec)
                    close = _from_usd_per_bbl(close_bbl, spec)
                    last = _from_usd_per_bbl(last_bbl, spec)
                    fair_1430 = _from_usd_per_bbl(fair_1430_bbl, spec)
                    volume = int(max(50, round(spec.base_volume * liquidity * volume_season * rng.lognormal(0.0, 0.15))))

                    columns["date"].append(current_date)
                    columns["security_str"].append(ticker)
                    columns["FUT_CUR_GEN_TICKER"].append(ticker)
                    columns["security_prefix"].append(root)
                    columns["CLEAN_NAME"].append(spec.clean_name)
                    columns["exchange"].append(spec.exchange)
                    columns["frequency"].append("Monthly")
                    columns["reference"].append(reference)
                    columns["month"].append(month_label)
                    columns["contract_month_yr"].append(month_yr)
                    columns["contract_year"].append(contract_year)
                    columns["PX_LAST"].append(float(round(last, 5)))
                    columns["PX_CLOSE"].append(float(round(close, 5)))
                    columns["PX_SETTLE"].append(float(round(settle, 5)))
                    columns["PX_FAIR_1430"].append(float(round(fair_1430, 5)))
                    columns["PX_VOLUME"].append(volume)
                    columns["year"].append(current_date.year)
                    columns["bbl_per_mt"].append(spec.bbl_per_mt)
                    columns["gal_per_bbl"].append(42)

    # RVO is an undated daily index, not a futures strip. Keep one observation
    # per date in the source; browser JavaScript aligns that same daily value to
    # whichever dated curve month is used by the other trade legs.
    rvo_rng = np.random.default_rng(seed + 29)
    for current_date in all_dates:
        idx = date_to_idx[current_date]
        settle = max(0.01, flat_rvo[idx] + rvo_rng.normal(0, 0.08))
        close = max(0.01, settle + rvo_rng.normal(0, 0.05))
        last = max(0.01, close + rvo_rng.normal(0, 0.04))
        fair_1430 = max(0.01, settle + rvo_rng.normal(0, 0.04))
        columns["date"].append(current_date)
        columns["security_str"].append("NAUG008A Index")
        columns["FUT_CUR_GEN_TICKER"].append("NAUG008A Index")
        columns["security_prefix"].append("NAUG008A")
        columns["CLEAN_NAME"].append("RVO")
        columns["exchange"].append("Bloomberg")
        columns["frequency"].append("Flat")
        columns["reference"].append(1)
        columns["month"].append("")
        columns["contract_month_yr"].append("")
        columns["contract_year"].append(None)
        columns["PX_LAST"].append(float(round(last, 5)))
        columns["PX_CLOSE"].append(float(round(close, 5)))
        columns["PX_SETTLE"].append(float(round(settle, 5)))
        columns["PX_FAIR_1430"].append(float(round(fair_1430, 5)))
        columns["PX_VOLUME"].append(None)
        columns["year"].append(current_date.year)
        columns["bbl_per_mt"].append(7.45)
        columns["gal_per_bbl"].append(42)

    df = pl.DataFrame(columns)
    df = df.sort(["security_prefix", "security_str", "reference", "date"])
    today = date.today()
    df = df.filter(pl.col("date") <= today)

    sqrt_252 = math.sqrt(252.0)
    df = (
        df.with_columns(
            pl.col("PX_SETTLE")
            .log()
            .diff()
            .over(["security_str", "reference"])
            .alias("_log_ret")
        )
        .with_columns(
            (pl.col("_log_ret").rolling_std(window_size=30, min_samples=20).over(["security_str", "reference"]) * sqrt_252)
            .alias("VOL_30D")
        )
        .with_columns(
            pl.col("VOL_30D")
            .fill_null(strategy="forward")
            .over(["security_str", "reference"])
            .fill_null(0.26)
            .clip(0.05, 1.5)
            .round(5)
            .alias("VOL_30D")
        )
        .drop("_log_ret")
    )
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic NYMEX/ICE mock Bloomberg pricing data.")
    parser.add_argument("--start-contract-year", type=int, default=2018, help="First contract year to generate.")
    parser.add_argument("--end-contract-year", type=int, default=2028, help="Last contract year to generate.")
    parser.add_argument("--history-start", default="2015-01-01", help="Earliest retained observation date (YYYY-MM-DD).")
    parser.add_argument("--history-months", type=int, default=36, help="Months retained before each delivery month.")
    parser.add_argument("--seed", type=int, default=20260215, help="Random seed for deterministic output.")
    parser.add_argument(
        "--csv-output",
        default="",
        help="Optional CSV output path. Parquet is the canonical compact format.",
    )
    parser.add_argument(
        "--parquet-output",
        default="data/sample_market_data.parquet",
        help="Output parquet path.",
    )
    args = parser.parse_args()

    df = generate_dataset(
        args.start_contract_year,
        args.end_contract_year,
        args.seed,
        history_start=date.fromisoformat(args.history_start),
        history_months=args.history_months,
    )
    parquet_path = Path(args.parquet_output)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    df.write_parquet(parquet_path, compression="zstd", compression_level=9, statistics=True)
    if args.csv_output:
        csv_path = Path(args.csv_output)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        df.with_columns(pl.col("date").cast(pl.Date)).write_csv(csv_path)

    print(f"rows={df.height} columns={len(df.columns)}")
    print(f"contract_year_range={int(df['contract_year'].min())}-{int(df['contract_year'].max())}")
    print(f"date_range={df['date'].min()}->{df['date'].max()}")
    print(f"history_months={args.history_months}")
    print(f"roots={','.join(sorted(df['security_prefix'].unique().to_list()))}")
    print(f"wrote_parquet={parquet_path}")
    if args.csv_output:
        print(f"wrote_csv={Path(args.csv_output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
