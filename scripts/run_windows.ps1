[CmdletBinding()]
param(
    [switch]$InstallOnly,
    [switch]$UpdateOnly,
    [string]$ExportDirectory = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RequirementsPath = Join-Path $RepoRoot "requirements.txt"
$BloombergRequirementsPath = Join-Path $RepoRoot "requirements-bloomberg.txt"
$RuntimeCheckPath = Join-Path $PSScriptRoot "check_runtime_compatibility.py"
$BuildDashboardPath = Join-Path $PSScriptRoot "build_dashboard.py"
$BloombergUpdatePath = Join-Path $PSScriptRoot "update_from_bloomberg.py"
$DashboardExporterPath = Join-Path $RepoRoot "app\export_single_file.py"
$EmbeddedDataPath = Join-Path $RepoRoot "app\static\embedded_data.js"
$StandaloneDashboardPath = Join-Path $RepoRoot "dist\pricing_dashboard_trade_builder.html"
$DashboardTemplatePath = Join-Path $RepoRoot "app\static\index.html"
$DashboardAppPath = Join-Path $RepoRoot "app\static\app.js"
$DashboardMathPath = Join-Path $RepoRoot "app\static\trade_math.js"
$DashboardThemePath = Join-Path $RepoRoot "app\static\theme.js"
$DashboardPlotlyPath = Join-Path $RepoRoot "app\static\plotly-3.3.1.min.js"
$DashboardUrl = "http://127.0.0.1:8765/"
$PricingHistoryPath = Join-Path $RepoRoot "data\pricing_history.csv"
$SampleDataPath = Join-Path $RepoRoot "data\sample_market_data.parquet"
$RootConfigPath = Join-Path $RepoRoot "config\security_roots.xlsx"
$BloombergIndexUrl = "https://blpapi.bloomberg.com/repository/releases/python/simple/"
if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not set; the managed Trade Builder environment cannot be located."
}
$VenvRoot = Join-Path $env:USERPROFILE "Pyenvs"
$VenvPath = Join-Path $VenvRoot "trade_builder"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$ActivateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
$SetupStamp = Join-Path $VenvPath ".requirements.sha256"
$CacheRoot = Join-Path $VenvPath "cache"
$BloombergUpdateSucceeded = $false
$StandalonePublishSucceeded = $false
$VersionCheck = @"
import sys
ok = sys.version_info[:2] in {(3, 12), (3, 13)}
print(sys.version.split()[0])
raise SystemExit(0 if ok else 1)
"@

