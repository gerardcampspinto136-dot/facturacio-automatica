@echo off
REM Doble clic aqui para comprobar que todo el sistema funciona.
REM No toca los datos reales: hace las pruebas sobre una base de datos temporal.

cd /d "%~dp0"
title Comprobacion del sistema

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

py verificar.py

echo.
echo Pulsa una tecla para cerrar.
pause >nul
