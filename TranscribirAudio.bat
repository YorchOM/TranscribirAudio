@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] No se encuentra el entorno virtual en .venv
    echo.
    echo Crealo con:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo    Transcriptor de Audio - Whisper + hablantes ^(pyannote^)
echo ============================================================
echo.
echo  Como quieres transcribir?
echo.
echo    1. RAPIDO   - solo texto, SIN separar quien habla
echo                  ^(unas 6 veces mas rapido: 1 hora de audio en ~10 min^)
echo.
echo    2. NORMAL   - con identificacion de hablantes  ^(recomendado^)
echo.
echo    3. PRECISO  - con hablantes, sin trocear ni recortar reintentos.
echo                  El mas lento con diferencia; solo si el 2 falla.
echo.

set "MODO=normal"
set /p OPCION="Opcion [1/2/3] (Enter = 2): "
if "%OPCION%"=="1" set "MODO=rapido"
if "%OPCION%"=="3" set "MODO=preciso"

set "OPCS=--modo %MODO%"
echo.

rem En modo rapido no se identifican hablantes: no tiene sentido preguntar
if "%MODO%"=="rapido" goto :sinPreguntas

set /p NUM="Cuantas personas hablan? (Enter = detectar automaticamente): "
if not "%NUM%"=="" set OPCS=%OPCS% --hablantes %NUM%

set /p NOMBRES="Nombres separados por comas (Enter = Hablante 1, 2, 3...): "
if not "%NOMBRES%"=="" set OPCS=%OPCS% --nombres "%NOMBRES%"

:sinPreguntas
rem Si se han arrastrado archivos sobre el .bat, se procesan directamente
if not "%~1"=="" goto :conArchivos

echo.
echo Se abrira una ventana para elegir el audio (Ctrl/Shift para varios)...
echo.
"%PY%" transcribe_audio.py %OPCS%
goto :fin

:conArchivos
echo Archivos recibidos (arrastrar y soltar): %*
echo.
"%PY%" transcribe_audio.py %OPCS% %*

:fin
echo.
echo El .txt se ha guardado junto al audio original.
pause
