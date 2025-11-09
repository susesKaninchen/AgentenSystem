@echo off
setlocal
REM Wechsel in das Projektverzeichnis (Standort dieser BAT-Datei)
cd /d "%~dp0"

set "ENV_FLAG="
if exist ".env" (
    set "ENV_FLAG=--env-file .env"
) else (
    echo [WARN] Keine .env gefunden. Lege eine Datei mit OPENAI_* Variablen an.
)

set "VENV_DIR=.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [INFO] Erstelle fehlende uv-venv unter %VENV_DIR% ...
    uv venv
    if errorlevel 1 (
        echo [ERR] Konnte .venv nicht erstellen. Bitte uv/Python prüfen.
        goto :end
    )
)

echo [INFO] Starte Recherche-Pipeline via uv ...
uv run %ENV_FLAG% python workflows/research_pipeline.py %*
if errorlevel 1 (
    echo [ERR] Pipeline beendet mit Fehlercode %errorlevel%.
) else (
    echo [OK] Pipeline erfolgreich abgeschlossen.
)
pause
:end
endlocal
