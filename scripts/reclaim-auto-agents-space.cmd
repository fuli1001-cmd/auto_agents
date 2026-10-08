@echo off
setlocal
set "RECLAIM_SCRIPT=%~dp0reclaim-auto-agents-space.ps1"
set "RECLAIM_LAUNCHER=%~f0"
if /i "%~1"=="--preview" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RECLAIM_SCRIPT%" -Preview
  goto :preview_end
)
powershell.exe -NoProfile -Command "$p=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}"
if errorlevel 1 (
  powershell.exe -NoProfile -Command "$p=Start-Process -FilePath $env:ComSpec -ArgumentList ('/d /c ""' + $env:RECLAIM_LAUNCHER + '""') -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
  goto :preview_end
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RECLAIM_SCRIPT%"
set "RECLAIM_EXIT=%errorlevel%"
echo.
if "%RECLAIM_EXIT%"=="0" (echo Cleanup and compaction finished.) else (echo Failed. Check D:\auto-agents-storage-reclaim for logs.)
pause
exit /b %RECLAIM_EXIT%
:preview_end
exit /b %errorlevel%
