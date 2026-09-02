#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.root_config import ConfigValidationError, load_root_config

DATE_CANDIDATES = ["date", "dt", "datetime"]
SECURITY_CANDIDATES = ["security_str", "security", "ticker", "symbol", "code", "security_id", "fut_cur_gen_ticker"]
PX_LAST_CANDIDATES = ["px_last", "px_last_st", "px_last_price"]
PX_CLOSE_CANDIDATES = ["px_close", "px_closing", "px_close_price"]
PX_SETTLE_CANDIDATES = ["px_settle", "px_settlement"]
PX_FAIR_CANDIDATES = [
    "px_fair_1430",
    "px_fair_value_1430",
    "px_1430_fair",
    "px_1430_fair_value",
    "px_fair_value",
    "fair_value_1430",
    "fair_value",
    "px_fv_1430",
    "fv_1430",
]
MONTH_CANDIDATES = ["month", "contract_month", "delivery_month", "contract_period"]
CONTRACT_MONTH_YR_CANDIDATES = [
    "current_contract_month_yr",
    "current_contract_month_year",
    "contract_month_yr",
    "contract_month_year",
    "contract_month",
    "contract_code",
    "fut_cur_gen_ticker",
]
CONTRACT_YEAR_CANDIDATES = ["contract_year", "contractyear", "contract_yr", "contract_year_num", "year"]
FREQUENCY_CANDIDATES = ["frequency", "freq"]
REFERENCE_CANDIDATES = ["reference", "ref", "ref_num"]
NAME_CANDIDATES = ["clean_name", "clean name", "cleanname", "name", "common_name", "security_name", "description"]
CODE_CANDIDATES = ["security_prefix", "security_code", "root_code", "code"]
VOLUME_CANDIDATES = ["px_volume", "volume", "vol", "qty", "quantity"]
VOL_30D_CANDIDATES = [
    "vol_30d",
    "vol30d",
    "volatility_30d",
    "hist_vol_30d",
    "hist_volatility_30d",
    "vol_30_day",
    "volatility_30_day",
]
BBL_PER_MT_CANDIDATES = ["bbl_per_mt", "bblpermt", "bbl_per_metric_ton"]
GAL_PER_BBL_CANDIDATES = ["gal_per_bbl", "galperbbl", "gallons_per_bbl"]

MONTH_LABELS = {
    "F": "Jan",
    "G": "Feb",
    "H": "Mar",
    "J": "Apr",
    "K": "May",
    "M": "Jun",
    "N": "Jul",
    "Q": "Aug",
    "U": "Sep",
    "V": "Oct",
    "X": "Nov",
    "Z": "Dec",
}


def pick(columns: list[str], candidates: list[str]) -> str | None:
    lower_map = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def ensure_date(df: pl.DataFrame, date_col: str) -> pl.DataFrame:
    dtype = df[date_col].dtype
    if dtype == pl.Date:
        return df
    if dtype == pl.Datetime:
        return df.with_columns(pl.col(date_col).cast(pl.Date))
    if dtype == pl.Utf8:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y%m%d"):
            parsed = df.with_columns(
                pl.col(date_col).str.strptime(pl.Date, fmt, strict=False).alias("__parsed_date")
            )
            if parsed["__parsed_date"].null_count() < len(parsed):
                return parsed.drop(date_col).rename({"__parsed_date": date_col})
    raise ValueError(f"Unsupported date column type for {date_col}: {dtype}")


