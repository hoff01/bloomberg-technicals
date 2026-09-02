[CmdletBinding()]
param(
    [ValidateSet("live", "demo")]
    [string]$Mode = "live",
    [ValidateSet("auto", "train", "score")]
    [string]$Workflow = "auto",
    [string]$Backfill = "",
    [switch]$InstallOnly,
    [switch]$PreflightOnly,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"
$BloombergIndexUrl = "https://blpapi.bloomberg.com/repository/releases/python/simple/"
$RunnerPath = Join-Path $RepoRoot "scripts\run_technical_system.py"
$CompatibilityPath = Join-Path $RepoRoot "scripts\check_runtime_compatibility.py"
$ValidatorPath = Join-Path $RepoRoot "scripts\validate_technical_release.py"
$WorkbookBuilderPath = Join-Path $RepoRoot "scripts\build_technical_workbook.py"
$PdfBuilderPath = Join-Path $RepoRoot "scripts\build_technical_pdf.py"
$ProductPdfPath = Join-Path $RepoRoot "output\pdf\Technical_Product_Report.pdf"
$TradeCsvPath = Join-Path $RepoRoot "output\csv\Technical_Trade_Levels.csv"
$PreflightPath = Join-Path $RepoRoot "scripts\preflight_bloomberg.py"
$ReleasePrunerPath = Join-Path $RepoRoot "scripts\prune_model_releases.py"
$LogRoot = Join-Path $RepoRoot "logs"
$RunStamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogRoot "technical_${Mode}_${Workflow}_${RunStamp}.log"

if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not set; the managed technical-system environment cannot be located."
}
$VenvRoot = Join-Path $env:USERPROFILE "Pyenvs"
$VenvPath = Join-Path $VenvRoot "bbg_technical_builder"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$SetupStamp = Join-Path $VenvPath ".technical-requirements.sha256"
$ModelArtifact = Join-Path $RepoRoot "models\$Mode\latest_model.json"

function Get-CompatiblePython {
    $Candidates = @()
    if ($env:BBG_TECHNICAL_PYTHON) {
        $Candidates += [PSCustomObject]@{Command=$env:BBG_TECHNICAL_PYTHON; Prefix=@(); Label="BBG_TECHNICAL_PYTHON"}
    }
    foreach ($VersionFolder in @("Python313", "Python312")) {
        foreach ($InstallRoot in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
            if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
                $Candidates += [PSCustomObject]@{Command=(Join-Path $InstallRoot "Programs\Python\$VersionFolder\python.exe"); Prefix=@(); Label=$VersionFolder}
                $Candidates += [PSCustomObject]@{Command=(Join-Path $InstallRoot "$VersionFolder\python.exe"); Prefix=@(); Label=$VersionFolder}
            }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in @("3.13", "3.12")) {
            $Candidates += [PSCustomObject]@{Command="py"; Prefix=@("-$Version"); Label="py -$Version"}
        }
    }
    foreach ($CommandName in @("python", "python3")) {
        $Resolved = Get-Command $CommandName -ErrorAction SilentlyContinue
        if ($null -ne $Resolved) {
            $Candidates += [PSCustomObject]@{Command=$Resolved.Source; Prefix=@(); Label=$CommandName}
        }
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate.Command -ne "py" -and -not (Test-Path -LiteralPath $Candidate.Command)) { continue }
        try {
            $VersionCode = "import struct, sys; raise SystemExit(0 if sys.version_info[:2] in {(3,12),(3,13)} and struct.calcsize('P') == 8 else 1)"
            & $Candidate.Command @($Candidate.Prefix) -c $VersionCode *> $null
            if ($LASTEXITCODE -eq 0) { return $Candidate }
        } catch { continue }
    }
    return $null
}

function Test-ManagedPython {
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) { return $false }
    & $VenvPython -c "import struct, sys; raise SystemExit(0 if sys.version_info[:2] in {(3,12),(3,13)} and struct.calcsize('P') == 8 else 1)" *> $null
    return $LASTEXITCODE -eq 0
}

