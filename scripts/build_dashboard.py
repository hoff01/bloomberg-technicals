#!/usr/bin/env python3
"""Build the standalone Pricing Dashboard trade-builder artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.export_single_file import DEFAULT_PRECISION, SUPPORTED_PRICE_FIELDS, export_dashboard
from app.root_config import load_root_config


DEFAULT_DATA = PROJECT_ROOT / "data" / "pricing_history.csv"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "security_roots.xlsx"
DEFAULT_HTML = PROJECT_ROOT / "dist" / "pricing_dashboard_trade_builder.html"
DEFAULT_PARQUET = PROJECT_ROOT / "dist" / "pricing_data.parquet"
DEFAULT_EMBEDDED_JS = PROJECT_ROOT / "app" / "static" / "embedded_data.js"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the config-backed Pricing Dashboard trade builder."
    )
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="Source Parquet/CSV/IPC path.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Security-root workbook path.")
    parser.add_argument("--output", default=str(DEFAULT_HTML), help="Standalone HTML output path.")
    parser.add_argument(
        "--parquet-output",
        default=str(DEFAULT_PARQUET),
        help="Rounded ZSTD Parquet output path.",
    )
    parser.add_argument(
        "--embedded-js-output",
        default=str(DEFAULT_EMBEDDED_JS),
        help="Compressed embedded-data JS output path for local index.html.",
    )
    parser.add_argument(
        "--fields",
        default="",
        help="Comma-separated price fields embedded in the dashboard; default uses Bloomberg Update dashboard_fields.",
    )
    parser.add_argument(
        "--parquet-fields",
        default="",
        help="Comma-separated price fields retained in Parquet; default uses supported Bloomberg Update fields.",
    )
    parser.add_argument(
        "--precision",
        type=int,
        choices=range(DEFAULT_PRECISION + 1),
        default=DEFAULT_PRECISION,
        help="Maximum stored/output decimal places.",
    )
    parser.add_argument(
        "--max-output-mb",
        type=float,
        default=40.0,
        help="Fail if the standalone file exceeds this budget; 0 disables.",
    )
    parser.add_argument(
        "--include-analytics",
        action="store_true",
        help="Include optional volume and precomputed volatility arrays.",
    )
    args = parser.parse_args()

    try:
        root_config = load_root_config(args.config)
        dashboard_fields = args.fields or ",".join(root_config.update.dashboard_fields)
        parquet_fields = args.parquet_fields or ",".join(
            field for field in root_config.update.fields if field in SUPPORTED_PRICE_FIELDS
        )
        summary = export_dashboard(
            data_path=args.data,
            root_config_path=args.config,
            output=args.output,
            embedded_js_output=args.embedded_js_output,
            compact_parquet_output=args.parquet_output,
            fields=dashboard_fields,
            parquet_fields=parquet_fields,
            precision=args.precision,
            max_output_mb=args.max_output_mb,
            include_analytics=args.include_analytics,
        )
    except Exception as exc:
        parser.error(str(exc))

    roots = ", ".join(summary["roots"])
    fields = ", ".join(summary["fields"])
    parquet_fields = ", ".join(summary["parquet_fields"])
    print(f"Data max date: {summary['data_max_date']}")
    print(f"Roots: {roots}")
    print(f"Fields: {fields}")
    print(f"Parquet fields: {parquet_fields}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
