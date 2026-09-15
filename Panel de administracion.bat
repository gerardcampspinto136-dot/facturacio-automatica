@echo off
REM Doble clic aqui para abrir el panel de administracion en el navegador.
REM Arranca solo la web (sin el bot de Telegram), que es lo que hace falta
REM para dar de alta empresas, configurarlas y gestionar cuentas.

cd /d "%~dp0"
title Panel de administracion - NO CERRAR

echo ============================================================
echo   PANEL DE ADMINISTRACION
echo ============================================================
echo.
echo   Tu panel (empresas):   http://localhost:8000/admin
echo   Equipo del cliente:    http://localhost:8000/team
echo   Lo que ve el cliente:  http://localhost:8000/
echo.
echo   Se abrira solo en el navegador en unos segundos.
echo   DEJA ESTA VENTANA ABIERTA mientras lo uses.
echo.
echo ============================================================
echo.

start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8000/admin"

py -c "from dotenv import load_dotenv; load_dotenv(); import uvicorn; from src.config_loader import get_config; c=get_config(); uvicorn.run('src.web.app:app', host=c.web_host, port=c.web_port, log_level='warning')"

echo.
echo ============================================================
echo   EL PANEL SE HA PARADO.
echo ============================================================
echo.
echo Si ves un error arriba, esa es la causa.
echo.
pause
