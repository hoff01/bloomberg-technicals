import argparse
import base64
from dataclasses import asdict, is_dataclass
import gzip
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
import re
import time

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / "app" / "static"
DEFAULT_TEMPLATE = str(STATIC_ROOT / "index.html")
DEFAULT_JS = str(STATIC_ROOT / "app.js")
DEFAULT_TRADE_MATH = str(STATIC_ROOT / "trade_math.js")
DEFAULT_THEME = str(STATIC_ROOT / "theme.js")
DEFAULT_OUTPUT = str(PROJECT_ROOT / "dist" / "pricing_dashboard_trade_builder.html")
DEFAULT_PLOTLY = str(STATIC_ROOT / "plotly-3.3.1.min.js")
DEFAULT_ROOT_CONFIG = str(PROJECT_ROOT / "config" / "security_roots.xlsx")
DEFAULT_PRECISION = 5
SUPPORTED_PRICE_FIELDS = ("PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430")
DATA_ACCESS_ATTEMPTS = 5
DATA_ACCESS_RETRY_SECONDS = 0.5

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
    "fv_1430"
]
VOL_30D_CANDIDATES = [
    "vol_30d",
    "vol30d",
    "volatility_30d",
    "hist_vol_30d",
    "hist_volatility_30d",
    "vol_30_day",
    "volatility_30_day"
]
VALUE_CANDIDATES = (
    PX_LAST_CANDIDATES
    + PX_CLOSE_CANDIDATES
    + PX_SETTLE_CANDIDATES
    + PX_FAIR_CANDIDATES
    + ["value"]
)
VOLUME_CANDIDATES = ["px_volume", "volume", "vol", "qty", "quantity"]
NAME_CANDIDATES = ["clean_name", "clean name", "cleanname", "name", "common_name", "security_name", "description"]
CODE_CANDIDATES = ["security_prefix", "security_code", "root_code", "code"]
MONTH_CANDIDATES = ["month", "contract_month", "delivery_month", "contract_period"]
FREQUENCY_CANDIDATES = ["frequency", "freq"]
REFERENCE_CANDIDATES = ["reference", "ref", "ref_num"]
CONTRACT_MONTH_YR_CANDIDATES = [
    "current_contract_month_yr",
    "current_contract_month_year",
    "contract_month_yr",
    "contract_month_year",
    "contract_month",
    "contract_code",
    "fut_cur_gen_ticker"
]
CONTRACT_YEAR_CANDIDATES = ["contract_year", "contractYear", "contract_yr", "contract_year_num", "year"]
BBL_PER_MT_CANDIDATES = ["bbl_per_mt", "bblpermt", "bbl_per_metric_ton"]
GAL_PER_BBL_CANDIDATES = ["gal_per_bbl", "galperbbl", "gallons_per_bbl"]


def _round_payload_floats(value, precision=DEFAULT_PRECISION):
    """Round every finite payload float and replace non-finite values with null."""
    precision = max(0, min(DEFAULT_PRECISION, int(precision)))
    if isinstance(value, float):
        return round(value, precision) if math.isfinite(value) else None
    if isinstance(value, list):
        return [_round_payload_floats(item, precision) for item in value]
    if isinstance(value, tuple):
        return [_round_payload_floats(item, precision) for item in value]
    if isinstance(value, dict):
        return {key: _round_payload_floats(item, precision) for key, item in value.items()}
    return value


def _round_dataframe_floats(df: pl.DataFrame, precision=DEFAULT_PRECISION) -> pl.DataFrame:
    precision = max(0, min(DEFAULT_PRECISION, int(precision)))
    float_columns = [
        name for name, dtype in df.schema.items()
        if dtype in (pl.Float32, pl.Float64)
    ]
    if not float_columns:
        return df
    return df.with_columns([pl.col(name).round(precision).alias(name) for name in float_columns])