def normalize_period(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    upper = text.upper()
    if upper[:3] in ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"):
        return upper[:1] + upper[1:3].lower()
    return text


def parse_contract_month(value: str | None) -> str | None:
    token = str(value or "").strip().split()[0].upper() if str(value or "").strip() else ""
    if not token:
        return None
    explicit = re.match(r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)", token)
    if explicit:
        return normalize_period(explicit.group(1))
    root_code = re.match(r"^[A-Z]{1,4}([FGHJKMNQUVXZ])\d{1,2}$", token)
    if root_code:
        return MONTH_LABELS.get(root_code.group(1))
    plain_code = re.match(r"^([FGHJKMNQUVXZ])\d{1,2}$", token)
    if plain_code:
        return MONTH_LABELS.get(plain_code.group(1))
    return None


def parse_contract_year(value: str | None) -> int | None:
    token = str(value or "").strip().split()[0] if str(value or "").strip() else ""
    if not token:
        return None
    match = re.search(r"(\d{1,4})$", token)
    if not match:
        return None
    raw = int(match.group(1))
    if raw < 100:
        raw += 2000 if raw < 70 else 1900
    return raw


def count_numeric_coercion_failures(df: pl.DataFrame, col: str) -> int:
    return df.filter(pl.col(col).is_not_null() & pl.col(col).cast(pl.Float64, strict=False).is_null()).height


def count_values_over_precision(df: pl.DataFrame, col: str, decimals: int) -> int:
    numeric = pl.col(col).cast(pl.Float64, strict=False)
    return df.filter(
        numeric.is_not_null()
        & numeric.is_finite()
        & (numeric != numeric.round(decimals))
    ).height


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pricing parquet/csv for dashboard + Bloomberg handoff.")
    parser.add_argument("path", help="Path to parquet/csv file.")
    parser.add_argument(
        "--config",
        default="config/security_roots.xlsx",
        help="Security-root XLSX/CSV used by the dashboard build.",
    )
    parser.add_argument(
        "--max-decimals",
        type=int,
        default=5,
        help="Maximum decimal places permitted in prices, volatility, and conversion factors.",
    )
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: file not found: {path}")
        return 2

    if path.suffix.lower() == ".csv":
        df = pl.read_csv(path, try_parse_dates=True)
    elif path.suffix.lower() in (".parquet", ".pq"):
        df = pl.read_parquet(path)
    else:
        print(f"ERROR: unsupported file type: {path.suffix}")
        return 2

    columns = df.columns
    resolved = {
        "date": pick(columns, DATE_CANDIDATES),
        "security_str": pick(columns, SECURITY_CANDIDATES),
        "px_last": pick(columns, PX_LAST_CANDIDATES),
        "px_close": pick(columns, PX_CLOSE_CANDIDATES),
        "px_settle": pick(columns, PX_SETTLE_CANDIDATES),
        "px_fair": pick(columns, PX_FAIR_CANDIDATES),
        "security_prefix": pick(columns, CODE_CANDIDATES),
        "clean_name": pick(columns, NAME_CANDIDATES),
        "month": pick(columns, MONTH_CANDIDATES),
        "contract_month_yr": pick(columns, CONTRACT_MONTH_YR_CANDIDATES),
        "contract_year": pick(columns, CONTRACT_YEAR_CANDIDATES),
        "frequency": pick(columns, FREQUENCY_CANDIDATES),
        "reference": pick(columns, REFERENCE_CANDIDATES),
        "volume": pick(columns, VOLUME_CANDIDATES),
        "vol_30d": pick(columns, VOL_30D_CANDIDATES),
        "bbl_per_mt": pick(columns, BBL_PER_MT_CANDIDATES),
        "gal_per_bbl": pick(columns, GAL_PER_BBL_CANDIDATES),
    }

    print("Resolved columns:")
    for key, value in resolved.items():
        print(f"  {key:18s}: {value or '--'}")

    required = ["date", "security_str", "security_prefix", "px_last"]
    missing_required = [key for key in required if not resolved[key]]
    if missing_required:
        print("\nERROR: Missing required columns:")
        for key in missing_required:
            print(f"  - {key}")
        print("\nFix names or pass explicit column flags to the export script.")
        return 1

    date_col = resolved["date"]
    security_col = resolved["security_str"]
    px_last_col = resolved["px_last"]

    try:
        df = ensure_date(df, date_col)
    except ValueError as exc:
        print(f"\nERROR: {exc}")
        return 1

    issues: list[str] = []
    warnings: list[str] = []

    if df.height == 0:
        issues.append("DataFrame has zero rows.")
    else:
        min_date = df[date_col].min()
        max_date = df[date_col].max()
        print(f"\nData summary: rows={df.height} columns={len(df.columns)} date_range={min_date}..{max_date}")

    today = date.today()
    future_rows = df.filter(pl.col(date_col) > today).height
    if future_rows:
        issues.append(f"{future_rows} rows have dates after today ({today}).")

    duplicate_keys = [date_col, security_col]
    reference_col = resolved["reference"]
    if reference_col:
        duplicate_keys.append(reference_col)
    duplicate_count = (
        df.group_by(duplicate_keys)
        .len()
        .filter(pl.col("len") > 1)
        .height
    )
    if duplicate_count:
        issues.append(f"{duplicate_count} duplicate (date, security, reference) keys detected.")

    if reference_col:
        bad_ref = df.filter(pl.col(reference_col).cast(pl.Int64, strict=False).is_null() | (pl.col(reference_col) < 1)).height
        if bad_ref:
            issues.append(f"{bad_ref} rows have invalid reference (<1 or non-numeric).")

    for price_key in ("px_last", "px_close", "px_settle", "px_fair"):
        col = resolved[price_key]
        if not col:
            continue
        coercion_failures = count_numeric_coercion_failures(df, col)
        if coercion_failures:
            issues.append(f"{coercion_failures} rows in {col} are non-numeric.")
        null_count = df.filter(pl.col(col).is_null()).height
        if null_count and price_key == "px_last":
            issues.append(f"{col} has {null_count} null rows (PX_LAST is required).")
        negative_count = df.filter(pl.col(col).cast(pl.Float64, strict=False) <= 0).height
        if negative_count and price_key == "px_last":
            warnings.append(f"{col} has {negative_count} non-positive values.")
        excessive_precision = count_values_over_precision(df, col, args.max_decimals)
        if excessive_precision:
            issues.append(
                f"{excessive_precision} rows in {col} exceed {args.max_decimals} decimal places."
            )

    volume_col = resolved["volume"]
    if volume_col:
        vol_bad = count_numeric_coercion_failures(df, volume_col)
        if vol_bad:
            issues.append(f"{vol_bad} rows in {volume_col} are non-numeric.")
        negative_volume = df.filter(pl.col(volume_col).cast(pl.Float64, strict=False) < 0).height
        if negative_volume:
            issues.append(f"{negative_volume} rows in {volume_col} are negative.")

    vol30_col = resolved["vol_30d"]
    if vol30_col:
        vol_bad = count_numeric_coercion_failures(df, vol30_col)
        if vol_bad:
            issues.append(f"{vol_bad} rows in {vol30_col} are non-numeric.")
        out_of_range = df.filter(
            pl.col(vol30_col).is_not_null()
            & ((pl.col(vol30_col).cast(pl.Float64, strict=False) < 0) | (pl.col(vol30_col).cast(pl.Float64, strict=False) > 3))
        ).height
        if out_of_range:
            warnings.append(f"{out_of_range} rows in {vol30_col} fall outside [0, 3].")
        excessive_precision = count_values_over_precision(df, vol30_col, args.max_decimals)
        if excessive_precision:
            issues.append(
                f"{excessive_precision} rows in {vol30_col} exceed {args.max_decimals} decimal places."
            )

    month_col = resolved["month"]
    contract_month_yr_col = resolved["contract_month_yr"]
    if month_col and contract_month_yr_col:
        check = (
            df.with_columns([
                pl.col(month_col).map_elements(lambda v: normalize_period(v), return_dtype=pl.Utf8).alias("__m_norm"),
                pl.col(contract_month_yr_col).map_elements(parse_contract_month, return_dtype=pl.Utf8).alias("__m_from_contract"),
            ])
            .filter(pl.col("__m_from_contract").is_not_null() & (pl.col("__m_norm") != pl.col("__m_from_contract")))
            .height
        )
        if check:
            issues.append(f"{check} rows have month mismatch between {month_col} and {contract_month_yr_col}.")

    contract_year_col = resolved["contract_year"]
    if contract_year_col and contract_month_yr_col:
        year_mismatch = (
            df.with_columns([
                pl.col(contract_year_col).cast(pl.Int64, strict=False).alias("__cy"),
                pl.col(contract_month_yr_col).map_elements(parse_contract_year, return_dtype=pl.Int64).alias("__cy_from_contract"),
            ])
            .filter(
                pl.col("__cy").is_not_null()
                & pl.col("__cy_from_contract").is_not_null()
                & (pl.col("__cy") != pl.col("__cy_from_contract"))
            )
            .height
        )
        if year_mismatch:
            issues.append(f"{year_mismatch} rows have year mismatch between {contract_year_col} and {contract_month_yr_col}.")

    bbl_col = resolved["bbl_per_mt"]
    if bbl_col:
        bad = df.filter(pl.col(bbl_col).cast(pl.Float64, strict=False).is_null() | (pl.col(bbl_col) <= 0)).height
        if bad:
            issues.append(f"{bad} rows have invalid {bbl_col} (must be > 0).")
        excessive_precision = count_values_over_precision(df, bbl_col, args.max_decimals)
        if excessive_precision:
            issues.append(
                f"{excessive_precision} rows in {bbl_col} exceed {args.max_decimals} decimal places."
            )

    gal_col = resolved["gal_per_bbl"]
    if gal_col:
        bad = df.filter(pl.col(gal_col).cast(pl.Float64, strict=False).is_null() | (pl.col(gal_col) <= 0)).height
        if bad:
            issues.append(f"{bad} rows have invalid {gal_col} (must be > 0).")
        excessive_precision = count_values_over_precision(df, gal_col, args.max_decimals)
        if excessive_precision:
            issues.append(
                f"{excessive_precision} rows in {gal_col} exceed {args.max_decimals} decimal places."
            )

    root_col = resolved["security_prefix"]
    if root_col:
        for factor_col in (bbl_col, gal_col):
            if not factor_col:
                continue
            inconsistent = (
                df.group_by(root_col)
                .agg(pl.col(factor_col).drop_nulls().n_unique().alias("__factor_count"))
                .filter(pl.col("__factor_count") > 1)
            )
            if inconsistent.height:
                roots_text = ", ".join(map(str, inconsistent[root_col].to_list()))
                issues.append(f"{factor_col} is not constant within roots: {roots_text}.")

    log_return_frame = (
        df.filter(pl.col(px_last_col).cast(pl.Float64, strict=False) > 0)
        .sort([security_col, date_col])
        .with_columns(
            pl.col(px_last_col)
            .cast(pl.Float64, strict=False)
            .log()
            .diff()
            .over([security_col] + ([reference_col] if reference_col else []))
            .alias("__ret")
        )
    )
    extreme_return_rows = log_return_frame.filter(pl.col("__ret").abs() > 0.50).height
    if extreme_return_rows:
        warnings.append(f"{extreme_return_rows} rows have |log return| > 50%; inspect for bad ticks.")

    roots = []
    if resolved["security_prefix"]:
        roots = sorted(df[resolved["security_prefix"]].drop_nulls().unique().to_list())
    if roots:
        print(f"Root coverage: {', '.join(map(str, roots))}")

    try:
        root_config = load_root_config(args.config)
    except ConfigValidationError as exc:
        issues.extend(exc.issues)
        root_config = None

    if root_config is not None and roots:
        data_roots = {str(root).strip().upper() for root in roots}
        configured_roots = set(root_config.enabled_root_codes)
        missing_config = sorted(data_roots - configured_roots)
        if missing_config:
            issues.append(
                "data roots are missing or disabled in the security-root workbook: "
                + ", ".join(missing_config)
            )
        missing_data = sorted(configured_roots - data_roots)
        if missing_data:
            issues.append(
                "enabled workbook roots have no rows in this data file: " + ", ".join(missing_data)
            )

        curve_columns = {
            "frequency": resolved["frequency"],
            "reference": resolved["reference"],
            "month": resolved["month"],
            "contract_month_yr": resolved["contract_month_yr"],
            "contract_year": resolved["contract_year"],
        }
        missing_curve_columns = [name for name, column in curve_columns.items() if not column]
        if missing_curve_columns:
            issues.append(
                "configured curve-mode checks require columns: "
                + ", ".join(missing_curve_columns)
            )

        if root_col:
            for root in sorted(data_roots & configured_roots):
                configured = root_config.by_root[root]
                root_df = df.filter(pl.col(root_col).cast(pl.Utf8).str.to_uppercase() == root)
                for factor_col, expected in (
                    (bbl_col, configured.bbl_per_mt),
                    (gal_col, configured.gal_per_bbl),
                ):
                    if not factor_col:
                        continue
                    values = root_df[factor_col].drop_nulls().unique().to_list()
                    if values and abs(float(values[0]) - float(expected)) > 1e-7:
                        issues.append(
                            f"{root} {factor_col}={values[0]} does not match workbook value {expected}."
                        )

                expected_suffix = f" {configured.yellow_key}".lower()
                suffix_mismatches = root_df.filter(
                    ~pl.col(security_col)
                    .cast(pl.Utf8)
                    .str.to_lowercase()
                    .str.ends_with(expected_suffix)
                ).height
                if suffix_mismatches:
                    issues.append(
                        f"{suffix_mismatches} rows for {root} do not use workbook yellow_key "
                        f"{configured.yellow_key}."
                    )

                root_mismatches = root_df.filter(
                    ~pl.col(security_col)
                    .cast(pl.Utf8)
                    .str.to_uppercase()
                    .str.starts_with(root)
                ).height
                if root_mismatches:
                    issues.append(
                        f"{root_mismatches} rows use security_prefix {root} but their ticker "
                        f"does not start with {root}."
                    )

                if not missing_curve_columns:
                    frequency_col = curve_columns["frequency"]
                    frequency = (
                        pl.col(frequency_col)
                        .cast(pl.Utf8, strict=False)
                        .str.strip_chars()
                        .str.to_lowercase()
                    )
                    month_col = curve_columns["month"]
                    contract_col = curve_columns["contract_month_yr"]
                    contract_year_col = curve_columns["contract_year"]
                    curve_reference_col = curve_columns["reference"]
                    if configured.curve_mode == "flat":
                        ticker_count = root_df[security_col].drop_nulls().n_unique()
                        if ticker_count != 1 or root_df[security_col].null_count():
                            issues.append(
                                f"{root} flat curve must contain exactly one non-null security; "
                                f"found {ticker_count}."
                            )
                        bad_frequency = root_df.filter(
                            pl.col(frequency_col).is_null() | (frequency != "flat")
                        ).height
                        if bad_frequency:
                            issues.append(
                                f"{bad_frequency} rows for flat root {root} do not use frequency Flat."
                            )
                        bad_month = root_df.filter(
                            pl.col(month_col).is_not_null()
                            & (pl.col(month_col).cast(pl.Utf8, strict=False).str.strip_chars() != "")
                        ).height
                        if bad_month:
                            issues.append(f"{bad_month} rows for flat root {root} have a month.")
                        bad_contract = root_df.filter(
                            pl.col(contract_col).is_not_null()
                            & (pl.col(contract_col).cast(pl.Utf8, strict=False).str.strip_chars() != "")
                        ).height
                        if bad_contract:
                            issues.append(
                                f"{bad_contract} rows for flat root {root} have contract_month_yr."
                            )
                        bad_contract_year = root_df.filter(
                            pl.col(contract_year_col).is_not_null()
                        ).height
                        if bad_contract_year:
                            issues.append(
                                f"{bad_contract_year} rows for flat root {root} have contract_year."
                            )
                        bad_reference = root_df.filter(
                            pl.col(curve_reference_col).cast(pl.Int64, strict=False).is_null()
                            | (pl.col(curve_reference_col).cast(pl.Int64, strict=False) != 1)
                        ).height
                        if bad_reference:
                            issues.append(
                                f"{bad_reference} rows for flat root {root} do not use reference 1."
                            )
                    else:
                        bad_frequency = root_df.filter(
                            pl.col(frequency_col).is_null() | (frequency != "monthly")
                        ).height
                        if bad_frequency:
                            issues.append(
                                f"{bad_frequency} rows for dated root {root} do not use frequency Monthly."
                            )
                        bad_month = root_df.filter(
                            pl.col(month_col).is_null()
                            | (pl.col(month_col).cast(pl.Utf8, strict=False).str.strip_chars() == "")
                        ).height
                        if bad_month:
                            issues.append(
                                f"{bad_month} rows for dated root {root} are missing month metadata."
                            )
                        bad_contract = root_df.filter(
                            pl.col(contract_col).is_null()
                            | (pl.col(contract_col).cast(pl.Utf8, strict=False).str.strip_chars() == "")
                        ).height
                        if bad_contract:
                            issues.append(
                                f"{bad_contract} rows for dated root {root} are missing contract_month_yr metadata."
                            )
                        bad_contract_year = root_df.filter(
                            pl.col(contract_year_col).cast(pl.Int64, strict=False).is_null()
                        ).height
                        if bad_contract_year:
                            issues.append(
                                f"{bad_contract_year} rows for dated root {root} are missing contract_year metadata."
                            )

    if warnings:
        print("\nWarnings:")
        for msg in warnings:
            print(f"  - {msg}")

    if issues:
        print("\nERRORS:")
        for msg in issues:
            print(f"  - {msg}")
        return 1

    print("\nOK: data passed schema, integrity, and pricing sanity checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
