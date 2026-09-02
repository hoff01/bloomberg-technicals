[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    throw "USERPROFILE is not set."
}
$Python = Join-Path $env:USERPROFILE "Pyenvs\bbg_technical_kronos\Scripts\python.exe"
$Bars = Join-Path $RepoRoot "data\technical\intraday_bars.parquet"
$Output = Join-Path $RepoRoot "data\technical\kronos_forecasts.parquet"
$Config = Join-Path $RepoRoot "config\kronos.toml"
$CacheRoot = if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Join-Path $env:USERPROFILE ".cache\BloombergTechnicals\huggingface"
} else {
    Join-Path $env:LOCALAPPDATA "BloombergTechnicals\huggingface"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Optional runtime not found. Run INSTALL_KRONOS_OPTIONAL.bat first."
}
if (-not (Test-Path -LiteralPath $Bars -PathType Leaf)) {
    throw "Live canonical bars not found. Run TRAIN_AND_SCORE.bat first."
}
& $Python (Join-Path $RepoRoot "scripts\kronos_sidecar.py") --bars $Bars --output $Output --cache-root $CacheRoot --config $Config --force
if ($LASTEXITCODE -ne 0) { throw "Optional Kronos inference failed." }
Write-Host "Kronos diagnostic written to $Output"
Write-Host "Run SCORE_CURRENT.bat to recombine it into the decision dashboard."