def _pick_column(columns, candidates):
    lower_map = {col.lower(): col for col in columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


KEY_DELIM = "::"
MONTH_LABELS = {
    "JAN": "Jan",
    "FEB": "Feb",
    "MAR": "Mar",
    "APR": "Apr",
    "MAY": "May",
    "JUN": "Jun",
    "JUL": "Jul",
    "AUG": "Aug",
    "SEP": "Sep",
    "OCT": "Oct",
    "NOV": "Nov",
    "DEC": "Dec"
}
QUARTER_LABELS = {
    "Q1": "Q1",
    "Q2": "Q2",
    "Q3": "Q3",
    "Q4": "Q4",
    "QUARTER 1": "Q1",
    "QUARTER 2": "Q2",
    "QUARTER 3": "Q3",
    "QUARTER 4": "Q4",
    "QUARTER1": "Q1",
    "QUARTER2": "Q2",
    "QUARTER3": "Q3",
    "QUARTER4": "Q4"
}
HALF_LABELS = {
    "H1": "Half 1",
    "H2": "Half 2",
    "1H": "Half 1",
    "2H": "Half 2",
    "S1": "Half 1",
    "S2": "Half 2",
    "HALF 1": "Half 1",
    "HALF 2": "Half 2",
    "HALF1": "Half 1",
    "HALF2": "Half 2"
}
QUARTER_MONTHS = {
    "Q1": ("Jan", "Feb", "Mar"),
    "Q2": ("Apr", "May", "Jun"),
    "Q3": ("Jul", "Aug", "Sep"),
    "Q4": ("Oct", "Nov", "Dec")
}
HALF_MONTHS = {
    "Half 1": ("Jan", "Feb", "Mar", "Apr", "May", "Jun"),
    "Half 2": ("Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
}
MONTH_TO_QUARTER = {month: quarter for quarter, months in QUARTER_MONTHS.items() for month in months}
MONTH_TO_HALF = {month: half for half, months in HALF_MONTHS.items() for month in months}
MONTH_INDEX = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12
}
MONTH_CODE_TO_LABEL = {
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
ROLL_MONTH_BY_PERIOD = {
    "Q1": "Jan",
    "Q2": "Apr",
    "Q3": "Jul",
    "Q4": "Oct",
    "Half 1": "Jan",
    "Half 2": "Jul"
}


def _roll_month_index(label: str) -> int | None:
    if not label:
        return None
    normalized = _normalize_period(label)
    if normalized in MONTH_INDEX:
        return MONTH_INDEX[normalized]
    roll_label = ROLL_MONTH_BY_PERIOD.get(normalized)
    return MONTH_INDEX.get(roll_label) if roll_label else None


def _plot_year_expr(date_col: str, reference_col: str, month_label_col: str) -> pl.Expr:
    roll_expr = pl.col(month_label_col).map_elements(_roll_month_index, return_dtype=pl.Int64)
    return (
        pl.col(date_col).dt.year()
        + (pl.col(reference_col) - 1)
        + pl.when(roll_expr.is_not_null() & (pl.col(date_col).dt.month() >= roll_expr)).then(1).otherwise(0)
    )


def _canonical_day_of_year_expr(date_col: str) -> pl.Expr:
    """Map every month/day onto a leap-year axis with a stable Feb 29 slot."""

    date_expr = pl.col(date_col)
    leap_alignment = (
        pl.when((~date_expr.dt.is_leap_year()) & (date_expr.dt.month() > 2))
        .then(1)
        .otherwise(0)
    )
    return date_expr.dt.ordinal_day() + leap_alignment


def _normalize_period(value: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    upper = re.sub(r"\s+", " ", text.upper().replace("-", " ").replace("_", " ")).strip()
    if upper in QUARTER_LABELS:
        return QUARTER_LABELS[upper]
    if upper in HALF_LABELS:
        return HALF_LABELS[upper]
    month_key = upper[:3]
    if month_key in MONTH_LABELS:
        return MONTH_LABELS[month_key]
    return text.strip()


def _normalize_frequency(value: str) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    upper = text.upper()
    if "QUART" in upper or upper.startswith("Q"):
        return "Quarterly"
    if "HALF" in upper or upper.startswith("H") or upper.startswith("S"):
        return "Half"
    if "MONTH" in upper:
        return "Monthly"
    return text.title()


def _normalize_reference(value) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 1 else 1
    except (TypeError, ValueError):
        return 1


def _build_series_key(root_code: str, month_label: str, reference: int) -> str:
    return f"{root_code}{KEY_DELIM}{month_label}{KEY_DELIM}{reference}"


def _month_to_quarter(value: str) -> str:
    return MONTH_TO_QUARTER.get(value or "", "")


def _month_to_half(value: str) -> str:
    return MONTH_TO_HALF.get(value or "", "")


def _extract_contract_month(value) -> str:
    month, _year = _parse_contract_month_yr(str(value or ""))
    return _normalize_period(month) if month else ""


def _ensure_date(df, date_col):
    dtype = df[date_col].dtype
    if dtype == pl.Date:
        return df
    if dtype == pl.Datetime:
        return df.with_columns(pl.col(date_col).cast(pl.Date))
    if dtype == pl.Utf8:
        for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y%m%d"]:
            parsed = df.with_columns(
                pl.col(date_col).str.strptime(pl.Date, fmt, strict=False).alias("__parsed_date")
            )
            if parsed["__parsed_date"].null_count() < len(parsed):
                return parsed.drop(date_col).rename({"__parsed_date": date_col})
    raise ValueError(f"Unsupported date column type for {date_col}: {dtype}")


def _is_data_access_error(exc: Exception) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in {5, 32, 33}:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "access is denied",
            "permission denied",
            "os error 5",
            "os error 32",
            "os error 33",
        )
    )


def _load_dataframe(data_path: str) -> pl.DataFrame:
    """Load an explicit source through Polars' streaming engine."""
    if not data_path:
        raise ValueError("--data-path is required; app.main fallback data is not supported.")

    path = Path(data_path)
    if not path.exists():
        raise ValueError(f"Pricing data file not found: {path}")
    if not path.is_file():
        raise ValueError(f"Pricing data path is not a file: {path}")

    lower = path.name.lower()

    def collect() -> pl.DataFrame:
        if lower.endswith((".parquet", ".pq")):
            return pl.scan_parquet(path, low_memory=True).collect(engine="streaming")
        if lower.endswith(".csv"):
            return pl.scan_csv(path, try_parse_dates=True, low_memory=True).collect(engine="streaming")
        if lower.endswith((".ipc", ".feather")):
            return pl.scan_ipc(path).collect(engine="streaming")
        raise ValueError(
            f"Unsupported data format for {path}. Expected Parquet (.parquet/.pq), CSV, IPC, or Feather."
        )

    for attempt in range(1, DATA_ACCESS_ATTEMPTS + 1):
        try:
            return collect()
        except Exception as exc:
            if not _is_data_access_error(exc):
                raise
            if attempt == DATA_ACCESS_ATTEMPTS:
                raise PermissionError(
                    f"Windows could not read pricing data after {DATA_ACCESS_ATTEMPTS} attempts: {path}. "
                    "Close any program using the file, check its permissions, and rerun UPDATE_AND_OPEN.bat."
                ) from exc
            time.sleep(DATA_ACCESS_RETRY_SECONDS)

    raise RuntimeError("Pricing data load retry loop ended unexpectedly.")


def _object_to_dict(value):
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        if isinstance(converted, dict):
            return dict(converted)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _entry_root(entry: dict, fallback="") -> str:
    value = (
        entry.get("root")
        or entry.get("security_root")
        or entry.get("root_code")
        or entry.get("code")
        or fallback
    )
    return str(value or "").strip().upper()


def _normalize_root_entry(entry: dict, fallback_root="") -> tuple[str, dict]:
    root = _entry_root(entry, fallback_root)
    if not root:
        return "", {}

    normalized = dict(entry)
    normalized["root"] = root
    common_name = str(
        entry.get("common_name")
        or entry.get("display_name")
        or entry.get("clean_name")
        or entry.get("name")
        or root
    ).strip()
    normalized["common_name"] = common_name
    normalized["display_name"] = common_name
    normalized["native_unit"] = str(
        entry.get("native_unit") or entry.get("source_unit") or entry.get("unit") or ""
    ).strip()
    normalized["yellow_key"] = str(
        entry.get("yellow_key") or entry.get("yellowKey") or ""
    ).strip()
    normalized["ticker_template"] = str(
        entry.get("ticker_template")
        or entry.get("tickerTemplate")
        or "{root}{month_code}{yy} {yellow_key}"
    ).strip()
    curve_mode = str(
        entry.get("curve_mode") or entry.get("curveMode") or "monthly"
    ).strip().lower()
    normalized["curve_mode"] = "flat" if curve_mode in {"flat", "flat forward", "monthless", "spot"} else "monthly"
    normalized["tradingview_symbol"] = str(
        entry.get("tradingview_symbol")
        or entry.get("tradingview")
        or entry.get("trading_view_symbol")
        or ""
    ).strip()
    for key in ("bbl_per_mt", "gal_per_bbl"):
        raw_value = entry.get(key)
        if raw_value in (None, ""):
            normalized[key] = None
        else:
            normalized[key] = float(raw_value)
    normalized["enabled"] = bool(entry.get("enabled", True))
    return root, normalized


def _load_root_metadata(config_path: str) -> tuple[dict[str, dict], set[str]]:
    path = Path(config_path)
    if not path.exists():
        raise ValueError(
            f"Root configuration not found: {path}. Create it from the security-roots workbook template."
        )
    try:
        from app.root_config import load_root_config
    except ImportError as exc:
        raise ValueError(
            "app.root_config is unavailable; the trade-builder root configuration module is required."
        ) from exc

    config = load_root_config(path)
    raw = config.to_dict()
    if not isinstance(raw, dict):
        raise ValueError("RootConfig.to_dict() must return a dictionary.")

    candidates = raw.get("roots") or raw.get("root_config") or raw.get("securities") or raw
    entries: dict[str, dict] = {}
    if isinstance(candidates, dict):
        for key, value in candidates.items():
            entry = _object_to_dict(value)
            root, normalized = _normalize_root_entry(entry, str(key))
            if root:
                entries[root] = normalized
    elif isinstance(candidates, list):
        for value in candidates:
            entry = _object_to_dict(value)
            root, normalized = _normalize_root_entry(entry)
            if root:
                entries[root] = normalized

    enabled_values = getattr(config, "enabled_roots", None)
    enabled_roots: set[str] = set()
    if enabled_values is not None:
        for value in enabled_values:
            if isinstance(value, str):
                root = value.strip().upper()
            else:
                root = _entry_root(_object_to_dict(value))
            if root:
                enabled_roots.add(root)
    if not enabled_roots:
        enabled_roots = {root for root, entry in entries.items() if entry.get("enabled", True)}
    if not entries:
        raise ValueError(f"Root configuration contains no security roots: {path}")
    return entries, enabled_roots


def _apply_root_configuration(
    df: pl.DataFrame,
    code_col: str,
    name_col: str | None,
    bbl_per_mt_col: str | None,
    gal_per_bbl_col: str | None,
    root_config_by_code: dict[str, dict],
    enabled_roots: set[str],
) -> tuple[pl.DataFrame, str, str, str]:
    if not code_col or code_col not in df.columns:
        raise ValueError(
            "No configured security root column was found. Add security_prefix/root_code "
            "or pass --code-col explicitly."
        )
    df = df.with_columns(
        pl.col(code_col).cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias(code_col)
    )
    data_roots = set(df[code_col].drop_nulls().unique().to_list())
    missing = sorted(str(root) for root in data_roots if root not in root_config_by_code)
    if missing:
        raise ValueError(
            "Pricing data contains roots with no configuration: "
            + ", ".join(missing)
            + ". Add exact rows to the Roots sheet or remove those roots from the input."
        )
    selected_roots = sorted(data_roots & enabled_roots)
    if not selected_roots:
        raise ValueError(
            "None of the roots present in the pricing data are enabled in the root configuration."
        )
    df = df.filter(pl.col(code_col).is_in(selected_roots))

    display_names = {root: root_config_by_code[root]["display_name"] for root in selected_roots}
    if not name_col or name_col not in df.columns:
        name_col = "CLEAN_NAME"
        df = df.with_columns(
            pl.col(code_col).replace_strict(display_names, return_dtype=pl.Utf8).alias(name_col)
        )
    else:
        df = df.with_columns(
            pl.col(code_col)
            .replace_strict(display_names, default=pl.col(name_col), return_dtype=pl.Utf8)
            .alias(name_col)
        )

    def apply_factor(column_name, config_key):
        factor_map = {
            root: root_config_by_code[root].get(config_key)
            for root in selected_roots
            if root_config_by_code[root].get(config_key) is not None
        }
        if not factor_map:
            return column_name
        if not column_name or column_name not in df.columns:
            column_name = config_key
            default_expr = None
        else:
            default_expr = pl.col(column_name).cast(pl.Float64, strict=False)
        expression = pl.col(code_col).replace_strict(
            factor_map,
            default=default_expr,
            return_dtype=pl.Float64,
        )
        return column_name, expression

    factor_result = apply_factor(bbl_per_mt_col, "bbl_per_mt")
    if isinstance(factor_result, tuple):
        bbl_per_mt_col, expression = factor_result
        df = df.with_columns(expression.alias(bbl_per_mt_col))
    factor_result = apply_factor(gal_per_bbl_col, "gal_per_bbl")
    if isinstance(factor_result, tuple):
        gal_per_bbl_col, expression = factor_result
        df = df.with_columns(expression.alias(gal_per_bbl_col))

    metadata_columns = {
        "native_unit": "native_unit",
        "yellow_key": "yellow_key",
        "ticker_template": "ticker_template",
        "curve_mode": "curve_mode",
        "tradingview_symbol": "tradingview_symbol",
    }
    expressions = []
    for output_col, config_key in metadata_columns.items():
        values = {root: str(root_config_by_code[root].get(config_key) or "") for root in selected_roots}
        expressions.append(
            pl.col(code_col).replace_strict(values, return_dtype=pl.Utf8).alias(output_col)
        )
    df = df.with_columns(expressions)
    return df, name_col, bbl_per_mt_col or "", gal_per_bbl_col or ""


def _extract_contract_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.split()[0].upper()


def _parse_contract_month_yr(value: str):
    if not value:
        return None, None
    text = str(value or "").strip()
    if not text:
        return None, None

    explicit = re.match(
        r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{1,4})$",
        text,
        flags=re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).title(), explicit.group(2)[-2:].zfill(2)

    token = _extract_contract_token(text)
    if not token:
        return None, None

    labeled = re.match(r"^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{1,2})$", token)
    if labeled:
        return labeled.group(1).title(), labeled.group(2).zfill(2)

    root_month = re.match(r"^[A-Z]{1,4}([FGHJKMNQUVXZ])(\d{1,2})$", token)
    if root_month:
        month = MONTH_CODE_TO_LABEL.get(root_month.group(1))
        return month, root_month.group(2).zfill(2)

    month_code = re.match(r"^([FGHJKMNQUVXZ])(\d{1,2})$", token)
    if month_code:
        month = MONTH_CODE_TO_LABEL.get(month_code.group(1))
        return month, month_code.group(2).zfill(2)

    return None, None


