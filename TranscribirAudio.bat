@echo off
rem Abre la aplicación. Los audios arrastrados sobre el .bat entran directos en la cola.
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo [ERROR] No se encuentra el entorno virtual en .venv
    echo.
    echo Crealo con:
    echo     py -3 -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" app.py %*
