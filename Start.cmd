@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0dist\TVCheck\TVCheck.exe" (
  start "" "%~dp0dist\TVCheck\TVCheck.exe" %*
) else (
  py -3 "%~dp0main.py" %*
  if errorlevel 1 pause
)
popd