def _normalize_contract_year(value) -> int | None:
    if value is None:
        return None
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if year < 100:
        year += 2000 if year < 70 else 1900
    return year


def _extract_contract_year(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    _month, year = _parse_contract_month_yr(text)
    if year:
        return _normalize_contract_year(year)
    token = _extract_contract_token(text)
    match = re.search(r"(\d{1,4})$", token)
    if match:
        return _normalize_contract_year(match.group(1))
    return None


def _infer_root_code(security_str: str):
    token = str(security_str).strip().split()[0]
    match = re.match(
        r"^([A-Za-z]{1,4})(?:[FGHJKMNQUVXZ]|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{1,2}$",
        token,
        flags=re.IGNORECASE
    )
    if match:
        return match.group(1).upper()
    match = re.match(r"^([A-Za-z]+)", token)
    return match.group(1).upper() if match else str(security_str)


def build_embedded_data(
    df,
    unit,
    date_col,
    security_col,
    value_col,
    name_col,
    code_col=None,
    contract_col=None,
    contract_year_col=None,
    bbl_per_mt_col=None,
    gal_per_bbl_col=None,
    volume_col=None,
    vol_30d_col=None,
    month_col=None,
    frequency_col=None,
    reference_col=None,
    px_last_col=None,
    px_close_col=None,
    px_settle_col=None,
    px_fair_col=None,
    tradingview_symbols=None,
    root_config_by_code=None,
    precision=DEFAULT_PRECISION,
    built_at=None,
    data_max_date=None,
):
    precision = max(0, min(DEFAULT_PRECISION, int(precision)))
    root_config_by_code = root_config_by_code or {}
    root_names = {
        str(root): str(entry.get("display_name") or entry.get("name") or root)
        for root, entry in root_config_by_code.items()
    }
    value_fields = {}
    if px_last_col and px_last_col in df.columns:
        value_fields["PX_LAST"] = px_last_col
    if px_close_col and px_close_col in df.columns:
        value_fields["PX_CLOSE"] = px_close_col
    if px_settle_col and px_settle_col in df.columns:
        value_fields["PX_SETTLE"] = px_settle_col
    if px_fair_col and px_fair_col in df.columns:
        value_fields["PX_FAIR_1430"] = px_fair_col
    if not value_fields and value_col and value_col in df.columns:
        value_fields["VALUE"] = value_col

    if not value_fields:
        raise ValueError("No usable value columns were found for export.")

    if "PX_LAST" in value_fields:
        default_field = "PX_LAST"
    elif "PX_CLOSE" in value_fields:
        default_field = "PX_CLOSE"
    elif "PX_SETTLE" in value_fields:
        default_field = "PX_SETTLE"
    elif "PX_FAIR_1430" in value_fields:
        default_field = "PX_FAIR_1430"
    else:
        default_field = next(iter(value_fields.keys()))

    value_cols = list(value_fields.values())
    df = df.filter(pl.any_horizontal([pl.col(col).is_not_null() for col in value_cols]))
    df = df.filter(pl.col(date_col).is_not_null())
    df = df.filter(pl.col(security_col).is_not_null())

    if not code_col or code_col not in df.columns:
        raise ValueError(
            "A configured root column is required for export. Add security_prefix/root_code "
            "to the source data or pass --code-col explicitly."
        )
    root_expr = pl.col(code_col).cast(pl.Utf8).str.strip_chars().str.to_uppercase()

    name_candidates = []
    if name_col and name_col in df.columns:
        name_candidates.append(pl.col(name_col))
    if code_col and code_col in df.columns:
        name_candidates.append(pl.col(code_col))
    if security_col and security_col in df.columns:
        name_candidates.append(pl.col(security_col))
    name_expr = pl.coalesce(name_candidates) if name_candidates else pl.lit("")

    month_expr = pl.lit("")
    if month_col and month_col in df.columns:
        month_expr = pl.col(month_col).map_elements(_normalize_period, return_dtype=pl.Utf8)
    elif contract_col and contract_col in df.columns:
        month_expr = pl.col(contract_col).map_elements(_extract_contract_month, return_dtype=pl.Utf8)

    freq_expr = pl.lit("")
    if frequency_col and frequency_col in df.columns:
        freq_expr = pl.col(frequency_col).map_elements(_normalize_frequency, return_dtype=pl.Utf8)

    ref_expr = pl.lit(1)
    if reference_col and reference_col in df.columns:
        ref_expr = pl.col(reference_col).map_elements(_normalize_reference, return_dtype=pl.Int64)

    df = df.with_columns([
        root_expr.alias("_root_code"),
        name_expr.alias("_clean_name"),
        month_expr.alias("_month"),
        freq_expr.alias("_freq"),
        ref_expr.alias("_reference")
    ])
    if root_names:
        df = df.with_columns(
            pl.col("_root_code")
            .map_elements(lambda root: root_names.get(str(root), str(root)), return_dtype=pl.Utf8)
            .alias("_clean_name")
        )
    df = df.with_columns(
        pl.when((pl.col("_freq") == "") & (pl.col("_month").is_in(list(MONTH_LABELS.values()))))
        .then(pl.lit("Monthly"))
        .otherwise(pl.col("_freq"))
        .alias("_freq")
    )

    contract_year_expr = None
    if contract_year_col and contract_year_col in df.columns:
        contract_year_expr = pl.col(contract_year_col).map_elements(_normalize_contract_year, return_dtype=pl.Int64)
    elif contract_col and contract_col in df.columns:
        contract_year_expr = pl.col(contract_col).map_elements(_extract_contract_year, return_dtype=pl.Int64)

    df = df.sort(date_col)
    fallback_plot_year_expr = _plot_year_expr(date_col, "_reference", "_month")
    plot_year_expr = (
        pl.when(contract_year_expr.is_not_null())
        .then(contract_year_expr)
        .otherwise(fallback_plot_year_expr)
        if contract_year_expr is not None
        else fallback_plot_year_expr
    )
    canonical_day_expr = _canonical_day_of_year_expr(date_col)
    cycle_end_year_expr = plot_year_expr - (pl.col("_reference") - 1)
    roll_month_expr = pl.col("_month").map_elements(
        _roll_month_index, return_dtype=pl.Int64
    )
    # A reference series runs from its delivery month in the prior calendar
    # year through the month before delivery in its cycle-end year. Keep that
    # chronology numerically increasing while retaining month/day alignment.
    day_of_year_expr = (
        pl.when(
            (roll_month_expr > 1)
            & (pl.col(date_col).dt.year() == cycle_end_year_expr)
        )
        .then(canonical_day_expr + 366)
        .otherwise(canonical_day_expr)
    )
    df = df.with_columns([
        day_of_year_expr.alias("day_of_year"),
        plot_year_expr.alias("plot_year")
    ])
    working_columns = []
    for column in (
        date_col,
        *value_cols,
        volume_col,
        vol_30d_col,
        contract_col,
        contract_year_col,
        bbl_per_mt_col,
        gal_per_bbl_col,
        "_root_code",
        "_clean_name",
        "_month",
        "_freq",
        "_reference",
        "day_of_year",
        "plot_year",
    ):
        if column and column in df.columns and column not in working_columns:
            working_columns.append(column)
    df = df.select(working_columns)

    if "_month" in df.columns:
        df = df.with_columns([
            pl.col("_month").fill_null("").alias("_month"),
            pl.col("_freq").fill_null("").alias("_freq")
        ])
        df = df.with_columns(
            pl.concat_str([pl.col("_root_code"), pl.col("_reference")], separator=KEY_DELIM).alias("_root_ref")
        )
        monthly_df = df.filter(
            pl.col("_month").is_in(list(MONTH_LABELS.values()))
        ).filter((pl.col("_freq") == "") | (pl.col("_freq") == "Monthly"))

        monthly_root_refs = set(
            monthly_df.select("_root_ref").unique()["_root_ref"].to_list()
        )
        if monthly_root_refs:
            quarter_labels = list(QUARTER_MONTHS.keys())
            half_labels = list(HALF_MONTHS.keys())
            df = df.filter(
                ~(
                    pl.col("_root_ref").is_in(list(monthly_root_refs))
                    & (
                        pl.col("_month").is_in(quarter_labels + half_labels)
                        | pl.col("_freq").is_in(["Quarterly", "Half"])
                    )
                )
            )

        agg_exprs = [pl.col(col).sum().alias(col) for col in value_cols]
        if volume_col and volume_col in df.columns:
            agg_exprs.append(pl.col(volume_col).sum().alias(volume_col))
        if vol_30d_col and vol_30d_col in df.columns:
            agg_exprs.append(pl.col(vol_30d_col).mean().alias(vol_30d_col))

        group_cols = ["_root_code", "_clean_name", "_reference", date_col, "day_of_year"]
        derived_frames = []

        if monthly_df.height > 0:
            quarter_df = (
                monthly_df.with_columns(
                    pl.col("_month").alias("_month_raw"),
                    pl.col("_month").map_elements(_month_to_quarter, return_dtype=pl.Utf8).alias("_month")
                )
                .filter(pl.col("_month") != "")
            )
            if quarter_df.height > 0:
                quarter_df = quarter_df.group_by(group_cols + ["_month"]).agg(
                    agg_exprs + [pl.col("_month_raw").n_unique().alias("_month_count")]
                )
                quarter_df = quarter_df.filter(pl.col("_month_count") == 3).drop("_month_count")
                quarter_df = quarter_df.with_columns(pl.lit("Quarterly").alias("_freq"))
                quarter_df = quarter_df.with_columns([
                    (pl.col(col) * (1 / 3)).alias(col) for col in value_cols
                ])
                if volume_col and volume_col in quarter_df.columns:
                    quarter_df = quarter_df.with_columns((pl.col(volume_col) * (1 / 3)).alias(volume_col))
                quarter_df = quarter_df.with_columns(
                    _plot_year_expr(date_col, "_reference", "_month").alias("plot_year")
                )
                derived_frames.append(quarter_df)

            half_df = (
                monthly_df.with_columns(
                    pl.col("_month").alias("_month_raw"),
                    pl.col("_month").map_elements(_month_to_half, return_dtype=pl.Utf8).alias("_month")
                )
                .filter(pl.col("_month") != "")
            )
            if half_df.height > 0:
                half_df = half_df.group_by(group_cols + ["_month"]).agg(
                    agg_exprs + [pl.col("_month_raw").n_unique().alias("_month_count")]
                )
                half_df = half_df.filter(pl.col("_month_count") == 6).drop("_month_count")
                half_df = half_df.with_columns(pl.lit("Half").alias("_freq"))
                half_df = half_df.with_columns([
                    (pl.col(col) * (1 / 6)).alias(col) for col in value_cols
                ])
                if volume_col and volume_col in half_df.columns:
                    half_df = half_df.with_columns((pl.col(volume_col) * (1 / 6)).alias(volume_col))
                half_df = half_df.with_columns(
                    _plot_year_expr(date_col, "_reference", "_month").alias("plot_year")
                )
                derived_frames.append(half_df)

        if derived_frames:
            df = pl.concat([df] + derived_frames, how="diagonal_relaxed")

    years = df["plot_year"].unique().sort().to_list()
    year_x = {}
    for year in years:
        year_df = df.filter(pl.col("plot_year") == year)
        x_vals = year_df.select(pl.col("day_of_year")).unique().sort("day_of_year")
        year_x[str(year)] = x_vals["day_of_year"].to_list()

    df = df.with_columns(
        pl.concat_str(
            [pl.col("_root_code"), pl.col("_month"), pl.col("_reference")],
            separator=KEY_DELIM
        ).alias("_series_key")
    )

    commodities = {}
    commodity_meta = []
    sidebar_map = {}
    unit_factors = {}
    x_pool = []
    x_pool_index = {}

    def pack_series(year_key, x_values, y_values):
        if year_key in year_x and x_values == year_x[year_key]:
            return y_values
        x_key = tuple(x_values)
        pool_index = x_pool_index.get(x_key)
        if pool_index is None:
            pool_index = len(x_pool)
            x_pool_index[x_key] = pool_index
            x_pool.append(x_values)
        return {"x_ref": pool_index, "y": y_values}

    for root_code, root_entry in root_config_by_code.items():
        factor_entry = {}
        bbl_per_mt = root_entry.get("bbl_per_mt")
        gal_per_bbl = root_entry.get("gal_per_bbl")
        if bbl_per_mt is not None:
            factor_entry["bbl_per_mt"] = float(bbl_per_mt)
        if gal_per_bbl is not None:
            factor_entry["gal_per_bbl"] = float(gal_per_bbl)
        if factor_entry:
            unit_factors[str(root_code)] = factor_entry

    # Keep export memory bounded: sorting once lets us visit zero-copy slices
    # instead of materializing every series in a partition_by(..., as_dict=True)
    # mapping at the same time.
    df = df.sort(["_series_key", "plot_year", date_col])
    series_sizes = df.group_by("_series_key", maintain_order=True).len()
    series_offset = 0
    for series_key, series_length in series_sizes.iter_rows():
        group = df.slice(series_offset, series_length)
        series_offset += series_length
        code = str(series_key)
        root_code = str(group["_root_code"][0]) if "_root_code" in group.columns else code
        name = str(group["_clean_name"][0]) if "_clean_name" in group.columns else root_code
        contract_month = str(group["_month"][0]) if "_month" in group.columns else ""
        frequency = str(group["_freq"][0]) if "_freq" in group.columns else ""
        reference = _normalize_reference(group["_reference"][0]) if "_reference" in group.columns else 1
        contract_month_yr = ""
        contract_year = ""
        if contract_year_col and contract_year_col in group.columns:
            values = group[contract_year_col].drop_nulls().unique().to_list()
            if values:
                normalized = _normalize_contract_year(values[0])
                contract_year = str(normalized) if normalized else ""
        if contract_col and contract_col in group.columns:
            values = group[contract_col].drop_nulls().unique().to_list()
            if values:
                contract_month_yr = str(values[0])
                if not contract_year:
                    _month_val, parsed_year = _parse_contract_month_yr(contract_month_yr)
                    normalized = _normalize_contract_year(parsed_year)
                    contract_year = str(normalized) if normalized else ""

        if (bbl_per_mt_col and bbl_per_mt_col in group.columns) or (gal_per_bbl_col and gal_per_bbl_col in group.columns):
            factor_entry = unit_factors.get(root_code, {})
            if "bbl_per_mt" not in factor_entry and bbl_per_mt_col and bbl_per_mt_col in group.columns:
                bbl_values = group[bbl_per_mt_col].drop_nulls().unique().to_list()
                if bbl_values:
                    factor_entry["bbl_per_mt"] = float(bbl_values[0])
            if "gal_per_bbl" not in factor_entry and gal_per_bbl_col and gal_per_bbl_col in group.columns:
                gal_values = group[gal_per_bbl_col].drop_nulls().unique().to_list()
                if gal_values:
                    factor_entry["gal_per_bbl"] = float(gal_values[0])
            if factor_entry:
                unit_factors[root_code] = factor_entry

        field_series = {field_key: {} for field_key in value_fields}
        volume_years = {}
        vol30_years = {}
        year_min = int(group["plot_year"].min())
        year_max = int(group["plot_year"].max())

        year_sizes = group.group_by("plot_year", maintain_order=True).len()
        year_offset = 0
        for year, year_length in year_sizes.iter_rows():
            year_df = group.slice(year_offset, year_length)
            year_offset += year_length
            x_vals = year_df["day_of_year"].to_list()
            year_key = str(int(year))

            for field_key, col in value_fields.items():
                y_vals = year_df[col].to_list()
                y_vals = [round(float(v), precision) if v is not None else None for v in y_vals]
                field_series[field_key][year_key] = pack_series(year_key, x_vals, y_vals)

            if volume_col and volume_col in year_df.columns:
                v_vals = year_df[volume_col].to_list()
                v_vals = [round(float(v), precision) if v is not None else None for v in v_vals]
                volume_years[year_key] = pack_series(year_key, x_vals, v_vals)
            if vol_30d_col and vol_30d_col in year_df.columns:
                v30_vals = year_df[vol_30d_col].to_list()
                v30_vals = [round(float(v), precision) if v is not None else None for v in v30_vals]
                vol30_years[year_key] = pack_series(year_key, x_vals, v30_vals)

        years_map = field_series.pop(default_field, {})
        commodities[code] = {
            "name": name,
            "years": years_map,
            "root_code": root_code,
            "contract_month": contract_month,
            "contract_month_yr": contract_month_yr or "",
            "contract_year": contract_year or "",
            "frequency": frequency,
            "reference": reference
        }
        if field_series:
            commodities[code]["fields"] = field_series
        if volume_years:
            commodities[code]["volumes"] = volume_years
        if vol30_years:
            commodities[code]["volatility_30d"] = vol30_years

        commodity_meta.append({
            "code": code,
            "name": name,
            "rng": f"{year_min}-{year_max}",
            "root_code": root_code,
            "contract_month": contract_month,
            "contract_month_yr": contract_month_yr or "",
            "contract_year": contract_year or "",
            "frequency": frequency,
            "reference": reference
        })

        if root_code:
            entry = sidebar_map.get(root_code, {
                "code": root_code,
                "name": name or root_code,
                "min_year": year_min,
                "max_year": year_max
            })
            entry["min_year"] = min(entry["min_year"], year_min)
            entry["max_year"] = max(entry["max_year"], year_max)
            if name and (entry.get("name") in ("", root_code)):
                entry["name"] = name
            sidebar_map[root_code] = entry

    commodity_meta.sort(key=lambda item: item["code"])
    sidebar = [
        {"code": entry["code"], "name": entry["name"], "rng": f'{entry["min_year"]}-{entry["max_year"]}'}
        for entry in sidebar_map.values()
    ]
    sidebar.sort(key=lambda item: item["code"])

    built_at = built_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    if data_max_date is None and df.height:
        data_max_date = df[date_col].max()
    if hasattr(data_max_date, "isoformat"):
        data_max_date = data_max_date.isoformat()
    data_max_date = str(data_max_date or "")
    data_updated_at = f"{data_max_date}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_max_date) else data_max_date

    data = {
        "meta": {
            "unit": unit,
            "years": [int(y) for y in years],
            "yearX": year_x,
            "xPool": x_pool,
            "commodities": commodity_meta,
            "sidebar": sidebar,
            "fields": {
                "default": default_field,
                "available": list(value_fields.keys())
            },
            "built_at": built_at,
            "data_max_date": data_max_date,
            "updated_at": data_updated_at or built_at,
            "columns": {
                "date": date_col,
                "security": security_col,
                "value": value_col,
                "value_fields": value_fields,
                "volume": volume_col or "",
                "volatility_30d": vol_30d_col or "",
                "name": name_col or "",
                "root_code": code_col or "",
                "contract_month_yr": contract_col or "",
                "month": month_col or "",
                "frequency": frequency_col or "",
                "reference": reference_col or ""
            }
        },
        "commodities": commodities
    }

    if tradingview_symbols:
        data["meta"]["tradingview_symbols"] = tradingview_symbols

    if unit_factors:
        data["meta"]["unit_factors"] = unit_factors

    if root_config_by_code:
        data["meta"]["root_config"] = root_config_by_code

    # Numeric source columns are rounded before this function and derived
    # values are rounded while creating their output arrays. Returning the
    # structure directly avoids a second full-size payload allocation.
    return data


def _compressed_loader_js(payload_expression: str, payload_cleanup_js: str) -> str:
    """Return a browser loader for one gzip/base64 payload."""
    return f'''(function(){{
  "use strict";
  const unsupportedMessage = "This dashboard requires browser gzip support (DecompressionStream). Open it in a current Chrome, Edge, Firefox, or Safari release.";
  function decodeBase64Chunks(value) {{
    const chunkSize = 32760;
    const chunks = [];
    let totalLength = 0;
    for (let offset = 0; offset < value.length; offset += chunkSize) {{
      const binary = atob(value.slice(offset, offset + chunkSize));
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
      chunks.push(bytes);
      totalLength += bytes.length;
    }}
    const output = new Uint8Array(totalLength);
    let outputOffset = 0;
    for (const bytes of chunks) {{
      output.set(bytes, outputOffset);
      outputOffset += bytes.length;
    }}
    return output;
  }}
  async function decodeEmbedded() {{
    if (!("DecompressionStream" in window)) throw new Error(unsupportedMessage);
    let payloadBase64 = {payload_expression};
    if (!payloadBase64) throw new Error("Compressed embedded dashboard data is missing.");
    let compressedBytes = decodeBase64Chunks(payloadBase64);
    payloadBase64 = "";
    {payload_cleanup_js}
    const response = new Response(
      new Blob([compressedBytes]).stream().pipeThrough(new DecompressionStream("gzip"))
    );
    compressedBytes = null;
    let jsonText = await response.text();
    window.EMBEDDED_DATA = JSON.parse(jsonText);
    jsonText = "";
    const sharedX = window.EMBEDDED_DATA?.meta?.xPool || [];
    for (const commodity of Object.values(window.EMBEDDED_DATA?.commodities || {{}})) {{
      const maps = [
        commodity.years,
        ...Object.values(commodity.fields || {{}}),
        commodity.volumes,
        commodity.volatility_30d
      ];
      for (const seriesMap of maps) {{
        if (!seriesMap) continue;
        for (const series of Object.values(seriesMap)) {{
          if (!series || Array.isArray(series) || !Number.isInteger(series.x_ref)) continue;
          const pooledX = sharedX[series.x_ref];
          if (!pooledX) throw new Error(`Compressed dashboard x-axis reference ${{series.x_ref}} is invalid.`);
          series.x = pooledX;
          delete series.x_ref;
        }}
      }}
    }}
    if (window.EMBEDDED_DATA?.meta) delete window.EMBEDDED_DATA.meta.xPool;
    return window.EMBEDDED_DATA;
  }}
  window.__EMBEDDED_READY__ = decodeEmbedded().catch((error) => {{
    window.__EMBEDDED_ERROR__ = error;
    console.error("Embedded dashboard data decode failed:", error.message || error);
    throw error;
  }});
}})();'''


def build_compressed_embedded_js(data_b64: str) -> str:
    payload = json.dumps(data_b64, ensure_ascii=True)
    return _compressed_loader_js(payload, "payloadBase64 = \"\";") + "\n"


def embed_into_html(
    template_html,
    app_js,
    data_b64,
    plotly_js="",
    theme_js="",
    trade_math_js="",
):
    html = template_html

    removable_patterns = [
        r'<script\s+id=["\']embedded-data["\'][\s\S]*?</script>\s*',
        r'<script\s+id=["\']embedded-data-raw["\'][\s\S]*?</script>\s*',
        r'<script[^>]*src=["\'][^"\']*embedded_data\.js[^"\']*["\'][^>]*></script>\s*',
        r'<script[^>]*src=["\'][^"\']*trade_math\.js[^"\']*["\'][^>]*></script>\s*',
        r'<script[^>]*src=["\'][^"\']*app\.js[^"\']*["\'][^>]*></script>\s*',
        r'<script[^>]*src=["\'][^"\']*theme\.js[^"\']*["\'][^>]*></script>\s*',
    ]
    if plotly_js:
        removable_patterns.append(
            r'<script[^>]*src=["\'][^"\']*plotly[^"\']*["\'][^>]*></script>\s*'
        )
    for pattern in removable_patterns:
        html = re.sub(pattern, "", html, flags=re.IGNORECASE | re.MULTILINE)

    dom_loader = _compressed_loader_js(
        '(document.getElementById("embedded-data") || {}).textContent?.trim() || ""',
        'const payloadElement = document.getElementById("embedded-data"); '
        'if (payloadElement) { payloadElement.textContent = ""; payloadElement.remove(); }',
    )
    embedded_block = (
        f'<script id="embedded-data" type="application/octet-stream">{data_b64}</script>\n'
        '<script>window.DASHBOARD_OWNER_UPDATE_API = false;</script>\n'
        f'<script>\n{dom_loader}\n</script>\n'
        + (f'<script>\n{theme_js}\n</script>\n' if theme_js else '')
        + (f'<script>\n{plotly_js}\n</script>\n' if plotly_js else '')
        + (f'<script>\n{trade_math_js}\n</script>\n' if trade_math_js else '')
        + f'<script>\n{app_js}\n</script>\n'
    )

    if "</body>" in html:
        html = html.replace("</body>", embedded_block + "</body>")
    else:
        html += embedded_block
    return html


def _parse_fields(fields) -> list[str] | None:
    if fields is None or fields == "":
        return None
    if isinstance(fields, str):
        values = fields.split(",")
    else:
        values = list(fields)
    aliases = {
        "LAST": "PX_LAST",
        "CLOSE": "PX_CLOSE",
        "SETTLE": "PX_SETTLE",
        "FAIR": "PX_FAIR_1430",
        "PX_FAIR": "PX_FAIR_1430",
    }
    parsed = []
    for value in values:
        key = str(value).strip().upper()
        key = aliases.get(key, key)
        if key not in SUPPORTED_PRICE_FIELDS:
            raise ValueError(
                f"Unsupported price field {value!r}. Choose from: {', '.join(SUPPORTED_PRICE_FIELDS)}."
            )
        if key not in parsed:
            parsed.append(key)
    if not parsed:
        raise ValueError("--fields must include at least one supported price field.")
    return parsed


def _read_text_asset(path: str, label: str, required=True) -> str:
    if not path:
        if required:
            raise ValueError(f"{label} path is required.")
        return ""
    asset = Path(path)
    if not asset.exists():
        if required:
            raise ValueError(f"{label} not found: {asset}")
        return ""
    return asset.read_text(encoding="utf-8").strip()


def export_dashboard(
    *,
    data_path: str,
    root_config_path: str = DEFAULT_ROOT_CONFIG,
    output: str = DEFAULT_OUTPUT,
    embedded_js_output: str = "",
    compact_parquet_output: str = "",
    data_json: str = "",
    fields=None,
    parquet_fields=None,
    precision: int = DEFAULT_PRECISION,
    unit: str = "$/bbl",
    template: str = DEFAULT_TEMPLATE,
    js: str = DEFAULT_JS,
    trade_math: str = DEFAULT_TRADE_MATH,
    plotly: str = DEFAULT_PLOTLY,
    theme: str = DEFAULT_THEME,
    max_output_mb: float = 20.0,
    include_analytics: bool = False,
    column_overrides: dict | None = None,
    built_at: str | None = None,
) -> dict:
    precision = int(precision)
    if precision < 0 or precision > DEFAULT_PRECISION:
        raise ValueError(f"precision must be between 0 and {DEFAULT_PRECISION}.")
    selected_fields = _parse_fields(fields)
    selected_parquet_fields = _parse_fields(parquet_fields)
    overrides = column_overrides or {}

    df = _load_dataframe(data_path)
    date_col = overrides.get("date") or _pick_column(df.columns, DATE_CANDIDATES)
    security_col = overrides.get("security") or _pick_column(df.columns, SECURITY_CANDIDATES)
    value_col = overrides.get("value") or _pick_column(df.columns, VALUE_CANDIDATES)
    name_col = overrides.get("name") or _pick_column(df.columns, NAME_CANDIDATES)
    code_col = overrides.get("code") or _pick_column(df.columns, CODE_CANDIDATES)
    contract_col = overrides.get("contract") or _pick_column(df.columns, CONTRACT_MONTH_YR_CANDIDATES)
    contract_year_col = overrides.get("contract_year") or _pick_column(df.columns, CONTRACT_YEAR_CANDIDATES)
    month_col = overrides.get("month") or _pick_column(df.columns, MONTH_CANDIDATES)
    frequency_col = overrides.get("frequency") or _pick_column(df.columns, FREQUENCY_CANDIDATES)
    reference_col = overrides.get("reference") or _pick_column(df.columns, REFERENCE_CANDIDATES)
    bbl_per_mt_col = overrides.get("bbl_per_mt") or _pick_column(df.columns, BBL_PER_MT_CANDIDATES)
    gal_per_bbl_col = overrides.get("gal_per_bbl") or _pick_column(df.columns, GAL_PER_BBL_CANDIDATES)
    volume_col = overrides.get("volume") or _pick_column(df.columns, VOLUME_CANDIDATES)
    vol_30d_col = overrides.get("vol_30d") or _pick_column(df.columns, VOL_30D_CANDIDATES)
    if not include_analytics:
        volume_col = None
        vol_30d_col = None

    source_field_columns = {
        "PX_LAST": overrides.get("px_last") or _pick_column(df.columns, PX_LAST_CANDIDATES),
        "PX_CLOSE": overrides.get("px_close") or _pick_column(df.columns, PX_CLOSE_CANDIDATES),
        "PX_SETTLE": overrides.get("px_settle") or _pick_column(df.columns, PX_SETTLE_CANDIDATES),
        "PX_FAIR_1430": overrides.get("px_fair") or _pick_column(df.columns, PX_FAIR_CANDIDATES),
    }
    numeric_price_columns = sorted(
        {column for column in source_field_columns.values() if column}
    )
    if numeric_price_columns:
        df = df.with_columns(
            [
                pl.col(column).cast(pl.Float64, strict=False).alias(column)
                for column in numeric_price_columns
            ]
        )
    if selected_fields is None:
        selected_fields = [field for field in SUPPORTED_PRICE_FIELDS if source_field_columns.get(field)]
    else:
        missing_fields = [field for field in selected_fields if not source_field_columns.get(field)]
        if missing_fields:
            raise ValueError(
                "Requested price fields are missing from the source: " + ", ".join(missing_fields)
            )
    if selected_parquet_fields is None:
        selected_parquet_fields = list(selected_fields)
    else:
        missing_parquet_fields = [
            field for field in selected_parquet_fields if not source_field_columns.get(field)
        ]
        if missing_parquet_fields:
            raise ValueError(
                "Requested Parquet fields are missing from the source: "
                + ", ".join(missing_parquet_fields)
            )
    field_columns = {
        field: (column if field in selected_fields else None)
        for field, column in source_field_columns.items()
    }
    value_col = field_columns.get("PX_LAST") or next(
        (field_columns[field] for field in selected_fields if field_columns.get(field)),
        value_col,
    )
    if not date_col or not security_col or not code_col or not value_col:
        raise ValueError(
            "Required columns were not found. The source needs date, security, configured root, "
            "and at least one requested price field. Use explicit column flags when names differ."
        )

    df = _ensure_date(df, date_col)
    today = date.today()
    df = df.filter(pl.col(date_col).is_not_null() & (pl.col(date_col) <= today))
    if not df.height:
        raise ValueError("No pricing rows remain after date validation and future-date filtering.")

    root_config_by_code, enabled_roots = _load_root_metadata(root_config_path)
    df, name_col, bbl_per_mt_col, gal_per_bbl_col = _apply_root_configuration(
        df,
        code_col,
        name_col,
        bbl_per_mt_col,
        gal_per_bbl_col,
        root_config_by_code,
        enabled_roots,
    )
    # Freshness must describe the rows that actually reach the dashboard. A
    # newer disabled root must never make an older enabled snapshot look fresh.
    data_max_date = df[date_col].max()
    df = _round_dataframe_floats(df, precision)
    exported_roots = sorted(df[code_col].unique().to_list())
    export_root_config = {root: root_config_by_code[root] for root in exported_roots}
    tradingview_symbols = {}
    for root, entry in export_root_config.items():
        symbol = entry.get("tradingview_symbol")
        if symbol:
            tradingview_symbols[root] = symbol
            tradingview_symbols[entry["display_name"]] = symbol

    if compact_parquet_output:
        parquet_path = Path(compact_parquet_output)
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        unselected_columns = {
            column
            for field, column in source_field_columns.items()
            if field not in selected_parquet_fields and column
        }
        compact_df = df.drop(sorted(unselected_columns)) if unselected_columns else df
        compact_df.write_parquet(
            parquet_path,
            compression="zstd",
            compression_level=9,
            statistics=True,
            row_group_size=65_536,
        )
        del compact_df

    # The standalone payload does not need source-only descriptive columns or
    # the duplicated config metadata written to the compact Parquet artifact.
    # Project before the transformation-heavy payload build so every derived
    # frame carries only columns that can reach the browser.
    payload_columns = []
    for column in (
        date_col,
        security_col,
        name_col,
        code_col,
        contract_col,
        contract_year_col,
        bbl_per_mt_col,
        gal_per_bbl_col,
        volume_col,
        vol_30d_col,
        month_col,
        frequency_col,
        reference_col,
        value_col,
        *field_columns.values(),
    ):
        if column and column in df.columns and column not in payload_columns:
            payload_columns.append(column)
    df = df.select(payload_columns)

    data = build_embedded_data(
        df,
        unit,
        date_col,
        security_col,
        value_col,
        name_col,
        code_col=code_col,
        contract_col=contract_col,
        contract_year_col=contract_year_col,
        bbl_per_mt_col=bbl_per_mt_col,
        gal_per_bbl_col=gal_per_bbl_col,
        volume_col=volume_col,
        vol_30d_col=vol_30d_col,
        month_col=month_col,
        frequency_col=frequency_col,
        reference_col=reference_col,
        px_last_col=field_columns.get("PX_LAST"),
        px_close_col=field_columns.get("PX_CLOSE"),
        px_settle_col=field_columns.get("PX_SETTLE"),
        px_fair_col=field_columns.get("PX_FAIR_1430"),
        tradingview_symbols=tradingview_symbols,
        root_config_by_code=export_root_config,
        precision=precision,
        built_at=built_at,
        data_max_date=data_max_date,
    )

    json_bytes = json.dumps(
        data,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    payload_data_max_date = data["meta"]["data_max_date"]
    payload_built_at = data["meta"]["built_at"]
    del data
    json_size = len(json_bytes)
    if data_json:
        data_json_path = Path(data_json)
        data_json_path.parent.mkdir(parents=True, exist_ok=True)
        data_json_path.write_bytes(json_bytes)
    compressed = gzip.compress(json_bytes, compresslevel=9, mtime=0)
    del json_bytes
    compressed_size = len(compressed)
    data_b64 = base64.b64encode(compressed).decode("ascii")
    del compressed
    if embedded_js_output:
        embedded_path = Path(embedded_js_output)
        embedded_path.parent.mkdir(parents=True, exist_ok=True)
        embedded_path.write_text(build_compressed_embedded_js(data_b64), encoding="utf-8")

    template_html = _read_text_asset(template, "HTML template")
    app_js = _read_text_asset(js, "application JavaScript")
    trade_math_js = _read_text_asset(trade_math, "trade math JavaScript")
    theme_js = _read_text_asset(theme, "theme JavaScript", required=False)
    plotly_js = _read_text_asset(plotly, "Plotly bundle")
    output_html = embed_into_html(
        template_html,
        app_js,
        data_b64,
        plotly_js=plotly_js,
        theme_js=theme_js,
        trade_math_js=trade_math_js,
    )
    output_bytes = output_html.encode("utf-8")
    output_mb = len(output_bytes) / 1024 / 1024
    if max_output_mb and output_mb > float(max_output_mb):
        raise ValueError(
            f"Standalone export is {output_mb:.2f} MB, above the {float(max_output_mb):.2f} MB budget. "
            "Select fewer --fields or split the dashboard profile."
        )

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output_bytes)

    summary = {
        "output": str(output_path),
        "output_bytes": len(output_bytes),
        "output_mb": output_mb,
        "json_bytes": json_size,
        "gzip_bytes": compressed_size,
        "rows": df.height,
        "roots": exported_roots,
        "fields": selected_fields,
        "parquet_fields": selected_parquet_fields,
        "analytics_included": bool(include_analytics),
        "data_max_date": payload_data_max_date,
        "built_at": payload_built_at,
    }
    print(
        f"Wrote {output_path} ({output_mb:.2f} MB; gzip data {compressed_size / 1024 / 1024:.2f} MB)"
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Build a config-backed, compressed single-file trade-builder dashboard.")
    parser.add_argument("--data-path", required=True, help="Required Parquet/CSV/IPC/Feather pricing data path.")
    parser.add_argument("--root-config", default=DEFAULT_ROOT_CONFIG, help="Security-root workbook path.")
    parser.add_argument("--fields", default="", help="Comma-separated price fields; default preserves every available standard field.")
    parser.add_argument("--parquet-fields", default="", help="Comma-separated price fields retained in compact Parquet; default matches --fields.")
    parser.add_argument("--precision", type=int, choices=range(DEFAULT_PRECISION + 1), default=DEFAULT_PRECISION)
    parser.add_argument("--date-col", default="", help="Date column name.")
    parser.add_argument("--security-col", default="", help="Security/ticker column name.")
    parser.add_argument("--value-col", default="", help="Fallback numeric value column name.")
    parser.add_argument("--px-last-col", default="", help="PX_LAST column name.")
    parser.add_argument("--px-close-col", default="", help="PX_CLOSE column name.")
    parser.add_argument("--px-settle-col", default="", help="PX_SETTLE column name.")
    parser.add_argument("--px-fair-col", default="", help="14:30 fair value column name.")
    parser.add_argument("--name-col", default="", help="Display-name column name.")
    parser.add_argument("--code-col", default="", help="Configured exact root column name.")
    parser.add_argument("--contract-col", default="", help="Contract month/year column name.")
    parser.add_argument("--contract-year-col", default="", help="Contract year column name.")
    parser.add_argument("--month-col", default="", help="Contract month column name.")
    parser.add_argument("--frequency-col", default="", help="Frequency column name.")
    parser.add_argument("--reference-col", default="", help="Reference column name.")
    parser.add_argument("--bbl-per-mt-col", default="", help="Barrels-per-metric-ton column name.")
    parser.add_argument("--gal-per-bbl-col", default="", help="Gallons-per-barrel column name.")
    parser.add_argument("--volume-col", default="", help="Volume column name.")
    parser.add_argument("--vol-30d-col", default="", help="30-day volatility column name.")
    parser.add_argument(
        "--include-analytics",
        action="store_true",
        help="Include optional volume and precomputed 30-day volatility arrays; off by default for a lean trade-builder export.",
    )
    parser.add_argument("--data-json", default="", help="Optional compact JSON output path.")
    parser.add_argument("--embedded-js-output", default="", help="Optional compressed embedded-data JS output path.")
    parser.add_argument("--compact-parquet-output", default="", help="Optional rounded ZSTD Parquet output path.")
    parser.add_argument("--unit", default="$/bbl", help="Legacy fallback base-unit label; per-root native units come from config.")
    parser.add_argument("--template", default=DEFAULT_TEMPLATE, help="HTML template path.")
    parser.add_argument("--js", default=DEFAULT_JS, help="Application JS path to inline.")
    parser.add_argument("--trade-math", default=DEFAULT_TRADE_MATH, help="Trade math JS path to inline before app.js.")
    parser.add_argument("--plotly", default=DEFAULT_PLOTLY, help="Plotly bundle path to inline.")
    parser.add_argument("--theme", default=DEFAULT_THEME, help="Theme JS path to inline.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Standalone HTML output path.")
    parser.add_argument("--max-output-mb", type=float, default=20.0, help="Fail if standalone exceeds this size; 0 disables.")
    args = parser.parse_args()
    column_overrides = {
        "date": args.date_col,
        "security": args.security_col,
        "value": args.value_col,
        "px_last": args.px_last_col,
        "px_close": args.px_close_col,
        "px_settle": args.px_settle_col,
        "px_fair": args.px_fair_col,
        "name": args.name_col,
        "code": args.code_col,
        "contract": args.contract_col,
        "contract_year": args.contract_year_col,
        "month": args.month_col,
        "frequency": args.frequency_col,
        "reference": args.reference_col,
        "bbl_per_mt": args.bbl_per_mt_col,
        "gal_per_bbl": args.gal_per_bbl_col,
        "volume": args.volume_col,
        "vol_30d": args.vol_30d_col,
    }
    column_overrides = {key: value for key, value in column_overrides.items() if value}
    try:
        export_dashboard(
            data_path=args.data_path,
            root_config_path=args.root_config,
            output=args.output,
            embedded_js_output=args.embedded_js_output,
            compact_parquet_output=args.compact_parquet_output,
            data_json=args.data_json,
            fields=args.fields,
            parquet_fields=args.parquet_fields,
            precision=args.precision,
            unit=args.unit,
            template=args.template,
            js=args.js,
            trade_math=args.trade_math,
            plotly=args.plotly,
            theme=args.theme,
            max_output_mb=args.max_output_mb,
            include_analytics=args.include_analytics,
            column_overrides=column_overrides,
        )
    except (OSError, ValueError, pl.exceptions.PolarsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
