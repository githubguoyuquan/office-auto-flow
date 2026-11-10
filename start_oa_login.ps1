[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

function Stop-WithMessage {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

$appScript = Join-Path $PSScriptRoot "oa_login_cli.py"
if (-not (Test-Path -LiteralPath $appScript -PathType Leaf)) {
    Stop-WithMessage "oa_login_cli.py was not found beside this launcher."
}
$trimModule = Join-Path $PSScriptRoot "trim_template.py"
if (-not (Test-Path -LiteralPath $trimModule -PathType Leaf)) {
    Stop-WithMessage "trim_template.py was not found beside this launcher."
}

$bootstrapExe = $null
$bootstrapArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $bootstrapExe = "py"
    $bootstrapArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $bootstrapExe = "python"
} else {
    Stop-WithMessage "Python 3 is not installed. Install it from https://www.python.org/downloads/windows/ and enable Add Python to PATH."
}

$runtimeRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "oa-login-cli"
} else {
    Join-Path $env:TEMP "oa-login-cli"
}
$venvRoot = Join-Path $runtimeRoot "venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "First run: creating the Python environment..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    $ErrorActionPreference = "Continue"
    & $bootstrapExe @bootstrapArgs -m venv $venvRoot
    $venvExitCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($venvExitCode -ne 0) {
        Stop-WithMessage "Failed to create the Python environment."
    }
}

$playwrightModule = Join-Path $venvRoot "Lib\site-packages\playwright"
if (-not (Test-Path -LiteralPath $playwrightModule -PathType Container)) {
    Write-Host "First run: installing Playwright..." -ForegroundColor Cyan
    $ErrorActionPreference = "Continue"
    & $venvPython -m pip install --disable-pip-version-check playwright
    $pipExitCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($pipExitCode -ne 0) {
        Stop-WithMessage "Failed to install Playwright. Check the network or proxy settings."
    }
}

$openpyxlModule = Join-Path $venvRoot "Lib\site-packages\openpyxl"
if (-not (Test-Path -LiteralPath $openpyxlModule -PathType Container)) {
    Write-Host "Installing XLSX template support..." -ForegroundColor Cyan
    $ErrorActionPreference = "Continue"
    & $venvPython -m pip install --disable-pip-version-check "openpyxl==3.1.5"
    $pipExitCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($pipExitCode -ne 0) {
        Stop-WithMessage "Failed to install XLSX template support. Check the network or proxy settings."
    }
}

Write-Host "Starting OA Login CLI..." -ForegroundColor Green
$ErrorActionPreference = "Continue"
& $venvPython -c "import cryptography, certifi" *> $null
$cryptoExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($cryptoExitCode -ne 0) {
    & $venvPython -m pip install --disable-pip-version-check "cryptography==50.0.2" certifi
    if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Failed to install license verification support." }
}
$ErrorActionPreference = "Continue"
& $venvPython $appScript
$appExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($appExitCode -ne 0) {
    Write-Host "OA Login CLI exited with code $appExitCode." -ForegroundColor Yellow
}
exit $appExitCode
