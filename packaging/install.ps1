# Per-user install (no administrator rights): copies the app to %LOCALAPPDATA%\Programs and adds a
# Start-menu shortcut. Run from the extracted folder:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Desktop]
param([switch]$Desktop)
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$dest = Join-Path $env:LOCALAPPDATA "Programs\PDFReplacementManager"
$running = Get-Process PDFReplacementManager -ErrorAction SilentlyContinue
if ($running) { throw "Please quit PDF Replacement Manager (tray icon > Quit) before installing." }
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item (Join-Path $src "*") $dest -Recurse -Force
$exe = Join-Path $dest "PDFReplacementManager.exe"
$shell = New-Object -ComObject WScript.Shell
$lnkPath = Join-Path ([Environment]::GetFolderPath("Programs")) "PDF Replacement Manager.lnk"
$lnk = $shell.CreateShortcut($lnkPath); $lnk.TargetPath = $exe; $lnk.WorkingDirectory = $dest; $lnk.Save()
if ($Desktop) {
    $d = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "PDF Replacement Manager.lnk"))
    $d.TargetPath = $exe; $d.WorkingDirectory = $dest; $d.Save()
}
Write-Host "Installed to $dest"
Write-Host "Start it from the Start menu: 'PDF Replacement Manager'."