function Remove-ManagedEnvironment {
    if (-not (Test-Path -LiteralPath $VenvPath)) { return }
    $ExpectedParent = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE "Pyenvs"))
    $ResolvedVenv = [System.IO.Path]::GetFullPath($VenvPath)
    if (-not $ResolvedVenv.StartsWith($ExpectedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to rebuild an environment outside $ExpectedParent"
    }
    Remove-Item -LiteralPath $ResolvedVenv -Recurse -Force
}

function New-ManagedEnvironment {
    param([Parameter(Mandatory=$true)]$BasePython)
    New-Item -ItemType Directory -Path $VenvRoot -Force | Out-Null
    Remove-Item -Force $SetupStamp -ErrorAction SilentlyContinue
    Write-Host "Creating managed environment: $VenvPath"
    & $BasePython.Command @($BasePython.Prefix) -m venv $VenvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Could not create $VenvPath"
    }
}

function Repair-ManagedPip {
    & $VenvPython -m pip --version *> $null
    if ($LASTEXITCODE -eq 0) { return }

    & $VenvPython -m ensurepip --upgrade
    $EnsureExit = $LASTEXITCODE
    & $VenvPython -m pip --version *> $null
    if ($EnsureExit -eq 0 -and $LASTEXITCODE -eq 0) { return }

    Remove-Item -Force $SetupStamp -ErrorAction SilentlyContinue
    Write-Warning "Managed pip is damaged; rebuilding only $VenvPath"
    Remove-ManagedEnvironment
    $BasePython = Get-CompatiblePython
    if ($null -eq $BasePython) {
        throw "Python 3.12 or 3.13 (64-bit) was not found for environment repair."
    }
    New-ManagedEnvironment -BasePython $BasePython
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw "pip repair failed after rebuilding $VenvPath" }
    & $VenvPython -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) { throw "pip is unavailable after rebuilding $VenvPath" }
}

foreach ($RequiredPath in @($RequirementsPath, $RunnerPath, $CompatibilityPath, $ValidatorPath, $WorkbookBuilderPath, $PdfBuilderPath, $PreflightPath, $ReleasePrunerPath)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file was not found: $RequiredPath"
    }
}
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
Write-Host "Run log: $LogPath"

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    $BasePython = Get-CompatiblePython
    if ($null -eq $BasePython) {
        throw "Python 3.12 or 3.13 (64-bit) was not found. Install it from python.org and rerun."
    }
    New-ManagedEnvironment -BasePython $BasePython
} elseif (-not (Test-ManagedPython)) {
    Write-Warning "Managed environment is not a supported 64-bit Python 3.12/3.13 runtime; rebuilding it."
    Remove-ManagedEnvironment
    $BasePython = Get-CompatiblePython
    if ($null -eq $BasePython) {
        throw "Python 3.12 or 3.13 (64-bit) was not found. Install it from python.org and rerun."
    }
    New-ManagedEnvironment -BasePython $BasePython
}

Repair-ManagedPip

$RequirementsHash = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash
$InstalledHash = if (Test-Path -LiteralPath $SetupStamp) {(Get-Content -LiteralPath $SetupStamp -Raw).Trim()} else {""}
$NeedsBaseInstall = $RequirementsHash -ne $InstalledHash
if (-not $NeedsBaseInstall) {
    & $VenvPython -c "from zoneinfo import ZoneInfo; import numpy, openpyxl, polars, polars_talis, pypdf, pyarrow, reportlab, xbbg; ZoneInfo('America/New_York')" *> $null
    $NeedsBaseInstall = $LASTEXITCODE -ne 0
}
if ($NeedsBaseInstall) {
    Write-Host "Installing pinned XBBG, Polars, TA, and reporting dependencies..."
    & $VenvPython -m pip install --disable-pip-version-check --upgrade -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }
    # Remove legacy audit-only packages that pulled Pandas/Numba/SciPy into the
    # operating environment. Native grouped Polars remains authoritative.
    & $VenvPython -m pip uninstall --yes polars-ta pandas scipy scikit-learn numba llvmlite polars-ols
    if ($LASTEXITCODE -ne 0) { throw "Legacy dependency cleanup failed." }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Installed Python dependencies are inconsistent." }
    Set-Content -LiteralPath $SetupStamp -Value $RequirementsHash -Encoding ASCII
}

