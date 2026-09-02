#!/usr/bin/env python3
"""Verify the owner runtime without opening a Bloomberg network session."""

from __future__ import annotations

import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SUPPORTED_PYTHON = ((3, 12), (3, 13))


def main() -> int:
    version = sys.version_info[:2]
    if version not in SUPPORTED_PYTHON:
        supported = " or ".join(f"{major}.{minor}" for major, minor in SUPPORTED_PYTHON)
        print(
            f"ERROR: Python {version[0]}.{version[1]} is not supported by this owner setup; "
            f"use Python {supported}."
        )
        return 1

    try:
        import blpapi
        import numpy
        import openpyxl
        import polars as pl
        import pypdf
        import reportlab
        import xbbg
    except (ImportError, OSError) as exc:
        print(f"ERROR: A required native or Python package could not be imported: {exc}")
        return 1

    try:
        new_york = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        print(
            "ERROR: Windows timezone data is unavailable; reinstall requirements.txt "
            f"so tzdata is present: {exc}"
        )
        return 1
    if new_york.key != "America/New_York":
        print("ERROR: America/New_York timezone smoke check returned the wrong zone.")
        return 1

    # Exercise each native runtime without attempting a licensed Bloomberg
    # connection. SessionOptions reaches the BLPAPI binding; the tiny Polars
    # expression reaches its platform runtime.
    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)
    # Accessing xbbg.__version__ alone does not load its native extension.
    # configure() and set_backend() force the C++ SDK-backed engine to load
    # without opening a Bloomberg network session.
    xbbg.configure(
        host="localhost",
        port=8194,
        request_pool_size=2,
        request_timeout_ms=120_000,
        retry_max_retries=1,
    )
    xbbg.set_backend("polars")
    rounded = pl.DataFrame({"value": [1.234567]}).select(
        pl.col("value").round(5)
    )["value"][0]
    if rounded != 1.23457:
        print("ERROR: Polars runtime smoke check returned an unexpected result.")
        return 1

    print(
        "Runtime OK: "
        f"Python {sys.version.split()[0]} | "
        f"blpapi {blpapi.__version__} | "
        f"xbbg {xbbg.__version__} | "
        f"polars {pl.__version__} | "
        f"numpy {numpy.__version__} | "
        f"openpyxl {openpyxl.__version__} | "
        f"reportlab {reportlab.Version} | "
        f"pypdf {pypdf.__version__}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