function Get-CompatiblePython {
    $Candidates = @()
    if ($env:TRADE_BUILDER_PYTHON) {
        $Candidates += [PSCustomObject]@{
            Command = $env:TRADE_BUILDER_PYTHON
            Prefix = @()
            Label = "TRADE_BUILDER_PYTHON"
        }
    }
    foreach ($VersionFolder in @("Python313", "Python312")) {
        foreach ($InstallRoot in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
            if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
                $Candidates += [PSCustomObject]@{
                    Command = Join-Path $InstallRoot "Programs\Python\$VersionFolder\python.exe"
                    Prefix = @()
                    Label = $VersionFolder
                }
                $Candidates += [PSCustomObject]@{
                    Command = Join-Path $InstallRoot "$VersionFolder\python.exe"
                    Prefix = @()
                    Label = $VersionFolder
                }
            }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in @("3.13", "3.12")) {
            $Candidates += [PSCustomObject]@{
                Command = "py"
                Prefix = @("-$Version")
                Label = "py -$Version"
            }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $Candidates += [PSCustomObject]@{
            Command = "python"
            Prefix = @()
            Label = "python"
        }
    }

    foreach ($Candidate in $Candidates) {
        if ($Candidate.Command -ne "py" -and $Candidate.Command -ne "python" -and -not (Test-Path -LiteralPath $Candidate.Command -PathType Leaf)) {
            continue
        }
        try {
            $Arguments = @($Candidate.Prefix) + @("-c", $VersionCheck)
            $Output = & $Candidate.Command @Arguments 2>$null
            if ($LASTEXITCODE -eq 0) {
                return [PSCustomObject]@{
                    Command = $Candidate.Command
                    Prefix = $Candidate.Prefix
                    Label = $Candidate.Label
                    Version = ($Output | Select-Object -First 1)
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

function Test-CompatiblePython([string]$PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        return $false
    }
    try {
        & $PythonPath -c $VersionCheck *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Test-Pip([string]$PythonPath) {
    try {
        & $PythonPath -m pip --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Test-ManagedDependencies([string]$PythonPath) {
    try {
        & $PythonPath -m pip check *> $null
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
        & $PythonPath $RuntimeCheckPath *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Remove-SetupStamp {
    Remove-Item -LiteralPath $SetupStamp -Force -ErrorAction SilentlyContinue
}

function Remove-ManagedEnvironment {
    if (-not (Test-Path -LiteralPath $VenvPath)) {
        return
    }
    $FullVenv = [System.IO.Path]::GetFullPath($VenvPath)
    $AllowedRoot = [System.IO.Path]::GetFullPath($VenvRoot)
    $Prefix = $AllowedRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $FullVenv.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to recreate a Python environment outside $AllowedRoot"
    }
    Write-Host "Rebuilding managed environment: $FullVenv"
    Remove-Item -LiteralPath $FullVenv -Recurse -Force
}

function New-ManagedEnvironment {
    $BasePython = Get-CompatiblePython
    if ($null -eq $BasePython) {
        throw "Python 3.13 or 3.12 was not found. Install 64-bit Python from python.org and rerun UPDATE_AND_OPEN.bat."
    }
    New-Item -ItemType Directory -Path $VenvRoot -Force | Out-Null
    Write-Host "Creating $VenvPath with $($BasePython.Label) (Python $($BasePython.Version))"
    $Arguments = @($BasePython.Prefix) + @("-m", "venv", $VenvPath)
    & $BasePython.Command @Arguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Python virtual environment creation failed."
    }
}

function Repair-Pip {
    if (Test-Pip $VenvPython) {
        return
    }
    Remove-SetupStamp
    Write-Host "pip is missing; repairing it with the managed Python interpreter..."
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -eq 0 -and (Test-Pip $VenvPython)) {
        return
    }

    Remove-ManagedEnvironment
    New-ManagedEnvironment
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0 -or -not (Test-Pip $VenvPython)) {
        throw "pip repair failed after rebuilding $VenvPath."
    }
}

function Install-ManagedDependencies {
    Remove-SetupStamp
    Write-Host "Installing Polars and all dashboard packages from requirements.txt..."
    & $VenvPython -m pip install --disable-pip-version-check --upgrade -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard package installation failed."
    }

    Write-Host "Installing Bloomberg BLPAPI from Bloomberg's official package index..."
    # Official Bloomberg command:
    # python -m pip install --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
    & $VenvPython -m pip install "--index-url=$BloombergIndexUrl" blpapi
    if ($LASTEXITCODE -ne 0) {
        throw "Bloomberg BLPAPI installation failed from $BloombergIndexUrl"
    }

    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Python dependencies are inconsistent."
    }
    & $VenvPython $RuntimeCheckPath
    if ($LASTEXITCODE -ne 0) {
        throw "Bloomberg or Polars could not load from $VenvPath."
    }
}

function Ensure-EmbeddedDashboardData {
    $OutputPaths = @($EmbeddedDataPath, $StandaloneDashboardPath)
    $NeedsBuild = $OutputPaths | Where-Object { -not (Test-Path -LiteralPath $_) } | Select-Object -First 1
    $SourceDataPath = if (Test-Path -LiteralPath $PricingHistoryPath) {
        $PricingHistoryPath
    } elseif (Test-Path -LiteralPath $SampleDataPath) {
        $SampleDataPath
    } else {
        $null
    }
    if ($null -eq $NeedsBuild) {
        $OldestOutputTimestamp = ($OutputPaths | ForEach-Object {
            (Get-Item -LiteralPath $_).LastWriteTimeUtc
        } | Measure-Object -Minimum).Minimum
        foreach ($SourcePath in @(
            $SourceDataPath,
            $RootConfigPath,
            $BuildDashboardPath,
            $DashboardExporterPath,
            $DashboardTemplatePath,
            $DashboardAppPath,
            $DashboardMathPath,
            $DashboardThemePath,
            $DashboardPlotlyPath
        )) {
            if ($null -eq $SourcePath) {
                continue
            }
            if ((Get-Item -LiteralPath $SourcePath).LastWriteTimeUtc -gt $OldestOutputTimestamp) {
                $NeedsBuild = $true
                break
            }
        }
    }
    if ($null -eq $NeedsBuild -or $NeedsBuild -eq $false) {
        return
    }
    if ($null -eq $SourceDataPath) {
        throw "Dashboard data is missing. Run a Bloomberg update to create $PricingHistoryPath."
    }

    Write-Host "Building the initial embedded dashboard data..."
    Push-Location $RepoRoot
    try {
        & $VenvPython $BuildDashboardPath --data $SourceDataPath
        $BuildExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($BuildExitCode -ne 0 -or ($OutputPaths | Where-Object { -not (Test-Path -LiteralPath $_) } | Select-Object -First 1)) {
        throw "Initial dashboard data build failed."
    }
}

function Update-BloombergData {
    Write-Host "Pulling the latest Bloomberg data and rebuilding export artifacts..."
    Push-Location $RepoRoot
    try {
        & $VenvPython $BloombergUpdatePath --config $RootConfigPath
        $UpdateExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($UpdateExitCode -ne 0) {
        if (Test-Path -LiteralPath $StandaloneDashboardPath -PathType Leaf) {
            Write-Warning "Bloomberg data update failed after retries. Continuing with the previous local dashboard; no stale export was republished."
            Write-Host "Previous local export: $StandaloneDashboardPath"
            return
        }
        throw "Bloomberg data update failed and no previous local dashboard is available."
    }
    if (-not (Test-Path -LiteralPath $StandaloneDashboardPath -PathType Leaf)) {
        throw "Bloomberg update completed without creating the standalone export: $StandaloneDashboardPath"
    }
    $script:BloombergUpdateSucceeded = $true
}

function Copy-StandaloneExportWithRetry(
    [string]$TemporaryPath,
    [string]$DestinationPath,
    [string]$SourceHash
) {
    $LastError = $null
    foreach ($Attempt in 1..5) {
        try {
            # Copying in place preserves the SharePoint-synced file identity.
            Copy-Item -LiteralPath $TemporaryPath -Destination $DestinationPath -Force -ErrorAction Stop
            $DestinationHash = (Get-FileHash -LiteralPath $DestinationPath -Algorithm SHA256).Hash
            if ($SourceHash -ne $DestinationHash) {
                throw "The published export did not match the local standalone dashboard."
            }
            return
        } catch {
            $LastError = $_
            if ($Attempt -lt 5) {
                Start-Sleep -Milliseconds (200 * $Attempt)
            }
        }
    }
    throw "Could not replace the SharePoint export after 5 attempts: $($LastError.Exception.Message)"
}

function Publish-StandaloneDashboard([string]$DestinationDirectory) {
    if ([string]::IsNullOrWhiteSpace($DestinationDirectory)) {
        $DestinationDirectory = Join-Path $env:USERPROFILE "OneDrive - Energy Transfer\Trading Analytics - Documents\General\Disty Analytics\Trade_Builder"
    }

    $DestinationPath = Join-Path $DestinationDirectory (Split-Path -Leaf $StandaloneDashboardPath)
    $TemporaryPath = Join-Path $DestinationDirectory ".$((Split-Path -Leaf $StandaloneDashboardPath)).$PID.tmp"
    try {
        New-Item -ItemType Directory -Path $DestinationDirectory -Force -ErrorAction Stop | Out-Null
        Copy-Item -LiteralPath $StandaloneDashboardPath -Destination $TemporaryPath -Force -ErrorAction Stop
        $SourceHash = (Get-FileHash -LiteralPath $StandaloneDashboardPath -Algorithm SHA256).Hash
        $TemporaryHash = (Get-FileHash -LiteralPath $TemporaryPath -Algorithm SHA256).Hash
        if ($SourceHash -ne $TemporaryHash) {
            throw "The staged export did not match the local standalone dashboard."
        }
        Copy-StandaloneExportWithRetry $TemporaryPath $DestinationPath $SourceHash
        $script:StandalonePublishSucceeded = $true
        Write-Host "Standalone export published: $DestinationPath"
    } catch {
        Write-Warning "Could not publish the standalone export to $DestinationPath`: $($_.Exception.Message)"
        Write-Host "Standalone export remains available locally: $StandaloneDashboardPath"
    } finally {
        Remove-Item -LiteralPath $TemporaryPath -Force -ErrorAction SilentlyContinue
    }
}

function Test-ExistingDashboardServer {
    try {
        $Status = Invoke-RestMethod -Uri ($DashboardUrl + "api/update/status") -TimeoutSec 2
        return $Status.update_api -eq $true
    } catch {
        return $false
    }
}

foreach ($RequiredPath in @(
    $RequirementsPath,
    $BloombergRequirementsPath,
    $RuntimeCheckPath,
    $BuildDashboardPath,
    $BloombergUpdatePath,
    $DashboardExporterPath,
    $DashboardTemplatePath,
    $DashboardAppPath,
    $DashboardMathPath,
    $DashboardThemePath,
    $DashboardPlotlyPath,
    $RootConfigPath
)) {
    if (-not (Test-Path -LiteralPath $RequiredPath)) {
        throw "Required setup file was not found: $RequiredPath"
    }
}

if ((Test-Path -LiteralPath $VenvPython) -and -not (Test-CompatiblePython $VenvPython)) {
    Remove-SetupStamp
    Remove-ManagedEnvironment
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Remove-SetupStamp
    New-ManagedEnvironment
}
Repair-Pip

$RequirementsHash = @(
    (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash
    (Get-FileHash -LiteralPath $BloombergRequirementsPath -Algorithm SHA256).Hash
    (Get-FileHash -LiteralPath $RuntimeCheckPath -Algorithm SHA256).Hash
) -join ":"
$InstalledHash = if (Test-Path -LiteralPath $SetupStamp) {
    (Get-Content -LiteralPath $SetupStamp -Raw).Trim()
} else {
    ""
}

if ($InstalledHash -ne $RequirementsHash -or -not (Test-ManagedDependencies $VenvPython)) {
    Install-ManagedDependencies
    Set-Content -LiteralPath $SetupStamp -Value $RequirementsHash -Encoding ASCII
}

New-Item -ItemType Directory -Path $CacheRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $ActivateScript)) {
    throw "The managed activation script is missing: $ActivateScript"
}
. $ActivateScript
$env:VIRTUAL_ENV = $VenvPath
$env:PATH = (Join-Path $VenvPath "Scripts") + ";" + $env:PATH
$env:PIP_CACHE_DIR = Join-Path $CacheRoot "pip"
$env:PYTHONPYCACHEPREFIX = Join-Path $CacheRoot "pycache"
$env:PYTHONUTF8 = "1"

Write-Host "Trade Builder environment ready: $VenvPath"
& $VenvPython $RuntimeCheckPath
if ($LASTEXITCODE -ne 0) {
    throw "Managed runtime validation failed."
}

if ($InstallOnly) {
    Ensure-EmbeddedDashboardData
    exit 0
}

Update-BloombergData
if ($BloombergUpdateSucceeded) {
    Publish-StandaloneDashboard $ExportDirectory
}

if ($UpdateOnly) {
    if (-not $BloombergUpdateSucceeded) {
        throw "Bloomberg update failed; the prior exports were preserved and nothing stale was republished."
    }
    if (-not $StandalonePublishSucceeded) {
        throw "Bloomberg update succeeded locally, but the standalone export could not be published."
    }
    Write-Host "Bloomberg data and standalone export are up to date."
    exit 0
}

if (Test-ExistingDashboardServer) {
    Write-Host "Pricing Dashboard owner server already running: $DashboardUrl"
    Start-Process $DashboardUrl
    exit 0
}

Push-Location $RepoRoot
try {
    & $VenvPython (Join-Path $RepoRoot "scripts\run_dashboard.py") --open
    $ExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($ExitCode -ne 0) {
    throw "Pricing Dashboard stopped with exit code $ExitCode."
}
