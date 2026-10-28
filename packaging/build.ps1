# Reproducible build: creates .venv (Python 3.12), installs pinned dependencies, runs tests, builds the
# one-folder app with PyInstaller, tests the PACKAGED build, and zips it.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-SkipTests]
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path .venv\Scripts\python.exe)) { py -3.12 -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install -r requirements.txt -r requirements-dev.txt
if ($LASTEXITCODE) { throw "pip install failed" }

if (-not $SkipTests) {
    & $py -m pytest -q
    if ($LASTEXITCODE) { throw "tests failed" }
}

& $py packaging\make_icon.py build\icon.ico
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\PDFReplacementManager.spec
if ($LASTEXITCODE) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\PDFReplacementManager\PDFReplacementManager.exe"
Copy-Item README.md, LICENSES.md -Destination dist\PDFReplacementManager -Force
Copy-Item packaging\install.ps1, packaging\uninstall.ps1 -Destination dist\PDFReplacementManager -Force
Copy-Item chrome_extension -Destination dist\PDFReplacementManager\chrome_extension -Recurse -Force

# Test the packaged executable itself (temporary data and download folders only)
$rep = Join-Path $env:TEMP "prm-packaged-selftest.json"
$p = Start-Process $exe -ArgumentList "--self-test", "--report", "`"$rep`"" -Wait -PassThru
if ($p.ExitCode -ne 0) { Get-Content $rep; throw "packaged self-test failed" }
$smoke = Join-Path $env:TEMP "prm-packaged-smoke.json"
$p = Start-Process $exe -ArgumentList "--smoke-test", "--report", "`"$smoke`"" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "packaged GUI smoke test failed" }
Write-Host "Packaged self-test:" (Get-Content $rep -Raw)
Write-Host "Packaged smoke test:" (Get-Content $smoke -Raw)

$zip = "dist\PDFReplacementManager-1.0.0-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\PDFReplacementManager\* -DestinationPath $zip
Write-Host "Built $zip"
