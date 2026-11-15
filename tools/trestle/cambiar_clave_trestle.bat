@echo off
title Cambiar clave de Trestle
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Environment]::SetEnvironmentVariable('TRESTLE_APIKEY', $null, 'User'); Write-Host 'Clave de Trestle borrada de este equipo.' -ForegroundColor Yellow; Write-Host 'La proxima vez que ejecutes revisar_telefonos_trestle.bat te pedira la nueva.'"
echo.
pause