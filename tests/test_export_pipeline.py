from __future__ import annotations

import base64
import csv
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import polars as pl

from app.export_single_file import _load_dataframe, build_embedded_data, export_dashboard
from app.root_config import ROOT_COLUMNS


class ExportPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_path = self.root / "prices.parquet"
        self.config_path = self.root / "security_roots.csv"
        self.output_path = self.root / "trade_builder.html"
        self.parquet_path = self.root / "pricing_data.parquet"
        self.embedded_js_path = self.root / "embedded_data.js"

        config_rows = [
            [
                True,
                "WU",
                "GC Jet",
                "Comdty",
                "cpg",
                7.45,
                42,
                "{root}{month_code}{yy} {yellow_key}",
                "Monthly",
                "NYMEX:WU1!",
                "ME",
                "Refined Products",
                1,
            ],
            [
                True,
                "HO",
                "Heating Oil",
                "Comdty",
                "cpg",
                7.45,
                42,
                "{root}{month_code}{yy} {yellow_key}",
                "Monthly",
                "NYMEX:HO1!",
                "",
                "Refined Products",
                2,
            ],
        ]
        with self.config_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(ROOT_COLUMNS)
            writer.writerows(config_rows)

        month_codes = ("F", "G", "H", "J", "K", "M")
        month_names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun")
        rows: list[dict[str, object]] = []
        for root_code, source_name in (("WU", "Wrong Jet Name"), ("HO", "Wrong HO Name")):
            for index, (month_code, month_name) in enumerate(zip(month_codes, month_names)):
                observation_date = date(2026, 1, 2 if index < 3 else 3)
                seed = 200.1234567 + index + (10 if root_code == "HO" else 0)
                rows.append(
                    {
                        "date": observation_date,
                        "security_str": f"{root_code}{month_code}26 Comdty",
                        "security_prefix": root_code,
                        "CLEAN_NAME": source_name,
                        "frequency": "Monthly",
                        "reference": 1,
                        "month": month_name,
                        "contract_month_yr": f"{month_name}-26",
                        "contract_year": 2026,
                        "PX_LAST": seed,
                        "PX_CLOSE": seed - 0.1234567,
                        "PX_SETTLE": seed + 0.2345678,
                        "PX_FAIR_1430": seed - 0.3456789,
                        "PX_VOLUME": 1_000 + index,
                        "bbl_per_mt": 99.1234567,
                        "gal_per_bbl": 43.1234567,
                        "VOL_30D": 0.123456789,
                    }
                )
        # The exporter must exclude future observations using the local date.
        rows.append(
            {
                **rows[0],
                "date": date.today() + timedelta(days=1),
                "security_str": "WUN99 Comdty",
                "month": "Jul",
            }
        )
        pl.DataFrame(rows).write_parquet(self.data_path)

        self.template_path = self._write_asset(
            "index.html",
            """<!doctype html><html><body>
<main>Pricing Dashboard</main>
<script src="embedded_data.js"></script>
<script src="theme.js"></script>
<script src="plotly.js"></script>
<script src="trade_math.js"></script>
<script src="app.js"></script>
</body></html>""",
        )
        self.theme_path = self._write_asset("theme.js", "window.THEME_MARKER = true;")
        self.plotly_path = self._write_asset("plotly.js", "window.PLOTLY_MARKER = true;")
        self.trade_math_path = self._write_asset(
            "trade_math.js", "window.TRADE_MATH_MARKER = true;"
        )
        self.app_path = self._write_asset("app.js", "window.APP_MARKER = true;")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_asset(self, name: str, content: str) -> Path:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def _export(self, **overrides) -> dict:
        arguments = {
            "data_path": str(self.data_path),
            "root_config_path": str(self.config_path),
            "output": str(self.output_path),
            "embedded_js_output": str(self.embedded_js_path),
            "compact_parquet_output": str(self.parquet_path),
            "template": str(self.template_path),
            "js": str(self.app_path),
            "trade_math": str(self.trade_math_path),
            "plotly": str(self.plotly_path),
            "theme": str(self.theme_path),
            "built_at": "2026-08-03T12:34:56Z",
        }
        arguments.update(overrides)
        return export_dashboard(**arguments)

    def _payload(self) -> dict:
        html = self.output_path.read_text(encoding="utf-8")
        match = re.search(
            r'<script id="embedded-data" type="application/octet-stream">([^<]+)</script>',
            html,
        )
        self.assertIsNotNone(match)
        return json.loads(gzip.decompress(base64.b64decode(match.group(1))))

    def test_csv_load_retries_a_transient_windows_access_error(self) -> None:
        csv_path = self.root / "prices.csv"
        csv_path.write_text("value\n1\n", encoding="utf-8")
        expected = pl.DataFrame({"value": [1]})

        with (
            patch(
                "app.export_single_file.pl.scan_csv",
                side_effect=[PermissionError("Access is denied. (os error 5)"), expected.lazy()],
            ) as scan_csv,
            patch("app.export_single_file.time.sleep") as sleep,
        ):
            actual = _load_dataframe(str(csv_path))

        self.assertTrue(actual.equals(expected))
        self.assertEqual(scan_csv.call_count, 2)
        sleep.assert_called_once()

    def test_export_uses_config_rounds_every_value_and_keeps_four_fields(self) -> None:
        summary = self._export()
        payload = self._payload()

        self.assertEqual(summary["rows"], 12)
        self.assertEqual(summary["roots"], ["HO", "WU"])
        self.assertEqual(
            summary["fields"],
            ["PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"],
        )
        self.assertFalse(summary["analytics_included"])
        self.assertEqual(payload["meta"]["built_at"], "2026-08-03T12:34:56Z")
        self.assertEqual(payload["meta"]["data_max_date"], "2026-01-03")
        self.assertEqual(payload["meta"]["updated_at"], "2026-01-03T00:00:00Z")
        self.assertEqual(payload["meta"]["fields"]["available"], summary["fields"])
        self.assertEqual(set(payload["meta"]["root_config"]), {"HO", "WU"})
        self.assertEqual(payload["meta"]["root_config"]["WU"]["common_name"], "GC Jet")
        self.assertEqual(payload["meta"]["root_config"]["WU"]["display_name"], "GC Jet")
        self.assertEqual(payload["meta"]["root_config"]["WU"]["native_unit"], "cpg")
        self.assertEqual(payload["meta"]["root_config"]["WU"]["yellow_key"], "Comdty")
        self.assertEqual(payload["meta"]["unit_factors"]["WU"]["bbl_per_mt"], 7.45)
        self.assertEqual(payload["meta"]["unit_factors"]["WU"]["gal_per_bbl"], 42.0)
        self.assertEqual(payload["meta"]["tradingview_symbols"]["WU"], "NYMEX:WU1!")
        self.assertTrue(payload["meta"]["xPool"])
        self.assertTrue(
            any(
                isinstance(series, dict) and "x_ref" in series
                for commodity in payload["commodities"].values()
                for field_map in [commodity["years"], *commodity.get("fields", {}).values()]
                for series in field_map.values()
            )
        )

        for commodity in payload["commodities"].values():
            self.assertIn(commodity["name"], {"GC Jet", "Heating Oil"})
            self.assertNotIn("volumes", commodity)
            self.assertNotIn("volatility_30d", commodity)

        def assert_precision(value: object) -> None:
            if isinstance(value, float):
                self.assertEqual(value, round(value, 5))
            elif isinstance(value, list):
                for item in value:
                    assert_precision(item)
            elif isinstance(value, dict):
                for item in value.values():
                    assert_precision(item)

        assert_precision(payload)

        compact = pl.read_parquet(self.parquet_path)
        self.assertEqual(compact.height, 12)
        self.assertEqual(set(compact["CLEAN_NAME"]), {"GC Jet", "Heating Oil"})
        self.assertEqual(set(compact["bbl_per_mt"]), {7.45})
        self.assertEqual(set(compact["gal_per_bbl"]), {42.0})
        for column_name, dtype in compact.schema.items():
            if dtype in (pl.Float32, pl.Float64):
                self.assertTrue(
                    compact.select((pl.col(column_name) == pl.col(column_name).round(5)).all()).item()
                )

    def test_reference_cycles_use_monotonic_leap_aligned_x_axis(self) -> None:
        rows = (
            (date(2024, 9, 3), 2, 20.0),
            (date(2025, 3, 3), 2, 21.0),
            (date(2025, 9, 3), 1, 10.0),
            (date(2026, 3, 3), 1, 11.0),
        )
        frame = pl.DataFrame(
            {
                "date": [row[0] for row in rows],
                "security_str": ["WUQ26 Comdty"] * len(rows),
                "security_prefix": ["WU"] * len(rows),
                "CLEAN_NAME": ["GC Jet"] * len(rows),
                "frequency": ["Monthly"] * len(rows),
                "reference": [row[1] for row in rows],
                "month": ["Aug"] * len(rows),
                "contract_month_yr": ["Q26"] * len(rows),
                "contract_year": [2026] * len(rows),
                "PX_LAST": [row[2] for row in rows],
            }
        )
        payload = build_embedded_data(
            frame,
            "cpg",
            "date",
            "security_str",
            "PX_LAST",
            "CLEAN_NAME",
            code_col="security_prefix",
            contract_col="contract_month_yr",
            contract_year_col="contract_year",
            month_col="month",
            frequency_col="frequency",
            reference_col="reference",
            px_last_col="PX_LAST",
        )

        def unpack(reference: int) -> tuple[list[int], list[float]]:
            series = payload["commodities"][f"WU::Aug::{reference}"]["years"]["2026"]
            if isinstance(series, list):
                return payload["meta"]["yearX"]["2026"], series
            return payload["meta"]["xPool"][series["x_ref"]], series["y"]

        reference_one_x, reference_one_y = unpack(1)
        reference_two_x, reference_two_y = unpack(2)
        self.assertEqual(payload["meta"]["years"], [2026])
        self.assertEqual(reference_one_x, [247, 429])
        self.assertEqual(reference_two_x, reference_one_x)
        self.assertEqual(reference_one_y, [10.0, 11.0])
        self.assertEqual(reference_two_y, [20.0, 21.0])

    def test_flat_curve_exports_one_monthless_calendar_series(self) -> None:
        frame = pl.DataFrame(
            {
                "date": [date(2025, 12, 31), date(2026, 1, 2)],
                "security_str": ["RVO Index", "RVO Index"],
                "security_prefix": ["RVO", "RVO"],
                "CLEAN_NAME": ["RVO", "RVO"],
                "frequency": ["Flat", "Flat"],
                "reference": [1, 1],
                "month": ["", ""],
                "contract_month_yr": ["", ""],
                "contract_year": [None, None],
                "PX_LAST": [18.1234567, 19.7654321],
            }
        )
        payload = build_embedded_data(
            frame,
            "cpg",
            "date",
            "security_str",
            "PX_LAST",
            "CLEAN_NAME",
            code_col="security_prefix",
            contract_col="contract_month_yr",
            contract_year_col="contract_year",
            month_col="month",
            frequency_col="frequency",
            reference_col="reference",
            px_last_col="PX_LAST",
            root_config_by_code={
                "RVO": {
                    "root": "RVO",
                    "display_name": "RVO",
                    "curve_mode": "flat",
                    "native_unit": "cpg",
                    "bbl_per_mt": 7.45,
                    "gal_per_bbl": 42,
                }
            },
        )

        self.assertEqual(len(payload["commodities"]), 1)
        commodity = next(iter(payload["commodities"].values()))
        self.assertEqual(commodity["contract_month"], "")
        self.assertEqual(commodity["frequency"], "Flat")
        self.assertEqual(set(commodity["years"]), {"2025", "2026"})
        self.assertEqual(payload["meta"]["root_config"]["RVO"]["curve_mode"], "flat")

    def test_standalone_and_external_js_contain_one_compressed_payload(self) -> None:
        summary = self._export()
        html = self.output_path.read_text(encoding="utf-8")
        external_js = self.embedded_js_path.read_text(encoding="utf-8")

        self.assertLess(summary["output_mb"], 20)
        self.assertEqual(html.count('id="embedded-data"'), 1)
        self.assertNotIn('id="embedded-data-raw"', html)
        self.assertNotIn("|| {{}}", html)
        self.assertNotIn("<script src=", html)
        self.assertIn("window.DASHBOARD_OWNER_UPDATE_API = false;", html)
        self.assertIn("DecompressionStream", html)
        self.assertIn("payloadElement.remove()", html)
        self.assertIn("meta?.xPool", html)
        self.assertLess(html.index("TRADE_MATH_MARKER"), html.index("APP_MARKER"))
        self.assertIn("DecompressionStream", external_js)
        self.assertNotIn('"commodities":', external_js)

    def test_explicit_field_selection_and_analytics_opt_in(self) -> None:
        summary = self._export(fields="PX_LAST,PX_SETTLE", include_analytics=True)
        payload = self._payload()

        self.assertEqual(summary["fields"], ["PX_LAST", "PX_SETTLE"])
        self.assertTrue(summary["analytics_included"])
        self.assertEqual(payload["meta"]["fields"]["available"], ["PX_LAST", "PX_SETTLE"])
        self.assertTrue(any("volumes" in item for item in payload["commodities"].values()))
        self.assertTrue(
            any("volatility_30d" in item for item in payload["commodities"].values())
        )
        compact = pl.read_parquet(self.parquet_path)
        self.assertNotIn("PX_CLOSE", compact.columns)
        self.assertNotIn("PX_FAIR_1430", compact.columns)

    def test_parquet_can_retain_full_fields_while_dashboard_stays_lean(self) -> None:
        full_fields = "PX_LAST,PX_CLOSE,PX_SETTLE,PX_FAIR_1430"
        summary = self._export(fields="PX_LAST", parquet_fields=full_fields)
        payload = self._payload()
        compact = pl.read_parquet(self.parquet_path)

        self.assertEqual(summary["fields"], ["PX_LAST"])
        self.assertEqual(
            summary["parquet_fields"],
            ["PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"],
        )
        self.assertEqual(payload["meta"]["fields"]["available"], ["PX_LAST"])
        self.assertTrue(
            {"PX_LAST", "PX_CLOSE", "PX_SETTLE", "PX_FAIR_1430"}.issubset(
                compact.columns
            )
        )

    def test_unconfigured_root_and_size_budget_fail_actionably(self) -> None:
        unconfigured = pl.read_parquet(self.data_path).with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(pl.lit("WUX"))
            .otherwise(pl.col("security_prefix"))
            .alias("security_prefix")
        )
        unconfigured.write_parquet(self.data_path)
        with self.assertRaisesRegex(ValueError, "roots with no configuration: WUX"):
            self._export()

        # Restore valid input and verify the output budget is an enforced guardrail.
        unconfigured.filter(pl.col("security_prefix") != "WUX").write_parquet(self.data_path)
        with self.assertRaisesRegex(ValueError, "above the .* MB budget"):
            self._export(max_output_mb=0.0001)


if __name__ == "__main__":
    unittest.main()
