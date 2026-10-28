# Removes the program, shortcuts and the per-user autostart entry.
# Your profiles, saved masters, backups of originals and history are KEPT unless you pass -RemoveData.
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-RemoveData]
param([switch]$RemoveData)
$ErrorActionPreference = "Continue"
Get-Process PDFReplacementManager -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() | Out-Null }
Start-Sleep 2
Get-Process PDFReplacementManager -ErrorAction SilentlyContinue | Stop-Process
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PDFReplacementManager" -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("Programs")) "PDF Replacement Manager.lnk") -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "PDF Replacement Manager.lnk") -ErrorAction SilentlyContinue
$prog = Join-Path $env:LOCALAPPDATA "Programs\PDFReplacementManager"
if ($PSScriptRoot -like "$prog*") {
    # running from the installed folder: remove it after this script exits
    Start-Process cmd -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$prog`"" -WindowStyle Hidden
} else {
    Remove-Item $prog -Recurse -Force -ErrorAction SilentlyContinue
}
$data = Join-Path $env:LOCALAPPDATA "PDFReplacementManager"
if ($RemoveData) {
    Remove-Item $data -Recurse -Force
    Write-Host "Program and data removed."
} else {
    Write-Host "Program removed. Your data was kept in $data (delete it manually or rerun with -RemoveData)."
}
