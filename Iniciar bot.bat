@echo off
REM Doble clic aqui para arrancar el bot de facturacion.
REM Deja esta ventana abierta: mientras este abierta, el bot funciona.
REM Para pararlo, cierra la ventana o pulsa Ctrl+C.

cd /d "%~dp0"
title Bot de facturacion - NO CERRAR

echo ============================================================
echo   BOT DE FACTURACION
echo ============================================================
echo.
echo   Arrancando... deja esta ventana abierta.
echo.
echo   Telegram: @gestoriatest_bot
echo   Revision: http://localhost:8000
echo.
echo   Para pararlo: cierra esta ventana.
echo ============================================================
echo.

py main.py

echo.
echo ============================================================
echo   EL BOT SE HA PARADO.
echo ============================================================
echo.
echo Si ves un error arriba, esa es la causa.
echo.
pause
