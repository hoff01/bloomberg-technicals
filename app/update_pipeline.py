"""Transactional Bloomberg-to-CSV-to-portable-dashboard update pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Protocol, Sequence

import polars as pl

from app.export_single_file import SUPPORTED_PRICE_FIELDS, export_dashboard
from app.root_config import RootConfig, SecurityRoot, load_root_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "security_roots.xlsx"
DEFAULT_CSV = PROJECT_ROOT / "data" / "pricing_history.csv"
DEFAULT_CSV_GZIP = PROJECT_ROOT / "dist" / "pricing_data.csv.gz"
DEFAULT_PARQUET = PROJECT_ROOT / "dist" / "pricing_data.parquet"
DEFAULT_HTML = PROJECT_ROOT / "dist" / "pricing_dashboard_trade_builder.html"
DEFAULT_EMBEDDED_JS = PROJECT_ROOT / "app" / "static" / "embedded_data.js"
DEFAULT_MANIFEST = PROJECT_ROOT / "dist" / "update_manifest.json"
PRECISION = 5
CSV_ACCESS_ATTEMPTS = 6
CSV_ACCESS_RETRY_SECONDS = 1.0
ARTIFACT_PROMOTION_ATTEMPTS = 30
ARTIFACT_PROMOTION_RETRY_SECONDS = 2.0

MONTH_CODES = {
    1: ("F", "Jan"),
    2: ("G", "Feb"),
    3: ("H", "Mar"),
    4: ("J", "Apr"),
    5: ("K", "May"),
    6: ("M", "Jun"),
    7: ("N", "Jul"),
    8: ("Q", "Aug"),
    9: ("U", "Sep"),
    10: ("V", "Oct"),
    11: ("X", "Nov"),
    12: ("Z", "Dec"),
}

METADATA_COLUMNS = (
    "date",
    "security_str",
    "FUT_CUR_GEN_TICKER",
    "security_prefix",
    "CLEAN_NAME",
    "frequency",
    "reference",
    "month",
    "contract_month_yr",
    "contract_year",
)
TRAILING_COLUMNS = (
    "year",
    "bbl_per_mt",
    "gal_per_bbl",
    "native_unit",
    "yellow_key",
)


class UpdateError(RuntimeError):
    """Raised when an update cannot be published safely."""


@dataclass(frozen=True)
class ContractSpec:
    ticker: str
    root: str
    display_name: str
    yellow_key: str
    native_unit: str
    bbl_per_mt: float
    gal_per_bbl: float
    curve_mode: str
    month_number: int
    month_code: str
    month_label: str
    contract_year: int | None
    contract_month_yr: str
    start_date: date
    end_date: date
    default_price_field: str
    time_zone_override: int | None


@dataclass(frozen=True)
class UpdatePaths:
    project_root: Path = PROJECT_ROOT
    config: Path = DEFAULT_CONFIG
    csv: Path = DEFAULT_CSV
    csv_gzip: Path = DEFAULT_CSV_GZIP
    parquet: Path = DEFAULT_PARQUET
    html: Path = DEFAULT_HTML
    embedded_js: Path = DEFAULT_EMBEDDED_JS
    manifest: Path = DEFAULT_MANIFEST

    @classmethod
    def under(cls, root: str | Path, config: str | Path) -> "UpdatePaths":
        base = Path(root).resolve()
        return cls(
            project_root=base,
            config=Path(config).resolve(),
            csv=base / "data" / "pricing_history.csv",
            csv_gzip=base / "dist" / "pricing_data.csv.gz",
            parquet=base / "dist" / "pricing_data.parquet",
            html=base / "dist" / "pricing_dashboard_trade_builder.html",
            embedded_js=base / "app" / "static" / "embedded_data.js",
            manifest=base / "dist" / "update_manifest.json",
        )


class HistoricalClient(Protocol):
    def fetch(
        self,
        requests: Sequence[Any],
        fields: Sequence[str],
        *,
        batch_size: int,
        timeout_seconds: int,
    ) -> Any: ...


def _canonical_columns(fields: Sequence[str]) -> tuple[str, ...]:
    return METADATA_COLUMNS + tuple(fields) + TRAILING_COLUMNS


def _canonical_schema(fields: Sequence[str]) -> dict[str, pl.DataType]:
    schema: dict[str, pl.DataType] = {
        "date": pl.Date,
        "security_str": pl.Utf8,
        "FUT_CUR_GEN_TICKER": pl.Utf8,
        "security_prefix": pl.Utf8,
        "CLEAN_NAME": pl.Utf8,
        "frequency": pl.Utf8,
        "reference": pl.Int64,
        "month": pl.Utf8,
        "contract_month_yr": pl.Utf8,
        "contract_year": pl.Int64,
    }
    schema.update({field: pl.Float64 for field in fields})
    schema.update(
        {
            "year": pl.Int64,
            "bbl_per_mt": pl.Float64,
            "gal_per_bbl": pl.Float64,
            "native_unit": pl.Utf8,
            "yellow_key": pl.Utf8,
        }
    )
    return schema


def _empty_frame(fields: Sequence[str]) -> pl.DataFrame:
    return pl.DataFrame(schema=_canonical_schema(fields))


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - int(months)
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


ABBREVIATED_YEAR_TOKEN_WIDTHS = {
    "{y}": 1,
    "{year_1d}": 1,
    "{year_digit}": 1,
    "{yy}": 2,
    "{year_2d}": 2,
}


def _ticker_for(
    root: SecurityRoot,
    month_code: str,
    contract_year: int,
    *,
    extra_year_digits: int = 0,
) -> str:
    year_text = str(contract_year)

    def abbreviated_year(default_width: int) -> str:
        width = min(len(year_text), max(2, default_width + max(0, extra_year_digits)))
        return year_text[-width:]

    values = {
        "root": root.root,
        "month_code": month_code,
        "y": abbreviated_year(1),
        "year_1d": abbreviated_year(1),
        "year_digit": abbreviated_year(1),
        "yy": abbreviated_year(2),
        "year_2d": abbreviated_year(2),
        "year": contract_year,
        "yyyy": contract_year,
        "yellow_key": root.yellow_key,
    }
    try:
        ticker = root.ticker_template.format(**values)
    except (KeyError, ValueError) as exc:
        raise UpdateError(f"Cannot render ticker_template for {root.root}: {exc}") from exc
    return " ".join(ticker.split())


def _monthly_tickers_for_year_range(
    root: SecurityRoot,
    contract_years: Sequence[int],
) -> dict[tuple[int, str], str]:
    """Render all monthly tickers, expanding every member of a collision group."""

    keys = [
        (contract_year, month_code)
        for contract_year in contract_years
        for month_code, _month_label in MONTH_CODES.values()
    ]
    abbreviated_widths = [
        width
        for token, width in ABBREVIATED_YEAR_TOKEN_WIDTHS.items()
        if token in root.ticker_template
    ]
    minimum_width = min(abbreviated_widths, default=4)
    max_extra_digits = max(0, 4 - minimum_width)
    extra_digits = {key: 0 for key in keys}

    while True:
        tickers = {
            key: _ticker_for(
                root,
                key[1],
                key[0],
                extra_year_digits=extra_digits[key],
            )
            for key in keys
        }
        by_ticker: dict[str, list[tuple[int, str]]] = {}
        for key, ticker in tickers.items():
            by_ticker.setdefault(ticker.casefold(), []).append(key)
        collisions = [group for group in by_ticker.values() if len(group) > 1]
        if not collisions:
            return tickers

        progressed = False
        for group in collisions:
            for key in group:
                if extra_digits[key] < max_extra_digits:
                    extra_digits[key] += 1
                    progressed = True
        if not progressed:
            first_key = collisions[0][0]
            raise UpdateError(
                "Ticker template generated a duplicate security: "
                + tickers[first_key]
            )


def build_contract_universe(config: RootConfig, as_of: date | None = None) -> tuple[ContractSpec, ...]:
    """Expand roots into dated contracts or one monthless flat-curve security."""

    current_date = as_of or date.today()
    settings = config.update
    specs: list[ContractSpec] = []
    seen: set[str] = set()
    for root in sorted(config.enabled_roots, key=lambda item: (item.sort_order, item.root)):
        if root.curve_mode == "flat":
            ticker = _ticker_for(root, "", current_date.year)
            normalized = ticker.casefold()
            if normalized in seen:
                raise UpdateError(f"Ticker template generated a duplicate security: {ticker}")
            seen.add(normalized)
            specs.append(
                ContractSpec(
                    ticker=ticker,
                    root=root.root,
                    display_name=root.common_name,
                    yellow_key=root.yellow_key,
                    native_unit=root.native_unit,
                    bbl_per_mt=root.bbl_per_mt,
                    gal_per_bbl=root.gal_per_bbl,
                    curve_mode="flat",
                    month_number=0,
                    month_code="",
                    month_label="",
                    contract_year=None,
                    contract_month_yr="",
                    start_date=settings.history_start,
                    end_date=current_date,
                    default_price_field=root.default_price_field,
                    time_zone_override=root.time_zone_override,
                )
            )
            continue
        contract_years = tuple(
            range(settings.contract_start_year, settings.contract_end_year + 1)
        )
        tickers = _monthly_tickers_for_year_range(root, contract_years)
        for contract_year in contract_years:
            for month_number, (month_code, month_label) in MONTH_CODES.items():
                delivery_start = date(contract_year, month_number, 1)
                start_date = max(
                    settings.history_start,
                    _subtract_months(delivery_start, settings.contract_history_months),
                )
                end_date = min(current_date, delivery_start - timedelta(days=1))
                ticker = tickers[(contract_year, month_code)]
                normalized = ticker.casefold()
                if normalized in seen:
                    raise UpdateError(
                        f"Ticker template generated a duplicate security: {ticker}"
                    )
                seen.add(normalized)
                specs.append(
                    ContractSpec(
                        ticker=ticker,
                        root=root.root,
                        display_name=root.common_name,
                        yellow_key=root.yellow_key,
                        native_unit=root.native_unit,
                        bbl_per_mt=root.bbl_per_mt,
                        gal_per_bbl=root.gal_per_bbl,
                        curve_mode="monthly",
                        month_number=month_number,
                        month_code=month_code,
                        month_label=month_label,
                        contract_year=contract_year,
                        contract_month_yr=f"{month_code}{contract_year % 100:02d}",
                        start_date=start_date,
                        end_date=end_date,
                        default_price_field=root.default_price_field,
                        time_zone_override=root.time_zone_override,
                    )
                )
    return tuple(specs)


def _parse_observation_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _finite_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return round(parsed, PRECISION) if math.isfinite(parsed) else None


def _reference_for(observation_date: date, spec: ContractSpec) -> int:
    if spec.curve_mode == "flat":
        return 1
    nearest_delivery_year = observation_date.year + (
        1 if observation_date.month >= spec.month_number else 0
    )
    return spec.contract_year - nearest_delivery_year + 1


def normalize_bloomberg_rows(
    rows: Iterable[dict[str, object]],
    specs: Sequence[ContractSpec],
    fields: Sequence[str],
    reference_depth: int,
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    """Convert Bloomberg response rows into the stable shareable CSV contract."""

    by_ticker = {spec.ticker.casefold(): spec for spec in specs}
    normalized_rows: list[dict[str, object]] = []
    skipped_unknown: set[str] = set()
    skipped_reference = 0
    skipped_missing_last = 0
    for raw in rows:
        security = str(raw.get("security") or raw.get("security_str") or "").strip()
        spec = by_ticker.get(security.casefold())
        if spec is None:
            if security:
                skipped_unknown.add(security)
            continue
        observation_date = _parse_observation_date(raw.get("date"))
        if observation_date is None or observation_date < spec.start_date or observation_date > spec.end_date:
            continue
        reference = _reference_for(observation_date, spec)
        if reference < 1 or reference > reference_depth:
            skipped_reference += 1
            continue
        values = {field: _finite_float(raw.get(field)) for field in fields}
        if spec.default_price_field:
            preferred_value = _finite_float(raw.get(spec.default_price_field))
            if preferred_value is not None:
                values["PX_LAST"] = preferred_value
        if values.get("PX_LAST") is None:
            skipped_missing_last += 1
            continue
        record: dict[str, object] = {
            "date": observation_date,
            "security_str": spec.ticker,
            "FUT_CUR_GEN_TICKER": spec.ticker,
            "security_prefix": spec.root,
            "CLEAN_NAME": spec.display_name,
            "frequency": "Flat" if spec.curve_mode == "flat" else "Monthly",
            "reference": reference,
            "month": spec.month_label,
            "contract_month_yr": spec.contract_month_yr,
            "contract_year": spec.contract_year,
            **values,
            "year": observation_date.year,
            "bbl_per_mt": round(spec.bbl_per_mt, PRECISION),
            "gal_per_bbl": round(spec.gal_per_bbl, PRECISION),
            "native_unit": spec.native_unit,
            "yellow_key": spec.yellow_key,
        }
        normalized_rows.append(record)

    warnings: list[str] = []
    if skipped_unknown:
        sample = ", ".join(sorted(skipped_unknown)[:5])
        warnings.append(f"Ignored {len(skipped_unknown)} unrequested Bloomberg securities: {sample}")
    if skipped_reference:
        warnings.append(
            f"Ignored {skipped_reference} observations outside reference depth {reference_depth}."
        )
    if skipped_missing_last:
        warnings.append(f"Ignored {skipped_missing_last} observations without PX_LAST.")

    if not normalized_rows:
        return _empty_frame(fields), tuple(warnings)
    frame = pl.DataFrame(
        normalized_rows,
        schema=_canonical_schema(fields),
        strict=False,
    )
    frame = frame.unique(subset=["date", "security_str"], keep="last", maintain_order=True)
    return _round_frame(frame, fields), tuple(warnings)


def _spec_metadata_frame(specs: Sequence[ContractSpec]) -> pl.DataFrame:
    frame = pl.DataFrame(
        [
            {
                "security_str": spec.ticker,
                "FUT_CUR_GEN_TICKER": spec.ticker,
                "security_prefix": spec.root,
                "CLEAN_NAME": spec.display_name,
                "frequency": "Flat" if spec.curve_mode == "flat" else "Monthly",
                "month": spec.month_label,
                "contract_month_yr": spec.contract_month_yr,
                "contract_year": spec.contract_year,
                "bbl_per_mt": spec.bbl_per_mt,
                "gal_per_bbl": spec.gal_per_bbl,
                "native_unit": spec.native_unit,
                "yellow_key": spec.yellow_key,
                "_curve_mode": spec.curve_mode,
                "_delivery_month": spec.month_number,
                "_start_date": spec.start_date,
                "_end_date": spec.end_date,
            }
            for spec in specs
        ]
    )
    return frame.with_columns(
        pl.col("contract_year").cast(pl.Int64, strict=False),
        pl.col("_delivery_month").cast(pl.Int64, strict=False),
    )


def _normalize_existing_frame(
    path: Path,
    specs: Sequence[ContractSpec],
    fields: Sequence[str],
    reference_depth: int,
) -> pl.DataFrame:
    if not path.exists() or not path.stat().st_size:
        return _empty_frame(fields)
    for attempt in range(1, CSV_ACCESS_ATTEMPTS + 1):
        try:
            source = pl.read_csv(
                io.BytesIO(path.read_bytes()),
                try_parse_dates=True,
                infer_schema_length=10_000,
            )
            break
        except Exception as exc:
            message = str(exc).lower()
            access_error = (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
                or any(
                    marker in message
                    for marker in (
                        "access is denied",
                        "permission denied",
                        "os error 5",
                        "os error 32",
                        "os error 33",
                    )
                )
            )
            if not access_error or attempt == CSV_ACCESS_ATTEMPTS:
                raise UpdateError(f"The existing CSV cannot be read: {path}: {exc}") from exc
            time.sleep(CSV_ACCESS_RETRY_SECONDS)
    if "date" not in source.columns or "security_str" not in source.columns:
        raise UpdateError("The existing CSV is missing date or security_str.")
    if source["date"].dtype == pl.Datetime:
        source = source.with_columns(pl.col("date").cast(pl.Date))
    elif source["date"].dtype != pl.Date:
        source = source.with_columns(
            pl.col("date").cast(pl.Utf8).str.to_date("%Y-%m-%d", strict=False)
        )
    for field in fields:
        if field not in source.columns:
            source = source.with_columns(pl.lit(None, dtype=pl.Float64).alias(field))
    source = source.select(
        ["date", "security_str"]
        + [pl.col(field).cast(pl.Float64, strict=False).alias(field) for field in fields]
    )
    source = source.join(_spec_metadata_frame(specs), on="security_str", how="inner")
    nearest_delivery_year = (
        pl.col("date").dt.year()
        + (pl.col("date").dt.month() >= pl.col("_delivery_month")).cast(pl.Int64)
    )
    source = source.with_columns(
        pl.when(pl.col("_curve_mode") == "flat")
        .then(pl.lit(1))
        .otherwise(pl.col("contract_year") - nearest_delivery_year + 1)
        .alias("reference"),
        pl.col("date").dt.year().alias("year"),
    ).filter(
        pl.col("date").is_not_null()
        & (pl.col("date") >= pl.col("_start_date"))
        & (pl.col("date") <= pl.col("_end_date"))
        & pl.col("reference").is_between(1, reference_depth)
        & pl.col("PX_LAST").is_not_null()
    )
    source = source.select(_canonical_columns(fields))
    source = source.unique(subset=["date", "security_str"], keep="last", maintain_order=True)
    return _round_frame(source, fields)


def _round_frame(frame: pl.DataFrame, fields: Sequence[str]) -> pl.DataFrame:
    float_columns = [
        column
        for column in (*fields, "bbl_per_mt", "gal_per_bbl")
        if column in frame.columns
    ]
    if float_columns:
        frame = frame.with_columns(
            [pl.col(column).cast(pl.Float64, strict=False).round(PRECISION) for column in float_columns]
        )
    return frame.select(_canonical_columns(fields))


def _build_requests(
    specs: Sequence[ContractSpec],
    existing: pl.DataFrame,
    overlap_days: int,
    as_of: date,
    full: bool,
) -> list[Any]:
    from app.bloomberg_client import HistoricalRequest

    last_by_ticker: dict[str, date] = {}
    if not full and existing.height:
        last_by_ticker = {
            str(security): last_date
            for security, last_date in existing.group_by("security_str")
            .agg(pl.col("date").max().alias("last_date"))
            .iter_rows()
        }

    requests: list[HistoricalRequest] = []
    for spec in specs:
        if spec.start_date > spec.end_date:
            continue
        last_date = last_by_ticker.get(spec.ticker)
        if last_date and spec.end_date < as_of:
            continue
        if last_date and last_date >= spec.end_date:
            continue
        start_date = spec.start_date
        if last_date:
            start_date = max(start_date, last_date - timedelta(days=overlap_days))
        extra_fields = (
            (spec.default_price_field,) if spec.default_price_field else ()
        )
        overrides = (
            (("TIME_ZONE_OVERRIDE", str(spec.time_zone_override)),)
            if spec.time_zone_override is not None
            else ()
        )
        requests.append(
            HistoricalRequest(
                spec.ticker,
                start_date,
                spec.end_date,
                extra_fields=extra_fields,
                overrides=overrides,
            )
        )
    return requests


def _merge_frames(existing: pl.DataFrame, pulled: pl.DataFrame, fields: Sequence[str]) -> pl.DataFrame:
    frames = [frame for frame in (existing, pulled) if frame.height]
    if not frames:
        return _empty_frame(fields)
    combined = pl.concat(frames, how="vertical_relaxed")
    combined = combined.unique(
        subset=["date", "security_str"], keep="last", maintain_order=True
    )
    return _round_frame(
        combined.sort(["security_prefix", "security_str", "date"]), fields
    )


def validate_canonical_frame(
    frame: pl.DataFrame,
    config: RootConfig,
    specs: Sequence[ContractSpec],
    as_of: date | None = None,
) -> None:
    issues: list[str] = []
    required = set(_canonical_columns(config.update.fields))
    missing = sorted(required - set(frame.columns))
    if missing:
        issues.append("missing canonical columns: " + ", ".join(missing))
    if not frame.height:
        issues.append("Bloomberg returned no usable rows.")
    if issues:
        raise UpdateError("Update data validation failed:\n- " + "\n- ".join(issues))

    validation_date = as_of or date.today()
    if frame.filter(pl.col("date") > validation_date).height:
        issues.append("rows contain future observation dates")
    duplicate_count = (
        frame.group_by(["date", "security_str"])
        .len()
        .filter(pl.col("len") > 1)
        .height
    )
    if duplicate_count:
        issues.append(f"{duplicate_count} duplicate (date, security_str) keys")
    if frame.filter(pl.col("PX_LAST").is_null() | ~pl.col("PX_LAST").is_finite()).height:
        issues.append("PX_LAST contains null or non-finite values")

    expected_roots = set(config.enabled_root_codes)
    actual_roots = set(frame["security_prefix"].unique().to_list())
    missing_roots = sorted(expected_roots - actual_roots)
    extra_roots = sorted(actual_roots - expected_roots)
    if missing_roots:
        issues.append("enabled roots have no usable Bloomberg rows: " + ", ".join(missing_roots))
    if extra_roots:
        issues.append("unconfigured roots reached the canonical CSV: " + ", ".join(extra_roots))

    universe = {spec.ticker for spec in specs}
    unexpected = frame.filter(~pl.col("security_str").is_in(list(universe))).height
    if unexpected:
        issues.append(f"{unexpected} rows have securities outside the spreadsheet ticker universe")
    bad_reference = frame.filter(
        ~pl.col("reference").is_between(1, config.update.reference_depth)
    ).height
    if bad_reference:
        issues.append(f"{bad_reference} rows have an invalid dated-contract reference")

    for root in config.enabled_roots:
        root_frame = frame.filter(pl.col("security_prefix") == root.root)
        if not root_frame.height:
            continue
        frequency = (
            pl.col("frequency")
            .cast(pl.Utf8, strict=False)
            .str.strip_chars()
            .str.to_lowercase()
        )
        if root.curve_mode == "flat":
            ticker_count = root_frame["security_str"].drop_nulls().n_unique()
            if ticker_count != 1 or root_frame["security_str"].null_count():
                issues.append(
                    f"{root.root} flat curve must contain exactly one non-null security; "
                    f"found {ticker_count}"
                )
            bad_frequency = root_frame.filter(
                pl.col("frequency").is_null() | (frequency != "flat")
            ).height
            if bad_frequency:
                issues.append(f"{bad_frequency} {root.root} flat rows must use frequency Flat")
            bad_month = root_frame.filter(
                pl.col("month").is_not_null()
                & (pl.col("month").cast(pl.Utf8, strict=False).str.strip_chars() != "")
            ).height
            if bad_month:
                issues.append(f"{bad_month} {root.root} flat rows must have a blank month")
            bad_contract = root_frame.filter(
                pl.col("contract_month_yr").is_not_null()
                & (
                    pl.col("contract_month_yr")
                    .cast(pl.Utf8, strict=False)
                    .str.strip_chars()
                    != ""
                )
            ).height
            if bad_contract:
                issues.append(
                    f"{bad_contract} {root.root} flat rows must have a blank contract_month_yr"
                )
            bad_contract_year = root_frame.filter(pl.col("contract_year").is_not_null()).height
            if bad_contract_year:
                issues.append(
                    f"{bad_contract_year} {root.root} flat rows must have a null contract_year"
                )
            bad_flat_reference = root_frame.filter(
                pl.col("reference").is_null() | (pl.col("reference") != 1)
            ).height
            if bad_flat_reference:
                issues.append(
                    f"{bad_flat_reference} {root.root} flat rows must use reference 1"
                )
        else:
            bad_frequency = root_frame.filter(
                pl.col("frequency").is_null() | (frequency != "monthly")
            ).height
            if bad_frequency:
                issues.append(
                    f"{bad_frequency} {root.root} dated rows must use frequency Monthly"
                )
            bad_month = root_frame.filter(
                pl.col("month").is_null()
                | (pl.col("month").cast(pl.Utf8, strict=False).str.strip_chars() == "")
            ).height
            if bad_month:
                issues.append(f"{bad_month} {root.root} dated rows are missing month metadata")
            bad_contract = root_frame.filter(
                pl.col("contract_month_yr").is_null()
                | (
                    pl.col("contract_month_yr")
                    .cast(pl.Utf8, strict=False)
                    .str.strip_chars()
                    == ""
                )
            ).height
            if bad_contract:
                issues.append(
                    f"{bad_contract} {root.root} dated rows are missing contract_month_yr metadata"
                )
            bad_contract_year = root_frame.filter(pl.col("contract_year").is_null()).height
            if bad_contract_year:
                issues.append(
                    f"{bad_contract_year} {root.root} dated rows are missing contract_year metadata"
                )

    precision_columns = list(config.update.fields) + ["bbl_per_mt", "gal_per_bbl"]
    for column in precision_columns:
        numeric = pl.col(column).cast(pl.Float64, strict=False)
        excessive = frame.filter(
            numeric.is_not_null()
            & numeric.is_finite()
            & (numeric != numeric.round(PRECISION))
        ).height
        if excessive:
            issues.append(f"{excessive} rows in {column} exceed {PRECISION} decimals")
    if issues:
        raise UpdateError("Update data validation failed:\n- " + "\n- ".join(issues))


def _run_validator(csv_path: Path, config_path: Path) -> str:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "validate_pricing_data.py"),
        str(csv_path),
        "--config",
        str(config_path),
        "--max-decimals",
        str(PRECISION),
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stdout + "\n" + completed.stderr).strip()
        raise UpdateError(f"Staged CSV failed the repository validator:\n{detail}")
    return completed.stdout.strip()


def _write_csv_gzip(source: Path, output: Path) -> None:
    with source.open("rb") as input_handle, output.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_output,
            mtime=0,
        ) as gzip_output:
            shutil.copyfileobj(input_handle, gzip_output, length=1024 * 1024)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pull_contract_sha256(config: RootConfig) -> str:
    settings = config.update
    contract = {
        "schema_version": 1,
        "settings": {
            "history_start": settings.history_start.isoformat(),
            "contract_start_year": settings.contract_start_year,
            "contract_end_year": settings.contract_end_year,
            "contract_history_months": settings.contract_history_months,
            "reference_depth": settings.reference_depth,
            "fields": list(settings.fields),
            "service": settings.service,
        },
        "roots": [
            {
                "root": root.root,
                "yellow_key": root.yellow_key,
                "ticker_template": root.ticker_template,
                "curve_mode": root.curve_mode,
                "default_price_field": root.default_price_field,
                "time_zone_override": root.time_zone_override,
            }
            for root in sorted(config.enabled_roots, key=lambda item: item.root)
        ],
    }
    serialized = json.dumps(
        contract,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _automatic_full_refresh_reason(
    paths: UpdatePaths,
    config: RootConfig,
    pull_contract_sha256: str,
) -> str:
    if not paths.csv.exists():
        return "Pricing history is missing"
    if not paths.manifest.exists():
        return "Update manifest is missing"
    try:
        prior_manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "Update manifest could not be read"

    prior_contract_sha256 = str(
        prior_manifest.get("pull_contract_sha256") or ""
    ).strip()
    if prior_contract_sha256:
        if prior_contract_sha256 != pull_contract_sha256:
            return "Bloomberg pull configuration changed"
        return ""

    prior_config_sha256 = str(prior_manifest.get("config_sha256") or "").strip()
    if prior_config_sha256 and prior_config_sha256 != _sha256(config.source_path):
        return "Bloomberg pull configuration changed"
    return ""


def _git_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        return completed.stdout.strip()
    except Exception:
        return "unknown"


def _relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _root_stats(frame: pl.DataFrame) -> dict[str, dict[str, object]]:
    stats = (
        frame.group_by("security_prefix")
        .agg(
            pl.len().alias("rows"),
            pl.col("security_str").n_unique().alias("securities"),
            pl.col("date").min().alias("min_date"),
            pl.col("date").max().alias("max_date"),
        )
        .sort("security_prefix")
    )
    result: dict[str, dict[str, object]] = {}
    for row in stats.iter_rows(named=True):
        result[str(row["security_prefix"])] = {
            "rows": int(row["rows"]),
            "securities": int(row["securities"]),
            "min_date": row["min_date"].isoformat(),
            "max_date": row["max_date"].isoformat(),
        }
    return result


def _verify_parquet_parity(
    csv_path: Path, parquet_path: Path, fields: Sequence[str]
) -> None:
    csv_frame = pl.read_csv(csv_path, try_parse_dates=True, infer_schema_length=10_000)
    parquet = pl.read_parquet(parquet_path)
    if parquet.height != csv_frame.height:
        raise UpdateError(
            f"CSV/Parquet row-count mismatch: {csv_frame.height} CSV rows vs {parquet.height} Parquet rows."
        )
    projection = [
        pl.col("date").cast(pl.Date).alias("date"),
        pl.col("security_str").cast(pl.Utf8).alias("security_str"),
        pl.col("reference").cast(pl.Int64).alias("reference"),
        *[
            pl.col(field).cast(pl.Float64, strict=False).round(PRECISION).alias(field)
            for field in fields
        ],
    ]
    left = csv_frame.select(projection).sort(["date", "security_str", "reference"])
    right = parquet.select(projection).sort(["date", "security_str", "reference"])
    if not left.equals(right, null_equal=True):
        raise UpdateError("CSV and compact Parquet keys/prices are not identical.")


def _is_transient_windows_access_error(exc: OSError) -> bool:
    message = str(exc).lower()
    return (
        isinstance(exc, PermissionError)
        or getattr(exc, "winerror", None) in {5, 32, 33}
        or any(
            marker in message
            for marker in (
                "access is denied",
                "permission denied",
                "os error 5",
                "os error 32",
                "os error 33",
            )
        )
    )


def _promote_artifacts(staged_to_target: Sequence[tuple[Path, Path]], backup_root: Path) -> None:
    backup_root.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path | None] = {}
    promoted: list[Path] = []
    for index, (_staged, target) in enumerate(staged_to_target):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = backup_root / f"{index:02d}-{target.name}"
            shutil.copy2(target, backup)
            backups[target] = backup
        else:
            backups[target] = None
    try:
        for staged, target in staged_to_target:
            for attempt in range(1, ARTIFACT_PROMOTION_ATTEMPTS + 1):
                try:
                    os.replace(staged, target)
                    break
                except OSError as exc:
                    if (
                        not _is_transient_windows_access_error(exc)
                        or attempt == ARTIFACT_PROMOTION_ATTEMPTS
                    ):
                        raise
                    time.sleep(ARTIFACT_PROMOTION_RETRY_SECONDS)
            promoted.append(target)
    except Exception as exc:
        for target in reversed(promoted):
            backup = backups[target]
            if backup is None:
                target.unlink(missing_ok=True)
            else:
                os.replace(backup, target)
        raise UpdateError(f"Artifact promotion failed and was rolled back: {exc}") from exc


def run_bloomberg_update(
    *,
    paths: UpdatePaths | None = None,
    client: HistoricalClient | None = None,
    as_of: date | None = None,
    full: bool = False,
    run_repository_validator: bool = True,
) -> dict[str, Any]:
    """Pull Bloomberg data and publish one internally consistent export package."""

    output_paths = paths or UpdatePaths()
    output_paths.project_root.mkdir(parents=True, exist_ok=True)
    current_date = as_of or date.today()
    requested_at = datetime.now(timezone.utc).replace(microsecond=0)
    config = load_root_config(output_paths.config)
    pull_contract_sha256 = _pull_contract_sha256(config)
    full_refresh_reason = (
        "Full refresh requested"
        if full
        else _automatic_full_refresh_reason(
            output_paths,
            config,
            pull_contract_sha256,
        )
    )
    effective_full = full or bool(full_refresh_reason)
    specs = build_contract_universe(config, current_date)
    existing = (
        _empty_frame(config.update.fields)
        if effective_full
        else _normalize_existing_frame(
            output_paths.csv,
            specs,
            config.update.fields,
            config.update.reference_depth,
        )
    )
    requests = _build_requests(
        specs,
        existing,
        config.update.overlap_days,
        current_date,
        effective_full,
    )

    pull_rows: Iterable[dict[str, object]] = ()
    pull_warnings: list[str] = []
    if requests:
        if client is None:
            from app.bloomberg_client import BloombergClient

            client = BloombergClient(
                host=config.update.host,
                port=config.update.port,
                service=config.update.service,
            )
        result = client.fetch(
            requests,
            config.update.fields,
            batch_size=config.update.batch_size,
            timeout_seconds=config.update.request_timeout_seconds,
        )
        pull_rows = result.rows
        pull_warnings.extend(getattr(result, "warnings", ()))

    pulled, normalize_warnings = normalize_bloomberg_rows(
        pull_rows,
        specs,
        config.update.fields,
        config.update.reference_depth,
    )
    pull_warnings.extend(normalize_warnings)
    combined = _merge_frames(existing, pulled, config.update.fields)
    validate_canonical_frame(combined, config, specs, current_date)

    staging_parent = output_paths.project_root
    with tempfile.TemporaryDirectory(prefix=".pricing-update-", dir=staging_parent) as directory:
        staging = Path(directory)
        staged_csv = staging / "pricing_history.csv"
        staged_csv_gzip = staging / "pricing_data.csv.gz"
        staged_parquet = staging / "pricing_data.parquet"
        staged_html = staging / "pricing_dashboard_trade_builder.html"
        staged_embedded_js = staging / "embedded_data.js"
        staged_manifest = staging / "update_manifest.json"

        combined.write_csv(staged_csv, float_precision=PRECISION)
        if run_repository_validator:
            _run_validator(staged_csv, output_paths.config)

        completed_at = datetime.now(timezone.utc).replace(microsecond=0)
        price_fields = [
            field for field in config.update.fields if field in SUPPORTED_PRICE_FIELDS
        ]
        dashboard_fields = list(config.update.dashboard_fields)
        export_summary = export_dashboard(
            data_path=str(staged_csv),
            root_config_path=str(output_paths.config),
            output=str(staged_html),
            embedded_js_output=str(staged_embedded_js),
            compact_parquet_output=str(staged_parquet),
            fields=dashboard_fields,
            parquet_fields=price_fields,
            precision=PRECISION,
            max_output_mb=config.update.standalone_max_mb,
            include_analytics=any(
                field in config.update.fields for field in ("PX_VOLUME", "VOL_30D")
            ),
            built_at=completed_at.isoformat().replace("+00:00", "Z"),
        )
        _verify_parquet_parity(staged_csv, staged_parquet, price_fields)
        _write_csv_gzip(staged_csv, staged_csv_gzip)

        artifact_pairs = (
            (staged_csv, output_paths.csv),
            (staged_csv_gzip, output_paths.csv_gzip),
            (staged_parquet, output_paths.parquet),
            (staged_embedded_js, output_paths.embedded_js),
            (staged_html, output_paths.html),
        )
        artifact_manifest = {
            _relative_display(target, output_paths.project_root): {
                "bytes": staged.stat().st_size,
                "sha256": _sha256(staged),
            }
            for staged, target in artifact_pairs
        }
        manifest = {
            "schema_version": 1,
            "status": "complete",
            "source": "Bloomberg Desktop API",
            "requested_at": requested_at.isoformat().replace("+00:00", "Z"),
            "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
            "mode": "full" if effective_full else "incremental",
            "full_refresh_reason": full_refresh_reason,
            "pull_contract_sha256": pull_contract_sha256,
            "precision": PRECISION,
            "config": _relative_display(output_paths.config, output_paths.project_root),
            "config_sha256": _sha256(output_paths.config),
            "git_revision": _git_revision(),
            "requested_fields": list(config.update.fields),
            "dashboard_fields": dashboard_fields,
            "requested_securities": len(requests),
            "universe_securities": len(specs),
            "received_rows": pulled.height,
            "retained_rows": combined.height,
            "data_min_date": combined["date"].min().isoformat(),
            "data_max_date": combined["date"].max().isoformat(),
            "root_coverage": _root_stats(combined),
            "curve_modes": {
                root.root: root.curve_mode
                for root in sorted(config.enabled_roots, key=lambda item: item.root)
            },
            "warnings": list(dict.fromkeys(str(item) for item in pull_warnings)),
            "export": {
                "rows": export_summary["rows"],
                "roots": export_summary["roots"],
                "fields": export_summary["fields"],
                "parquet_fields": export_summary["parquet_fields"],
                "standalone_mb": export_summary["output_mb"],
            },
            "artifacts": artifact_manifest,
        }
        staged_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _promote_artifacts(
            (*artifact_pairs, (staged_manifest, output_paths.manifest)),
            staging / "backups",
        )

    return {
        "ok": True,
        "success": True,
        "message": (
            f"Bloomberg {'full' if effective_full else 'incremental'} update complete: "
            f"{combined.height:,} rows through "
            f"{combined['date'].max().isoformat()}. Reloading…"
        ),
        "mode": "full" if effective_full else "incremental",
        "full_refresh_reason": full_refresh_reason,
        "manifest": _relative_display(output_paths.manifest, output_paths.project_root),
        "rows": combined.height,
        "data_max_date": combined["date"].max().isoformat(),
        "warnings": list(dict.fromkeys(str(item) for item in pull_warnings)),
    }