if ($Mode -eq "live") {
    & $VenvPython -c "import blpapi; raise SystemExit(0 if 3 <= int(blpapi.__version__.split('.')[0]) < 4 else 1)" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing Bloomberg BLPAPI from the official package index..."
        # Official Bloomberg command: python -m pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
        & $VenvPython -m pip install --disable-pip-version-check "--index-url=$BloombergIndexUrl" "blpapi>=3.25,<4"
        if ($LASTEXITCODE -ne 0) { throw "Bloomberg BLPAPI installation failed from $BloombergIndexUrl" }
    } else {
        Write-Host "Existing Bloomberg BLPAPI runtime is compatible; package-index check skipped."
    }
    & $VenvPython -c "import xbbg; xbbg.configure(host='localhost', port=8194, request_pool_size=2, request_timeout_ms=120000, retry_max_retries=1); xbbg.set_backend('polars'); print('Bloomberg/XBBG native runtime ready')"
    if ($LASTEXITCODE -ne 0) {
        throw "Bloomberg SDK could not load. Keep Terminal open and confirm the 64-bit Bloomberg API is installed."
    }
    & $VenvPython $CompatibilityPath 2>&1 | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) { throw "Runtime compatibility preflight failed. See $LogPath" }
} else {
    & $VenvPython -c "import polars, polars_talis, xbbg; print('Demo Polars analytics runtime ready')" 2>&1 | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) { throw "Demo runtime preflight failed. See $LogPath" }
}

if ($InstallOnly) {
    Write-Host "Bloomberg technical environment is installed and verified: $VenvPath"
    exit 0
}

if ($PreflightOnly) {
    & $VenvPython $PreflightPath 2>&1 | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "Bloomberg live preflight failed. See $LogPath"
    }
    Write-Host "Bloomberg live preflight completed successfully."
    exit 0
}

$EffectiveWorkflow = $Workflow
if ($Workflow -eq "auto") {
    $EffectiveWorkflow = if (Test-Path -LiteralPath $ModelArtifact -PathType Leaf) {"score"} else {"train"}
}
if ($EffectiveWorkflow -eq "score" -and -not (Test-Path -LiteralPath $ModelArtifact -PathType Leaf)) {
    throw "Frozen model not found: $ModelArtifact. Run TRAIN_AND_SCORE.bat first."
}
Write-Host "Workflow: $EffectiveWorkflow"

$ModelBackup = Join-Path (Split-Path -Parent $ModelArtifact) ".latest_model.pre_run_backup.json"
$LastKnownGoodModel = Join-Path (Split-Path -Parent $ModelArtifact) "last_known_good_model.json"
$ReleaseBackupRoot = Join-Path (Split-Path -Parent $ModelArtifact) ".pre_run_release_backup"
$ReleaseBackupStaging = "$ReleaseBackupRoot.staging"
$ReleaseArtifacts = @(
    (Join-Path $RepoRoot "dist\technical_live_signals.csv"),
    (Join-Path $RepoRoot "dist\technical_strategy_scorecard.csv"),
    (Join-Path $RepoRoot "dist\technical_backtest_trades.csv"),
    (Join-Path $RepoRoot "dist\technical_fold_metrics.csv"),
    (Join-Path $RepoRoot "dist\technical_strategy_library.csv"),
    (Join-Path $RepoRoot "dist\technical_spread_library.csv"),
    (Join-Path $RepoRoot "dist\technical_spread_legs.csv"),
    (Join-Path $RepoRoot "dist\technical_current_indicators.csv"),
    (Join-Path $RepoRoot "dist\technical_parameter_catalog.csv"),
    (Join-Path $RepoRoot "dist\technical_daily_spread_history.csv"),
    (Join-Path $RepoRoot "dist\technical_expiry_calendar.csv"),
    (Join-Path $RepoRoot "dist\technical_data_quality.csv"),
    (Join-Path $RepoRoot "dist\technical_indicator_library_audit.csv"),
    (Join-Path $RepoRoot "dist\technical_adaptive_weight_history.csv"),
    (Join-Path $RepoRoot "dist\technical_seasonality_profiles.csv"),
    (Join-Path $RepoRoot "dist\technical_structure_coverage.csv"),
    (Join-Path $RepoRoot "dist\technical_structure_summaries.csv"),
    (Join-Path $RepoRoot "dist\technical_model_summary.json"),
    (Join-Path $RepoRoot "dist\technical_daily_settle_spreads.parquet"),
    (Join-Path $RepoRoot "dist\technical_features.parquet"),
    (Join-Path $RepoRoot "dist\technical_signal_dashboard.html"),
    (Join-Path $RepoRoot "dist\technical_run_summary.json"),
    (Join-Path $RepoRoot "dist\technical_run_manifest.json"),
    (Join-Path $RepoRoot "dist\technical_scoring_manifest.json"),
    (Join-Path $RepoRoot "dist\technical_backtest_windows.csv"),
    (Join-Path $RepoRoot "dist\technical_portfolio_lockbox_trades.csv"),
    (Join-Path $RepoRoot "Technical_Trading_System.xlsx"),
    $ProductPdfPath,
    $TradeCsvPath
)
$HadPreviousModel = $false

