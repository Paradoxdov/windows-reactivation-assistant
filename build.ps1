[CmdletBinding()]
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceRoot = Join-Path $ProjectRoot "src"
$EntryPoint = Join-Path $SourceRoot "main.py"
$BuildRoot = Join-Path $ProjectRoot ".build"
$WorkDir = Join-Path $BuildRoot "work"
$SpecDir = Join-Path $BuildRoot "spec"
$ReleaseDir = Join-Path $ProjectRoot "release"
$Executable = Join-Path $ReleaseDir "WindowsReactivationAssistant.exe"
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($Python) {
    $PythonExe = $Python
} elseif (Test-Path -LiteralPath $VenvPython) {
    $PythonExe = $VenvPython
} else {
    $PythonExe = "python"
}

New-Item -ItemType Directory -Force -Path $WorkDir, $SpecDir, $ReleaseDir | Out-Null

& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "WindowsReactivationAssistant" `
    --paths $SourceRoot `
    --distpath $ReleaseDir `
    --workpath $WorkDir `
    --specpath $SpecDir `
    $EntryPoint

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") `
    -Destination (Join-Path $ReleaseDir "README.md") -Force

$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash.ToLowerInvariant()
"$Hash  WindowsReactivationAssistant.exe" | Set-Content `
    -LiteralPath (Join-Path $ReleaseDir "SHA256SUMS.txt") `
    -Encoding ascii

Write-Host "Built: $Executable"
Write-Host "SHA256: $Hash"
