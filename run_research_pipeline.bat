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

set "DEFAULT_PHASE=refine"
set "DEFAULT_REGION=luebeck-local"
set "DEFAULT_RESUME=data/staging/candidates_selected.json"

echo(
echo [PROMPT] Kandidaten-Snapshot vor dem Lauf deduplizieren (tools/seed_registry.py)?
set /p "DEDUPE_CHOICE=Vorab aufraeumen (y/N): "
if /I "%DEDUPE_CHOICE%"=="Y" (
    echo [INFO] Fuehre tools/seed_registry.py aus ...
    uv run %ENV_FLAG% python tools/seed_registry.py
    if errorlevel 1 (
        echo [WARN] seed_registry.py meldete Fehler %errorlevel%.
    ) else (
        echo [OK] Deduplizierung abgeschlossen.
    )
)

echo(
echo [PROMPT] Bitte Phase auswaehlen:
echo   1^) explore  - breite Suche, niedriger Score, wenige Briefe
echo   2^) refine   - ausgewogen (Standard)
echo   3^) acquire  - fokussiert, mehrere Briefe
set /p "PHASE_CHOICE=Phase [2]: "
if "%PHASE_CHOICE%"=="" set "PHASE_CHOICE=2"
set "PIPELINE_PHASE=%DEFAULT_PHASE%"
if "%PHASE_CHOICE%"=="1" set "PIPELINE_PHASE=explore"
if "%PHASE_CHOICE%"=="3" set "PIPELINE_PHASE=acquire"

echo(
echo [PROMPT] Region auswaehlen:
echo   1^) nord           - gesamter Norden (Default Seeds)
echo   2^) hamburg        - Metropolregion Hamburg
echo   3^) luebeck        - Stadt + direkte Umgebung
echo   4^) luebeck-local  - Luebeck + Ostholstein + Kiel (empfohlen)
echo   5^) kiel           - Kiel + Eckernfoerde + Rendsburg
echo   6^) hannover       - Hannover/Braunschweig
echo   7^) benutzerdefiniert
set /p "REGION_CHOICE=Region [4]: "
if "%REGION_CHOICE%"=="" set "REGION_CHOICE=4"
set "PIPELINE_REGION=%DEFAULT_REGION%"
if "%REGION_CHOICE%"=="1" set "PIPELINE_REGION=nord"
if "%REGION_CHOICE%"=="2" set "PIPELINE_REGION=hamburg"
if "%REGION_CHOICE%"=="3" set "PIPELINE_REGION=luebeck"
if "%REGION_CHOICE%"=="4" set "PIPELINE_REGION=luebeck-local"
if "%REGION_CHOICE%"=="5" set "PIPELINE_REGION=kiel"
if "%REGION_CHOICE%"=="6" set "PIPELINE_REGION=hannover"
if "%REGION_CHOICE%"=="7" (
    set /p "PIPELINE_REGION=Eigene Region (z.B. luebeck-local): "
    if "%PIPELINE_REGION%"=="" set "PIPELINE_REGION=%DEFAULT_REGION%"
)

echo(
echo [PROMPT] Anzahl Anschreiben (1-5) waehlen:
set /p "LETTER_CHOICE=Briefe pro Lauf [3]: "
if "%LETTER_CHOICE%"=="" set "LETTER_CHOICE=3"
for /f "tokens=* delims=0123456789" %%A in ("%LETTER_CHOICE%") do set "LETTER_CHOICE=3"
set /a "LETTER_CHOICE=%LETTER_CHOICE%"
if %LETTER_CHOICE% LSS 1 set "LETTER_CHOICE=1"
if %LETTER_CHOICE% GTR 5 set "LETTER_CHOICE=5"
set "LETTERS_ARG=--letters-per-run %LETTER_CHOICE%"

echo(
echo [PROMPT] Resume-Modus nutzen (bestehende Kandidaten ohne neue Suche)?
set /p "RESUME_CHOICE=Resume verwenden (y/N): "
set "RESUME_ARG="
set "RESUME_FILE="
if /I "%RESUME_CHOICE%"=="Y" (
    set /p "RESUME_FILE=Snapshot-Datei [%DEFAULT_RESUME%]: "
    if "%RESUME_FILE%"=="" set "RESUME_FILE=%DEFAULT_RESUME%"
    set "RESUME_ARG=--resume-candidates \"%RESUME_FILE%\""
)

set "PHASE_ARG=--phase %PIPELINE_PHASE%"
set "REGION_ARG=--region %PIPELINE_REGION%"

echo(
echo [INFO] Starte Recherche-Pipeline via uv ...
echo         Phase  : %PIPELINE_PHASE%
echo         Region : %PIPELINE_REGION%
echo         Briefe : %LETTER_CHOICE%
if defined RESUME_ARG (
    echo         Resume : %RESUME_FILE%
) else (
    echo         Resume : nein
)
echo         (weitere Argumente aus CLI: %*)
echo.
uv run %ENV_FLAG% python workflows/research_pipeline.py %PHASE_ARG% %REGION_ARG% %LETTERS_ARG% %RESUME_ARG% %*
if errorlevel 1 (
    echo [ERR] Pipeline beendet mit Fehlercode %errorlevel%.
) else (
    echo [OK] Pipeline erfolgreich abgeschlossen.
)
pause
:end
endlocal