function Restore-ReleaseArtifacts {
    if ($EffectiveWorkflow -ne "train") { return }
    if (-not (Test-Path -LiteralPath $ReleaseBackupRoot -PathType Container)) { return }
    $CompleteMarker = Join-Path $ReleaseBackupRoot ".complete"
    $PresentFile = Join-Path $ReleaseBackupRoot "present.txt"
    if (
        -not (Test-Path -LiteralPath $CompleteMarker -PathType Leaf) -or
        -not (Test-Path -LiteralPath $PresentFile -PathType Leaf)
    ) {
        throw "Refusing to restore an incomplete release backup: $ReleaseBackupRoot"
    }
    $PresentNames = @(Get-Content -LiteralPath $PresentFile | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($Target in $ReleaseArtifacts) {
        $Name = [System.IO.Path]::GetFileName($Target)
        $Backup = Join-Path $ReleaseBackupRoot $Name
        if ($PresentNames -contains $Name) {
            if (-not (Test-Path -LiteralPath $Backup -PathType Leaf)) {
                throw "Complete release backup is missing $Name"
            }
            New-Item -ItemType Directory -Path (Split-Path -Parent $Target) -Force | Out-Null
            Copy-Item -LiteralPath $Backup -Destination $Target -Force
        } elseif (Test-Path -LiteralPath $Target -PathType Leaf) {
            Remove-Item -LiteralPath $Target -Force
        }
    }
    Write-Warning "Restored the prior model-dependent release artifacts."
}

function Test-CompleteReleaseBackup {
    return (
        (Test-Path -LiteralPath (Join-Path $ReleaseBackupRoot ".complete") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $ReleaseBackupRoot "present.txt") -PathType Leaf)
    )
}

function New-ReleaseBackup {
    Remove-Item -LiteralPath $ReleaseBackupStaging -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $ReleaseBackupStaging -Force | Out-Null
    $PresentNames = @()
    foreach ($Source in $ReleaseArtifacts) {
        if (Test-Path -LiteralPath $Source -PathType Leaf) {
            $Name = [System.IO.Path]::GetFileName($Source)
            Copy-Item -LiteralPath $Source -Destination (Join-Path $ReleaseBackupStaging $Name) -Force
            $PresentNames += $Name
        }
    }
    Set-Content -LiteralPath (Join-Path $ReleaseBackupStaging "present.txt") -Value ($PresentNames -join "`r`n") -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $ReleaseBackupStaging ".complete") -Value "complete" -Encoding ASCII
    Move-Item -LiteralPath $ReleaseBackupStaging -Destination $ReleaseBackupRoot
}

