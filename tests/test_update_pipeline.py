from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
import polars as pl

from app.bloomberg_client import BloombergPullResult
from app.root_config import ROOT_COLUMNS, UPDATE_SHEET_NAME, load_root_config
from app.update_pipeline import (
    UpdateError,
    UpdatePaths,
    _build_requests,
    _normalize_existing_frame,
    _promote_artifacts,
    build_contract_universe,
    normalize_bloomberg_rows,
    run_bloomberg_update,
    validate_canonical_frame,
)
from scripts.validate_pricing_data import count_values_over_precision


ROOT_ROWS = (
    (True, "WU", "GC Jet", "Comdty", "cpg", 7.45, 42, "{root}{month_code}{yy} {yellow_key}", "Monthly", "NYMEX:WU", "ME", "Refined", 1),
    (True, "HO", "Heating Oil", "Index", "$/gal", 7.45, 42, "{root}{month_code}{y} {yellow_key}", "Monthly", "NYMEX:HO", "", "Refined", 2),
    (True, "NAUG008A", "RVO", "Index", "cpg", 7.45, 42, "{root} {yellow_key}", "Flat", "", "RVO|RENEWABLE VOLUME OBLIGATION", "Renewable Fuels", 3),
)


def write_config(
    path: Path,
    *,
    max_mb: float = 20.0,
    contract_start_year: int = 2026,
    contract_end_year: int = 2026,
    wu_ticker_template: str = "{root}{month_code}{yy} {yellow_key}",
) -> None:
    workbook = Workbook()
    roots = workbook.active
    roots.title = "Security Roots"
    roots.append(ROOT_COLUMNS)
    for row in ROOT_ROWS:
        if row[1] == "WU":
            row = (*row[:7], wu_ticker_template, *row[8:])
        roots.append(row)
    update = workbook.create_sheet(UPDATE_SHEET_NAME)
    update.append(("setting", "value", "description"))
    settings = (
        ("history_start", "2024-01-01"),
        ("contract_start_year", contract_start_year),
        ("contract_end_year", contract_end_year),
        ("contract_history_months", 24),
        ("reference_depth", 2),
        ("overlap_days", 7),
        ("fields", "PX_LAST,PX_CLOSE,PX_SETTLE,PX_FAIR_1430"),
        ("dashboard_fields", "PX_LAST,PX_SETTLE"),
        ("host", "localhost"),
        ("port", 8194),
        ("service", "//blp/refdata"),
        ("batch_size", 25),
        ("request_timeout_seconds", 30),
        ("standalone_max_mb", max_mb),
    )
    for key, value in settings:
        update.append((key, value, ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


@dataclass
class FakeClient:
    calls: list[tuple]
    price: float = 200.123456789
    null_optional_fields: bool = False

    def fetch(self, requests, fields, *, batch_size, timeout_seconds):
        self.calls.append((tuple(requests), tuple(fields), batch_size, timeout_seconds))
        rows = []
        for index, request in enumerate(requests):
            value = self.price + index / 1000
            rows.append(
                {
                    "security": request.security,
                    "date": request.end_date,
                    "PX_LAST": value,
                    "PX_CLOSE": None if self.null_optional_fields else value - 0.1111111,
                    "PX_SETTLE": None if self.null_optional_fields else value + 0.2222222,
                    "PX_FAIR_1430": None if self.null_optional_fields else value - 0.3333333,
                }
            )
        return BloombergPullResult(tuple(rows), ())


class UpdatePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config" / "security_roots.xlsx"
        write_config(self.config_path)
        self.paths = UpdatePaths.under(self.root, self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_existing_csv_read_retries_a_transient_windows_access_error(self) -> None:
        config = load_root_config(self.config_path)
        specs = build_contract_universe(config, date(2026, 8, 3))
        spec = next(item for item in specs if item.ticker == "WUF26 Comdty")
        self.paths.csv.parent.mkdir(parents=True, exist_ok=True)
        self.paths.csv.write_text("locked-placeholder", encoding="utf-8")
        source = pl.DataFrame(
            {
                "date": [date(2025, 12, 31)],
                "security_str": [spec.ticker],
                "PX_LAST": [200.0],
            }
        )

        with (
            patch(
                "app.update_pipeline.pl.read_csv",
                side_effect=[PermissionError("Access is denied. (os error 5)"), source],
            ) as read_csv,
            patch("app.update_pipeline.time.sleep") as sleep,
        ):
            normalized = _normalize_existing_frame(
                self.paths.csv,
                specs,
                config.update.fields,
                config.update.reference_depth,
            )

        self.assertEqual(normalized.height, 1)
        self.assertEqual(read_csv.call_count, 2)
        sleep.assert_called_once()

    def test_existing_csv_is_released_before_artifact_promotion(self) -> None:
        config = load_root_config(self.config_path)
        specs = build_contract_universe(config, date(2026, 8, 3))
        spec = next(item for item in specs if item.ticker == "WUF26 Comdty")
        self.paths.csv.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(
            {
                "date": [date(2025, 12, 31)],
                "security_str": [spec.ticker],
                "PX_LAST": [200.0],
            }
        ).write_csv(self.paths.csv)

        normalized = _normalize_existing_frame(
            self.paths.csv,
            specs,
            config.update.fields,
            config.update.reference_depth,
        )
        staged = self.paths.csv.with_name("staged.csv")
        staged.write_bytes(self.paths.csv.read_bytes())
        os.replace(staged, self.paths.csv)

        self.assertEqual(normalized.height, 1)
        self.assertTrue(self.paths.csv.is_file())

    def test_artifact_promotion_retries_a_transient_windows_access_error(self) -> None:
        staged = self.root / "staged.csv"
        target = self.root / "pricing_history.csv"
        backup_root = self.root / "backups"
        staged.write_text("fresh", encoding="utf-8")
        target.write_text("previous", encoding="utf-8")
        original_replace = os.replace
        attempts = 0

        def replace_with_transient_failure(staged_path: Path, target_path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("Access is denied. (os error 5)")
            original_replace(staged_path, target_path)

        with (
            patch(
                "app.update_pipeline.os.replace",
                side_effect=replace_with_transient_failure,
            ) as replace,
            patch("app.update_pipeline.time.sleep") as sleep,
        ):
            _promote_artifacts(((staged, target),), backup_root)

        self.assertEqual(target.read_text(encoding="utf-8"), "fresh")
        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once()

    def test_spreadsheet_generates_wu_comdty_and_ho_index_tickers(self) -> None:
        config = load_root_config(self.config_path)
        specs = build_contract_universe(config, date(2026, 8, 3))
        tickers = {spec.ticker for spec in specs}
        self.assertIn("WUF26 Comdty", tickers)
        self.assertIn("WUZ26 Comdty", tickers)
        self.assertIn("HOF26 Index", tickers)
        self.assertIn("HOG26 Index", tickers)
        self.assertIn("HOZ26 Index", tickers)
        self.assertIn("NAUG008A Index", tickers)
        self.assertEqual(sum(ticker == "NAUG008A Index" for ticker in tickers), 1)

    def test_default_workbook_generates_hog26_comdty(self) -> None:
        config = load_root_config("config/security_roots.xlsx")
        specs = build_contract_universe(config, date(2026, 8, 3))
        tickers = {spec.ticker for spec in specs}
        self.assertIn("HOG26 Comdty", tickers)
        self.assertIn("XBG26 Comdty", tickers)
        self.assertIn("WUG18 Comdty", tickers)
        self.assertIn("WUG28 Comdty", tickers)
        self.assertIn("NAUG008A Index", tickers)
        first_wu_contract = next(
            spec
            for spec in specs
            if spec.root == "WU" and spec.contract_year == 2018 and spec.month_code == "F"
        )
        self.assertEqual(first_wu_contract.start_date, date(2015, 1, 1))
        self.assertEqual(first_wu_contract.end_date, date(2017, 12, 31))

    def test_default_history_requests_three_years_per_monthly_contract(self) -> None:
        config = load_root_config("config/security_roots.xlsx")
        specs = build_contract_universe(config, date(2026, 8, 3))
        ho_2026 = {
            spec.month_label: spec
            for spec in specs
            if spec.root == "HO" and spec.contract_year == 2026
        }

        self.assertEqual(ho_2026["Jan"].start_date, date(2023, 1, 1))
        self.assertEqual(ho_2026["Jan"].end_date, date(2025, 12, 31))
        self.assertEqual(ho_2026["Dec"].start_date, date(2023, 12, 1))

    def test_european_roots_prefer_ny_fair_value_and_fallback_to_px_last(self) -> None:
        config = load_root_config("config/security_roots.xlsx")
        specs = build_contract_universe(config, date(2026, 8, 3))
        selected = {
            spec.root: spec
            for spec in specs
            if spec.root in {"QS", "CO", "HO"}
            and spec.contract_year == 2026
            and spec.month_code == "F"
        }
        requests = _build_requests(
            tuple(selected.values()),
            pl.DataFrame(),
            config.update.overlap_days,
            date(2026, 8, 3),
            True,
        )
        requests_by_security = {request.security: request for request in requests}

        for root_code in ("QS", "CO"):
            request = requests_by_security[selected[root_code].ticker]
            self.assertEqual(
                request.extra_fields,
                ("FAIR_VALUE_SNAPSHOT_AT_1630",),
            )
            self.assertEqual(
                request.overrides,
                (("TIME_ZONE_OVERRIDE", "13"),),
            )
        ho_request = requests_by_security[selected["HO"].ticker]
        self.assertFalse(ho_request.extra_fields)
        self.assertFalse(ho_request.overrides)

        frame, warnings = normalize_bloomberg_rows(
            (
                {
                    "security": selected["QS"].ticker,
                    "date": selected["QS"].end_date,
                    "PX_LAST": None,
                    "PX_CLOSE": 700.0,
                    "PX_SETTLE": 699.0,
                    "FAIR_VALUE_SNAPSHOT_AT_1630": 701.0,
                },
                {
                    "security": selected["CO"].ticker,
                    "date": selected["CO"].end_date,
                    "PX_LAST": 80.0,
                    "PX_CLOSE": 79.5,
                    "PX_SETTLE": 79.75,
                    "FAIR_VALUE_SNAPSHOT_AT_1630": None,
                },
                {
                    "security": selected["HO"].ticker,
                    "date": selected["HO"].end_date,
                    "PX_LAST": 200.0,
                    "PX_CLOSE": 199.5,
                    "PX_SETTLE": 199.75,
                    "FAIR_VALUE_SNAPSHOT_AT_1630": 999.0,
                },
            ),
            tuple(selected.values()),
            config.update.fields,
            config.update.reference_depth,
        )

        self.assertFalse(warnings)
        by_root = {
            row["security_prefix"]: row
            for row in frame.select(
                "security_prefix", "PX_LAST", "PX_SETTLE"
            ).iter_rows(named=True)
        }
        self.assertEqual(by_root["QS"]["PX_LAST"], 701.0)
        self.assertEqual(by_root["QS"]["PX_SETTLE"], 699.0)
        self.assertEqual(by_root["CO"]["PX_LAST"], 80.0)
        self.assertEqual(by_root["CO"]["PX_SETTLE"], 79.75)
        self.assertEqual(by_root["HO"]["PX_LAST"], 200.0)
        self.assertEqual(by_root["HO"]["PX_SETTLE"], 199.75)

    def test_one_digit_year_template_still_uses_unambiguous_years(self) -> None:
        write_config(
            self.config_path,
            contract_start_year=2018,
            contract_end_year=2028,
            wu_ticker_template="{root}{month_code}{y} {yellow_key}",
        )
        config = load_root_config(self.config_path)
        specs = build_contract_universe(config, date(2026, 8, 3))

        wu_february = {
            spec.contract_year: spec.ticker
            for spec in specs
            if spec.root == "WU" and spec.month_code == "G"
        }
        self.assertEqual(wu_february[2018], "WUG18 Comdty")
        self.assertEqual(wu_february[2019], "WUG19 Comdty")
        self.assertEqual(wu_february[2028], "WUG28 Comdty")
        self.assertEqual(
            len({spec.ticker.casefold() for spec in specs}),
            len(specs),
        )

    def test_rvo_is_one_monthless_flat_curve_request(self) -> None:
        config = load_root_config(self.config_path)
        specs = [
            spec
            for spec in build_contract_universe(config, date(2026, 8, 3))
            if spec.root == "NAUG008A"
        ]
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.ticker, "NAUG008A Index")
        self.assertEqual(spec.curve_mode, "flat")
        self.assertEqual(spec.month_label, "")
        self.assertIsNone(spec.contract_year)
        self.assertEqual(spec.start_date, date(2024, 1, 1))
        self.assertEqual(spec.end_date, date(2026, 8, 3))

        frame, warnings = normalize_bloomberg_rows(
            (
                {"security": "NAUG008A Index", "date": date(2026, 8, 1), "PX_LAST": 18.1234567, "PX_CLOSE": 18.0, "PX_SETTLE": 18.1, "PX_FAIR_1430": 18.2},
                {"security": "NAUG008A Index", "date": date(2026, 8, 2), "PX_LAST": 19.1234567, "PX_CLOSE": 19.0, "PX_SETTLE": 19.1, "PX_FAIR_1430": 19.2},
            ),
            (spec,),
            config.update.fields,
            reference_depth=2,
        )
        self.assertFalse(warnings)
        self.assertEqual(frame["frequency"].unique().to_list(), ["Flat"])
        self.assertEqual(frame["month"].unique().to_list(), [""])
        self.assertEqual(frame["reference"].unique().to_list(), [1])
        self.assertEqual(frame["PX_LAST"].to_list(), [18.12346, 19.12346])

    def test_reference_is_derived_from_contract_and_observation_dates(self) -> None:
        config = load_root_config(self.config_path)
        spec = next(
            item
            for item in build_contract_universe(config, date(2026, 8, 3))
            if item.ticker == "WUH26 Comdty"
        )
        frame, warnings = normalize_bloomberg_rows(
            (
                {"security": spec.ticker, "date": date(2024, 9, 3), "PX_LAST": 1.1234567, "PX_CLOSE": 1.0, "PX_SETTLE": 1.0, "PX_FAIR_1430": 1.0},
                {"security": spec.ticker, "date": date(2025, 3, 3), "PX_LAST": 2.1234567, "PX_CLOSE": 2.0, "PX_SETTLE": 2.0, "PX_FAIR_1430": 2.0},
            ),
            (spec,),
            config.update.fields,
            reference_depth=2,
        )
        self.assertFalse(warnings)
        self.assertEqual(frame["reference"].to_list(), [2, 1])
        self.assertEqual(frame["PX_LAST"].to_list(), [1.12346, 2.12346])

    def test_precision_validation_accepts_large_values_already_rounded(self) -> None:
        config = load_root_config(self.config_path)
        specs = build_contract_universe(config, date(2026, 8, 3))
        rows = tuple(
            {
                "security": spec.ticker,
                "date": spec.end_date,
                "PX_LAST": 10000.12345,
                "PX_CLOSE": 10000.12345,
                "PX_SETTLE": 10000.12345,
                "PX_FAIR_1430": 10000.12345,
            }
            for spec in specs
        )
        frame, warnings = normalize_bloomberg_rows(
            rows,
            specs,
            config.update.fields,
            reference_depth=config.update.reference_depth,
        )
        self.assertFalse(warnings)
        validate_canonical_frame(frame, config, specs, date(2026, 8, 3))
        self.assertEqual(count_values_over_precision(frame, "PX_LAST", 5), 0)

        malformed = frame.with_row_index("_row").with_columns(
            pl.when(pl.col("_row") == 0)
            .then(pl.lit(10000.123456))
            .otherwise(pl.col("PX_LAST"))
            .alias("PX_LAST")
        ).drop("_row")
        with self.assertRaisesRegex(UpdateError, "1 rows in PX_LAST exceed 5 decimals"):
            validate_canonical_frame(malformed, config, specs, date(2026, 8, 3))

    def test_precision_validation_rejects_a_real_sixth_decimal(self) -> None:
        frame = pl.DataFrame({"PX_LAST": [10000.12345, 10000.123456]})
        self.assertEqual(count_values_over_precision(frame, "PX_LAST", 5), 1)

    def test_publish_validation_rejects_malformed_flat_curve_metadata(self) -> None:
        config = load_root_config(self.config_path)
        specs = build_contract_universe(config, date(2026, 8, 3))
        rows = tuple(
            {
                "security": spec.ticker,
                "date": spec.end_date,
                "PX_LAST": 100.0,
                "PX_CLOSE": 100.0,
                "PX_SETTLE": 100.0,
                "PX_FAIR_1430": 100.0,
            }
            for spec in specs
        )
        frame, warnings = normalize_bloomberg_rows(
            rows,
            specs,
            config.update.fields,
            reference_depth=config.update.reference_depth,
        )
        self.assertFalse(warnings)
        validate_canonical_frame(frame, config, specs, date(2026, 8, 3))

        malformed = frame.with_columns(
            pl.when(pl.col("security_prefix") == "NAUG008A")
            .then(pl.lit("Monthly"))
            .otherwise(pl.col("frequency"))
            .alias("frequency"),
            pl.when(pl.col("security_prefix") == "NAUG008A")
            .then(pl.lit("Jan"))
            .otherwise(pl.col("month"))
            .alias("month"),
            pl.when(pl.col("security_prefix") == "NAUG008A")
            .then(pl.lit("F26"))
            .otherwise(pl.col("contract_month_yr"))
            .alias("contract_month_yr"),
            pl.when(pl.col("security_prefix") == "NAUG008A")
            .then(pl.lit(2026))
            .otherwise(pl.col("contract_year"))
            .alias("contract_year"),
            pl.when(pl.col("security_prefix") == "NAUG008A")
            .then(pl.lit(2))
            .otherwise(pl.col("reference"))
            .alias("reference"),
        )
        extra_flat_security = malformed.filter(pl.col("security_prefix") == "NAUG008A").head(1).with_columns(
            pl.lit("NAUG008AALT Index").alias("security_str")
        )
        malformed = pl.concat([malformed, extra_flat_security], how="vertical_relaxed")

        with self.assertRaises(UpdateError) as context:
            validate_canonical_frame(malformed, config, specs, date(2026, 8, 3))
        message = str(context.exception)
        self.assertIn("NAUG008A flat curve must contain exactly one non-null security", message)
        self.assertIn("NAUG008A flat rows must use frequency Flat", message)
        self.assertIn("NAUG008A flat rows must have a blank month", message)
        self.assertIn("NAUG008A flat rows must have a blank contract_month_yr", message)
        self.assertIn("NAUG008A flat rows must have a null contract_year", message)
        self.assertIn("NAUG008A flat rows must use reference 1", message)

    def test_fake_update_writes_csv_gzip_parquet_html_and_manifest(self) -> None:
        client = FakeClient([], price=10000.12345)
        summary = run_bloomberg_update(
            paths=self.paths,
            client=client,
            as_of=date(2026, 8, 3),
            full=True,
        )
        self.assertTrue(summary["ok"])
        self.assertTrue(client.calls)
        self.assertTrue(self.paths.csv.exists())
        self.assertTrue(self.paths.csv_gzip.exists())
        self.assertTrue(self.paths.parquet.exists())
        self.assertTrue(self.paths.html.exists())
        self.assertTrue(self.paths.embedded_js.exists())
        self.assertTrue(self.paths.manifest.exists())

        csv_frame = pl.read_csv(self.paths.csv, try_parse_dates=True)
        parquet_frame = pl.read_parquet(self.paths.parquet)
        self.assertEqual(csv_frame.height, parquet_frame.height)
        self.assertEqual(set(csv_frame["security_prefix"]), {"WU", "HO", "NAUG008A"})
        self.assertEqual(set(csv_frame["CLEAN_NAME"]), {"GC Jet", "Heating Oil", "RVO"})
        self.assertEqual(set(parquet_frame["CLEAN_NAME"]), {"GC Jet", "Heating Oil", "RVO"})
        self.assertTrue(
            {"PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"}.issubset(
                parquet_frame.columns
            )
        )
        self.assertTrue(
            csv_frame.select((pl.col("PX_LAST") == pl.col("PX_LAST").round(5)).all()).item()
        )
        with gzip.open(self.paths.csv_gzip, "rt", encoding="utf-8") as handle:
            self.assertEqual(handle.readline().strip(), ",".join(csv_frame.columns))

        html = self.paths.html.read_text(encoding="utf-8")
        self.assertEqual(html.count('id="embedded-data"'), 1)
        self.assertNotIn('id="embedded-data-raw"', html)
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["source"], "Bloomberg Desktop API")
        self.assertEqual(manifest["retained_rows"], csv_frame.height)
        self.assertEqual(set(manifest["root_coverage"]), {"WU", "HO", "NAUG008A"})
        self.assertEqual(
            manifest["requested_fields"],
            ["PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"],
        )
        self.assertEqual(manifest["dashboard_fields"], ["PX_LAST", "PX_SETTLE"])
        self.assertEqual(
            manifest["curve_modes"],
            {"HO": "monthly", "NAUG008A": "flat", "WU": "monthly"},
        )
        self.assertEqual(manifest["export"]["fields"], ["PX_LAST", "PX_SETTLE"])
        self.assertEqual(
            manifest["export"]["parquet_fields"],
            ["PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"],
        )

    def test_incremental_update_preserves_expired_history_and_replaces_overlap(self) -> None:
        first = FakeClient([], price=100.000001)
        run_bloomberg_update(
            paths=self.paths,
            client=first,
            as_of=date(2026, 8, 2),
            full=True,
        )
        before = pl.read_csv(self.paths.csv, try_parse_dates=True)
        second = FakeClient([], price=300.999999)
        run_bloomberg_update(
            paths=self.paths,
            client=second,
            as_of=date(2026, 8, 3),
            full=False,
        )
        after = pl.read_csv(self.paths.csv, try_parse_dates=True)
        self.assertGreaterEqual(after.height, before.height)
        requested = {request.security for request in second.calls[0][0]}
        self.assertTrue(requested)
        self.assertTrue(
            all(
                (ticker.startswith("WU") and ticker.endswith("26 Comdty"))
                or (ticker.startswith("HO") and ticker.endswith("6 Index"))
                or ticker == "NAUG008A Index"
                for ticker in requested
            )
        )
        self.assertGreater(float(after["PX_LAST"].max()), 300.0)

    def test_adding_a_product_automatically_forces_a_full_refresh(self) -> None:
        run_bloomberg_update(
            paths=self.paths,
            client=FakeClient([], price=100.0),
            as_of=date(2026, 8, 3),
            full=True,
        )
        workbook = load_workbook(self.config_path)
        try:
            roots = workbook["Security Roots"]
            roots.append(
                (
                    True,
                    "CL",
                    "WTI Crude Oil",
                    "Comdty",
                    "$/bbl",
                    7.33,
                    42,
                    "{root}{month_code}{yy} {yellow_key}",
                    "Monthly",
                    "NYMEX:CL",
                    "WTI",
                    "Crude",
                    4,
                    "",
                    "",
                )
            )
            workbook.save(self.config_path)
        finally:
            workbook.close()

        client = FakeClient([], price=300.0)
        summary = run_bloomberg_update(
            paths=self.paths,
            client=client,
            as_of=date(2026, 8, 3),
            full=False,
        )
        config = load_root_config(self.config_path)
        universe = build_contract_universe(config, date(2026, 8, 3))
        requested = client.calls[0][0]
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))

        self.assertEqual(len(requested), len(universe))
        self.assertEqual({request.security for request in requested}, {spec.ticker for spec in universe})
        self.assertEqual(summary["mode"], "full")
        self.assertEqual(summary["full_refresh_reason"], "Bloomberg pull configuration changed")
        self.assertEqual(manifest["mode"], "full")
        self.assertEqual(
            manifest["full_refresh_reason"],
            "Bloomberg pull configuration changed",
        )

    def test_all_null_optional_prices_remain_numeric_and_pass_csv_parquet_parity(self) -> None:
        summary = run_bloomberg_update(
            paths=self.paths,
            client=FakeClient([], null_optional_fields=True),
            as_of=date(2026, 8, 3),
            full=True,
        )
        self.assertTrue(summary["ok"])
        parquet = pl.read_parquet(self.paths.parquet)
        for field in ("PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"):
            self.assertEqual(parquet.schema[field], pl.Float64)
            self.assertEqual(parquet[field].null_count(), parquet.height)

    def test_late_export_failure_preserves_every_previous_artifact(self) -> None:
        sentinel = b"previous-good-artifact"
        tracked = (
            self.paths.csv,
            self.paths.csv_gzip,
            self.paths.parquet,
            self.paths.html,
            self.paths.embedded_js,
            self.paths.manifest,
        )
        for path in tracked:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(sentinel)
        write_config(self.config_path, max_mb=0.00001)

        with self.assertRaises(Exception):
            run_bloomberg_update(
                paths=self.paths,
                client=FakeClient([]),
                as_of=date(2026, 8, 3),
                full=True,
            )
        for path in tracked:
            self.assertEqual(path.read_bytes(), sentinel)


if __name__ == "__main__":
    unittest.main()
