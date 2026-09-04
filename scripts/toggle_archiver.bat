@echo off
REM Double-click convenience wrapper for toggle_archiver.ps1.
REM
REM Batch files run on double-click with no prompt;
REM a bare .ps1 usually doesn't, since Windows' default PowerShell execution policy blocks unsigned scripts.
REM This wrapper exists purely to sidestep that friction - all the real logic lives in toggle_archiver.ps1, this just launches it.
REM
REM %~dp0 resolves to this .bat file's own directory, so it finds the .ps1 correctly regardless of what folder you double-click it from.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0toggle_archiver.ps1" %*

echo.
pause