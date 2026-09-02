[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$RequirementsPath = Join-Path $RepoRoot "requirements-kronos.txt"
$CacheScript = Join-Path $RepoRoot "scripts\kronos_model_cache.py"

if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not set; the optional Kronos environment cannot be located."
}
$VenvRoot = Join-Path $env:USERPROFILE "Pyenvs"
$VenvPath = Join-Path $VenvRoot "bbg_technical_kronos"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$SetupStamp = Join-Path $VenvPath ".kronos-requirements.sha256"
$CacheRoot = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Join-Path $env:USERPROFILE ".cache\BloombergTechnicals\huggingface"
} else {
    Join-Path $env:LOCALAPPDATA "BloombergTechnicals\huggingface"
}

function Get-CompatiblePython {
    $Candidates = @()
    if ($env:BBG_TECHNICAL_PYTHON) {
        $Candidates += [PSCustomObject]@{Command=$env:BBG_TECHNICAL_PYTHON; Prefix=@()}
    }
    foreach ($VersionFolder in @("Python313", "Python312")) {
        foreach ($InstallRoot in @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
            if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) {
                $Candidates += [PSCustomObject]@{Command=(Join-Path $InstallRoot "Programs\Python\$VersionFolder\python.exe"); Prefix=@()}
                $Candidates += [PSCustomObject]@{Command=(Join-Path $InstallRoot "$VersionFolder\python.exe"); Prefix=@()}
            }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in @("3.13", "3.12")) {
            $Candidates += [PSCustomObject]@{Command="py"; Prefix=@("-$Version")}
        }
    }
    foreach ($Candidate in $Candidates) {
        if ($Candidate.Command -ne "py" -and -not (Test-Path -LiteralPath $Candidate.Command)) { continue }
        & $Candidate.Command @($Candidate.Prefix) -c "import sys; raise SystemExit(0 if sys.version_info[:2] in {(3,12),(3,13)} else 1)" *> $null
        if ($LASTEXITCODE -eq 0) { return $Candidate }
    }
    return $null
}

foreach ($RequiredPath in @($RequirementsPath, $CacheScript)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Required file was not found: $RequiredPath"
    }
}

$BasePython = Get-CompatiblePython
if ($null -eq $BasePython) {
    throw "Python 3.12 or 3.13 (64-bit) was not found. Install it from python.org and rerun."
}
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    New-Item -ItemType Directory -Path $VenvRoot -Force | Out-Null
    Remove-Item -Force $SetupStamp -ErrorAction SilentlyContinue
    & $BasePython.Command @($BasePython.Prefix) -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not create $VenvPath" }
}

& $VenvPython -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { Write-Warning "ensurepip could not repair the existing optional environment." }
    & $VenvPython -m pip --version *> $null
}
if ($LASTEXITCODE -ne 0) {
    Remove-Item -Force $SetupStamp -ErrorAction SilentlyContinue
    $ExpectedParent = [System.IO.Path]::GetFullPath($VenvRoot)
    $ResolvedVenv = [System.IO.Path]::GetFullPath($VenvPath)
    if (-not $ResolvedVenv.StartsWith($ExpectedParent, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to rebuild an environment outside $ExpectedParent"
    }
    Remove-Item -LiteralPath $VenvPath -Recurse -Force
    & $BasePython.Command @($BasePython.Prefix) -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not rebuild $VenvPath" }
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed in $VenvPath" }
}

$RequirementsHash = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash
$InstalledHash = if (Test-Path -LiteralPath $SetupStamp) {(Get-Content -LiteralPath $SetupStamp -Raw).Trim()} else {""}
if ($RequirementsHash -ne $InstalledHash) {
    Write-Host "Installing the isolated optional Kronos runtime..."
    & $VenvPython -m pip install --disable-pip-version-check --upgrade -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) { throw "Optional Kronos dependency installation failed." }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Optional Kronos dependencies are inconsistent." }
    Set-Content -LiteralPath $SetupStamp -Value $RequirementsHash -Encoding ASCII
}

New-Item -ItemType Directory -Path $CacheRoot -Force | Out-Null
Write-Host "Caching pinned Kronos model files under $CacheRoot"
& $VenvPython $CacheScript --cache-root $CacheRoot
if ($LASTEXITCODE -ne 0) { throw "Pinned Kronos model download or verification failed." }

Write-Host "Optional Kronos runtime ready: $VenvPath"
Write-Host "The base Bloomberg technical environment remains separate and unchanged."