if ($EffectiveWorkflow -eq "train") {
    $HasCompleteReleaseBackup = Test-CompleteReleaseBackup
    if (Test-Path -LiteralPath $ModelBackup -PathType Leaf) {
        Copy-Item -LiteralPath $ModelBackup -Destination $ModelArtifact -Force
        $HadPreviousModel = $true
        Write-Warning "Recovered the prior model from an interrupted training run."
    } elseif ($HasCompleteReleaseBackup) {
        if (Test-Path -LiteralPath $LastKnownGoodModel -PathType Leaf) {
            Copy-Item -LiteralPath $LastKnownGoodModel -Destination $ModelArtifact -Force
            Copy-Item -LiteralPath $LastKnownGoodModel -Destination $ModelBackup -Force
            $HadPreviousModel = $true
            Write-Warning "Recovered last-known-good model after an interrupted release."
        } elseif (Test-Path -LiteralPath $ModelArtifact -PathType Leaf) {
            Remove-Item -LiteralPath $ModelArtifact -Force
            Write-Warning "Discarded an unvalidated first model candidate."
        }
    } elseif (Test-Path -LiteralPath $ModelArtifact -PathType Leaf) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $ModelBackup) -Force | Out-Null
        Copy-Item -LiteralPath $ModelArtifact -Destination $ModelBackup -Force
        $HadPreviousModel = $true
    }
    if ($HasCompleteReleaseBackup) {
        Restore-ReleaseArtifacts
    } else {
        Remove-Item -LiteralPath $ReleaseBackupRoot -Recurse -Force -ErrorAction SilentlyContinue
        New-ReleaseBackup
    }
}

function Restore-PreviousModel {
    if ($EffectiveWorkflow -ne "train") { return }
    if ($HadPreviousModel -and (Test-Path -LiteralPath $ModelBackup -PathType Leaf)) {
        Copy-Item -LiteralPath $ModelBackup -Destination $ModelArtifact -Force
        Write-Warning "Restored the prior frozen model after a failed release gate."
    } elseif (Test-Path -LiteralPath $ModelArtifact -PathType Leaf) {
        Remove-Item -LiteralPath $ModelArtifact -Force
        Write-Warning "Removed the unvalidated first model candidate."
    }
}

try {
    Push-Location $RepoRoot
    try {
        $Arguments = @($RunnerPath, "--mode", $Mode, "--workflow", $EffectiveWorkflow)
        if (-not [string]::IsNullOrWhiteSpace($Backfill)) { $Arguments += @("--backfill", $Backfill) }
        if ($Mode -eq "demo" -and $EffectiveWorkflow -eq "score") { $Arguments += "--reuse-demo" }
        if ($NoOpen) { $Arguments += "--no-open" }
        & $VenvPython @Arguments 2>&1 | Tee-Object -FilePath $LogPath -Append
        $ExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($ExitCode -ne 0) {
        throw "Technical pipeline stopped with exit code $ExitCode. Prior atomic data files remain intact. See $LogPath"
    }
    if ($EffectiveWorkflow -eq "train") {
        & $VenvPython $WorkbookBuilderPath --mode $Mode 2>&1 | Tee-Object -FilePath $LogPath -Append
        if ($LASTEXITCODE -ne 0) { throw "Workbook export failed. See $LogPath" }
    }
    & $VenvPython $PdfBuilderPath --mode $Mode --output $ProductPdfPath 2>&1 | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) { throw "Product PDF export failed. See $LogPath" }
    & $VenvPython $ValidatorPath --mode $Mode --workflow $EffectiveWorkflow 2>&1 | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) { throw "Release-proof validation failed. See $LogPath" }
} catch {
    Restore-PreviousModel
    Restore-ReleaseArtifacts
    throw
}
if ($EffectiveWorkflow -eq "train") {
    Copy-Item -LiteralPath $ModelArtifact -Destination $LastKnownGoodModel -Force
    Remove-Item -LiteralPath $ModelBackup -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ReleaseBackupRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ReleaseBackupStaging -Recurse -Force -ErrorAction SilentlyContinue
    & $VenvPython $ReleasePrunerPath --project-root $RepoRoot --mode $Mode --keep 5 2>&1 | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Validated release succeeded, but old model bundles were not pruned."
    }
}
Write-Host "Completed successfully. Log retained at $LogPath"
