@echo off
title Prueba Trestle con litigantes
cd /d "%~dp0"
echo Prueba de Trestle con el add-on de litigantes (tiene costo extra por consulta).
echo.
set "LEADS=1"
set /p "LEADS=Cuantos leads quieres revisar? (Enter = 1, 0 = todos): "
powershell.exe -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0trestle_excel.ps1" -Litigator -MaxLeads %LEADS%
echo.
pause