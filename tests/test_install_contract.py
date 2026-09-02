from __future__ import annotations

from pathlib import Path
import unittest

from scripts.check_runtime_compatibility import SUPPORTED_PYTHON
from scripts.build_dashboard import DEFAULT_DATA


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_BLOOMBERG_INDEX = (
    "https://blpapi.bloomberg.com/repository/releases/python/simple"
)


class InstallContractTests(unittest.TestCase):
    def test_owner_runtime_supports_python_312_and_313(self) -> None:
        self.assertEqual(SUPPORTED_PYTHON, ((3, 12), (3, 13)))

    def test_windows_installer_uses_the_dedicated_bloomberg_index(self) -> None:
        bootstrap = (PROJECT_ROOT / "scripts" / "run_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(OFFICIAL_BLOOMBERG_INDEX + "/", bootstrap)
        self.assertIn(
            '& $VenvPython -m pip install "--index-url=$BloombergIndexUrl" blpapi',
            bootstrap,
        )
        self.assertIn("python -m pip install --index-url=", bootstrap)
        self.assertNotIn("--index-url=https://bloomberg.com ", bootstrap)

    def test_windows_environment_is_user_local_and_never_repo_local(self) -> None:
        bootstrap = (PROJECT_ROOT / "scripts" / "run_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('Join-Path $env:USERPROFILE "Pyenvs"', bootstrap)
        self.assertIn('$VenvPath = Join-Path $VenvRoot "trade_builder"', bootstrap)
        self.assertIn('. $ActivateScript', bootstrap)
        self.assertIn('$env:VIRTUAL_ENV = $VenvPath', bootstrap)
        self.assertNotIn('Join-Path $RepoRoot ".venv"', bootstrap)

    def test_launcher_repairs_a_missing_or_incomplete_environment(self) -> None:
        launcher = (PROJECT_ROOT / "UPDATE_AND_OPEN.bat").read_text(encoding="utf-8")
        installer = (PROJECT_ROOT / "INSTALL_BLOOMBERG.bat").read_text(encoding="utf-8")
        bootstrap = (PROJECT_ROOT / "scripts" / "run_windows.ps1").read_text(
            encoding="utf-8"
        )
        technical_bootstrap = (
            PROJECT_ROOT / "scripts" / "run_technical_windows.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts\\run_windows.ps1", launcher)
        self.assertIn("scripts\\run_technical_windows.ps1", installer)
        self.assertIn("-Mode live", installer)
        self.assertIn("-InstallOnly", installer)
        self.assertIn("[switch]$InstallOnly", technical_bootstrap)
        self.assertIn("[switch]$PreflightOnly", technical_bootstrap)
        self.assertIn("scripts\\preflight_bloomberg.py", technical_bootstrap)
        self.assertIn('"bbg_technical_builder"', technical_bootstrap)
        self.assertIn(
            "Bloomberg technical environment is installed and verified",
            technical_bootstrap,
        )
        self.assertIn("-m ensurepip --upgrade", bootstrap)
        self.assertIn("Test-ManagedDependencies", bootstrap)
        self.assertIn("function Ensure-EmbeddedDashboardData", bootstrap)
        self.assertIn("& $VenvPython $BuildDashboardPath", bootstrap)
        self.assertIn("Ensure-EmbeddedDashboardData", bootstrap)
        self.assertNotIn(".venv", launcher)
        self.assertNotIn("py -3", launcher)
        self.assertIn(
            '& $VenvPython (Join-Path $RepoRoot "scripts\\run_dashboard.py") --open',
            bootstrap,
        )

    def test_windows_launchers_update_and_publish_the_standalone_export(self) -> None:
        launcher = (PROJECT_ROOT / "UPDATE_AND_OPEN.bat").read_text(encoding="utf-8")
        updater = (PROJECT_ROOT / "UPDATE_AND_EXPORT.bat").read_text(encoding="utf-8")
        bootstrap = (PROJECT_ROOT / "scripts" / "run_windows.ps1").read_text(
            encoding="utf-8"
        )
        default_directory = (
            "%USERPROFILE%\\OneDrive - Energy Transfer\\Trading Analytics - Documents"
            "\\General\\Disty Analytics\\Trade_Builder"
        )

        self.assertIn('set "EXPORT_DIRECTORY=%~1"', launcher)
        self.assertIn(default_directory, launcher)
        self.assertIn('-ExportDirectory "%EXPORT_DIRECTORY%"', launcher)
        self.assertIn('set "EXPORT_DIRECTORY=%~1"', updater)
        self.assertIn('cd /d "%~dp0"', updater)
        self.assertIn(default_directory, updater)
        self.assertIn("-UpdateOnly", updater)
        self.assertIn('-ExportDirectory "%EXPORT_DIRECTORY%"', updater)
        self.assertNotIn("pause", updater.lower())
        self.assertIn("$env:TRADE_BUILDER_PYTHON", bootstrap)
        self.assertIn('$env:LOCALAPPDATA', bootstrap)
        self.assertIn('"Python313", "Python312"', bootstrap)
        self.assertIn("[switch]$UpdateOnly", bootstrap)
        self.assertIn("function Update-BloombergData", bootstrap)
        self.assertIn(
            "& $VenvPython $BloombergUpdatePath --config $RootConfigPath",
            bootstrap,
        )
        self.assertIn("function Publish-StandaloneDashboard", bootstrap)
        self.assertIn("function Copy-StandaloneExportWithRetry", bootstrap)
        self.assertIn("Copy-Item -LiteralPath $StandaloneDashboardPath", bootstrap)
        self.assertIn(
            "Copy-Item -LiteralPath $TemporaryPath -Destination $DestinationPath -Force",
            bootstrap,
        )
        self.assertIn("Could not replace the SharePoint export after 5 attempts", bootstrap)
        self.assertNotIn("[System.IO.File]::Replace", bootstrap)
        self.assertIn("no stale export was republished", bootstrap)
        self.assertIn("if ($BloombergUpdateSucceeded)", bootstrap)
        self.assertIn("if ($UpdateOnly)", bootstrap)
        self.assertIn("function Test-ExistingDashboardServer", bootstrap)
        self.assertIn("Pricing Dashboard owner server already running", bootstrap)
        self.assertLess(
            bootstrap.rindex("\nUpdate-BloombergData\n"),
            bootstrap.rindex("\nif ($UpdateOnly) {\n"),
        )
        self.assertLess(
            bootstrap.rindex("\nif ($UpdateOnly) {\n"),
            bootstrap.rindex("\nif (Test-ExistingDashboardServer) {\n"),
        )

    def test_windows_launcher_does_not_require_optional_sample_data(self) -> None:
        bootstrap = (PROJECT_ROOT / "scripts" / "run_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '$PricingHistoryPath = Join-Path $RepoRoot "data\\pricing_history.csv"',
            bootstrap,
        )
        self.assertIn(
            "if (Test-Path -LiteralPath $PricingHistoryPath)",
            bootstrap,
        )
        self.assertIn(
            "elseif (Test-Path -LiteralPath $SampleDataPath)",
            bootstrap,
        )
        required_paths = bootstrap.split("foreach ($RequiredPath in @(", 1)[1].split(
            ")) {", 1
        )[0]
        self.assertNotIn("$SampleDataPath", required_paths)

    def test_dashboard_rebuild_defaults_to_bloomberg_history(self) -> None:
        self.assertEqual(DEFAULT_DATA, PROJECT_ROOT / "data" / "pricing_history.csv")

    def test_windows_launcher_rebuilds_after_frontend_changes(self) -> None:
        bootstrap = (PROJECT_ROOT / "scripts" / "run_windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$StandaloneDashboardPath", bootstrap)
        self.assertIn("$DashboardTemplatePath", bootstrap)
        self.assertIn("$DashboardAppPath", bootstrap)
        self.assertIn("$DashboardMathPath", bootstrap)
        self.assertIn("$DashboardThemePath", bootstrap)
        self.assertIn("$DashboardPlotlyPath", bootstrap)
        self.assertIn("$DashboardExporterPath", bootstrap)
        self.assertIn("$OldestOutputTimestamp", bootstrap)

    def test_frontend_uses_one_update_date_and_prebuilt_crack_contracts(self) -> None:
        app = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        template = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("Created &amp; Maintained by Alex Hoffmann", template)
        self.assertNotIn("Age:", template)
        self.assertEqual(template.count('id="data-last-update"'), 1)
        self.assertNotIn('id="last-updated"', template)
        self.assertNotIn('id="pricing-updated"', template)
        self.assertIn("box: { legs: 4, ratios: [1, -1, -1, 1]", app)
        self.assertNotIn("id: 'rbob_ho_brent'", app)
        self.assertNotIn("id: 'usgc_321'", app)
        self.assertIn("id: 'jet_crack'", app)
        self.assertIn("label: 'USGC 54 Crack'", app)
        self.assertIn("formula: 'GC 54 Swap + next-month HO - next-month WTI'", app)
        self.assertIn("formula: 'USGC 62 Swap + next-month HO - next-month WTI'", app)
        self.assertIn("{ code: 'UDS', ratio: 1, monthOffset: 0 }", app)
        self.assertIn("state.unit = current.unit || '$/bbl';", app)

    def test_requirement_ranges_cover_verified_packages(self) -> None:
        base = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        bloomberg = (PROJECT_ROOT / "requirements-bloomberg.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("polars>=1.38,<2", base)
        self.assertIn("numpy==2.5.2", base)
        self.assertIn("openpyxl==3.1.5", base)
        self.assertIn("reportlab==5.0.1", base)
        self.assertIn("pypdf==6.16.2", base)
        self.assertIn("tzdata==2026.3", base)
        self.assertIn("blpapi>=3.25,<4", bloomberg)
        self.assertIn(OFFICIAL_BLOOMBERG_INDEX, bloomberg)

    def test_technical_runtime_is_pandas_free_and_user_local(self) -> None:
        requirements = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).lower()
        launcher = (
            PROJECT_ROOT / "scripts" / "run_technical_windows.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("pandas", requirements)
        self.assertIn("tzdata==2026.3", requirements)
        self.assertNotIn("scikit-learn", requirements)
        self.assertNotIn("scipy", requirements)
        self.assertNotIn("polars-ta==", requirements)
        self.assertIn('Join-Path $env:USERPROFILE "Pyenvs"', launcher)
        self.assertIn('"bbg_technical_builder"', launcher)
        self.assertIn("Repair-ManagedPip", launcher)
        self.assertIn("Test-ManagedPython", launcher)
        self.assertIn("struct.calcsize('P') == 8", launcher)
        self.assertIn("-m pip check", launcher)
        self.assertIn('[ValidateSet("auto", "train", "score")]', launcher)
        self.assertIn("Existing Bloomberg BLPAPI runtime is compatible", launcher)
        self.assertIn("Restore-PreviousModel", launcher)
        self.assertIn("Restore-ReleaseArtifacts", launcher)
        self.assertIn("New-ReleaseBackup", launcher)
        self.assertIn('".complete"', launcher)
        self.assertIn('"present.txt"', launcher)
        self.assertIn("$ReleaseBackupStaging", launcher)
        self.assertIn("Move-Item -LiteralPath $ReleaseBackupStaging", launcher)
        self.assertIn('dist\\technical_run_manifest.json', launcher)
        self.assertNotIn('data\\technical\\technical_run_manifest.json', launcher)

    def test_bloomberg_preflight_exercises_required_live_surfaces(self) -> None:
        batch = (PROJECT_ROOT / "CHECK_BLOOMBERG_READY.bat").read_text(
            encoding="utf-8"
        )
        preflight = (PROJECT_ROOT / "scripts" / "preflight_bloomberg.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("-PreflightOnly", batch)
        self.assertIn("fetch_reference", preflight)
        self.assertIn("fetch_daily", preflight)
        self.assertIn("fetch_intraday", preflight)
        self.assertIn("capture_liquidity", preflight)
        self.assertIn("bloomberg_preflight.json", preflight)
        self.assertIn('"daily_checks"', preflight)
        self.assertIn('"intraday_checks"', preflight)
        self.assertIn('"blpapi_cpp"', preflight)
        self.assertIn("xbbg.get_sdk_info", preflight)

    def test_product_pdf_is_an_atomic_windows_release_surface(self) -> None:
        launcher = (PROJECT_ROOT / "EXPORT_TECHNICAL_PDF.bat").read_text(
            encoding="utf-8"
        )
        setup = (PROJECT_ROOT / "SETUP_AND_CHECK_BLOOMBERG.bat").read_text(
            encoding="utf-8"
        )
        bootstrap = (
            PROJECT_ROOT / "scripts" / "run_technical_windows.ps1"
        ).read_text(encoding="utf-8")
        validator = (
            PROJECT_ROOT / "scripts" / "validate_technical_release.py"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts\\build_technical_pdf.py", launcher)
        self.assertIn("output\\pdf\\Technical_Product_Report.pdf", launcher)
        self.assertIn("$PdfBuilderPath", bootstrap)
        self.assertIn("$ProductPdfPath", bootstrap)
        self.assertIn("Product PDF export failed", bootstrap)
        self.assertIn("$ProductPdfPath", bootstrap.split("$ReleaseArtifacts = @(", 1)[1])
        self.assertIn("PdfReader", validator)
        self.assertIn("product-split PDF report", validator)
        self.assertIn("call INSTALL_BLOOMBERG.bat", setup)
        self.assertIn("call CHECK_BLOOMBERG_READY.bat", setup)

    def test_training_launcher_accepts_a_licensed_backfill_path(self) -> None:
        launcher = (PROJECT_ROOT / "TRAIN_AND_SCORE.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn('set "BACKFILL=%~1"', launcher)
        self.assertIn('-Backfill "%BACKFILL%"', launcher)

    def test_model_candidate_is_promoted_after_reporting(self) -> None:
        pipeline = (PROJECT_ROOT / "app" / "technical_pipeline.py").read_text(
            encoding="utf-8"
        )
        training = pipeline.split("def _score_and_report", 1)[0]
        self.assertLess(
            training.rindex("write_technical_outputs("),
            training.rindex("write_model_artifact("),
        )

    def test_optional_kronos_is_isolated_and_fail_closed(self) -> None:
        requirements = (PROJECT_ROOT / "requirements-kronos.txt").read_text(
            encoding="utf-8"
        ).lower()
        config = (PROJECT_ROOT / "config" / "kronos.toml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("pandas", requirements)
        self.assertIn("torch", requirements)
        self.assertIn("enabled = false", config)
        self.assertIn("action_enabled = false", config)
        self.assertIn("minimum_evaluation_sessions = 30", config)


if __name__ == "__main__":
    unittest.main()
