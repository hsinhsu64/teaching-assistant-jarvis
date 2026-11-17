@echo off
chcp 65001 >nul
cd /d %~dp0
set "UV=uv"
where uv >nul 2>nul
if errorlevel 1 if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
"%UV%" run python -m xiaoshen.keysetup
pause
