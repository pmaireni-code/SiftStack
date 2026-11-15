@echo off
title Revision de telefonos en Trestle
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0trestle_excel.ps1" %*
echo.
pause