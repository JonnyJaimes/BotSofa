@echo off
cd /d "%~dp0"
if not exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" (
    echo No se encontro Python en la ruta configurada.
    pause
    exit /b 1
)
"%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" main.py >> bot.log 2>&1
