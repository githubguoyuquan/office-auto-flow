[CmdletBinding()]
param([string]$PublicKeys = "", [string]$UpdatePublicKeys = "", [switch]$BuildIssuer)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PublicKeys) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Select public-keys.json (never select a private key)"
    $dialog.Filter = "Public keys|public-keys.json|JSON files|*.json"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
    $PublicKeys = $dialog.FileName
    $dialog.Dispose()
}
$PublicKeys = (Resolve-Path -LiteralPath $PublicKeys).Path
if (-not $UpdatePublicKeys) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Select independent update-public-keys.json"
    $dialog.Filter = "Update public keys|update-public-keys.json|JSON files|*.json"
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
    $UpdatePublicKeys = $dialog.FileName
    $dialog.Dispose()
}
$UpdatePublicKeys = (Resolve-Path -LiteralPath $UpdatePublicKeys).Path
$nsisRoot = Join-Path ${env:ProgramFiles(x86)} "NSIS"
$makensis = Join-Path $nsisRoot "makensis.exe"
if (-not (Test-Path -LiteralPath $makensis)) { throw "Install NSIS 3 before building: https://nsis.sourceforge.io/Download" }
$buildPython = Join-Path $PSScriptRoot ".build-venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $buildPython)) {
    & py -3.12 -m venv (Join-Path $PSScriptRoot ".build-venv")
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12 x64 first." }
}
& $buildPython -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
Push-Location $projectRoot
try {
    & $buildPython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
    $buildStamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $payload = Join-Path $projectRoot "preview\AIOA-Licensed-$buildStamp"
    & $buildPython packaging/build_compiled.py --public-keys $PublicKeys --update-public-keys $UpdatePublicKeys --output $payload
    if ($LASTEXITCODE -ne 0) { throw "Compilation failed. Install Visual Studio 2022 C++ build tools if required." }
    & (Join-Path $payload "AIOA.exe") --self-test
    if ($LASTEXITCODE -ne 0) { throw "Compiled program self-test failed." }
    & (Join-Path $payload "AIOA.exe") --browser-smoke
    if ($LASTEXITCODE -ne 0) { throw "Browser smoke test failed. Install Microsoft Edge before retrying." }
    & $buildPython packaging/smoke_pptx.py (Join-Path $payload "pptx_extractor\bin\pptx-extractor.exe")
    if ($LASTEXITCODE -ne 0) { throw "Packaged PPTX component smoke test failed." }
    $setup = Join-Path $projectRoot "preview\Office-Auto-Flow-API-Licensed-$buildStamp-win-x64.exe"
    & $buildPython packaging/build_windows_installer.py --payload $payload --output $setup --makensis $makensis --nsis-dir $nsisRoot --version "0.2.0-$buildStamp"
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed." }
    if ($BuildIssuer) {
        & $buildPython packaging/build_compiled.py --target issuer --output (Join-Path $projectRoot "preview\AIOA-Issuer-$buildStamp")
        if ($LASTEXITCODE -ne 0) { throw "Issuer compilation failed." }
    }
    Write-Host "Customer installer: $setup"
} finally { Pop-Location }
