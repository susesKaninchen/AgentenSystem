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
set "DEFAULT_MAX_ITER=6"
set "DEFAULT_RESULTS_PER_QUERY=6"

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
echo [PROMPT] Maximale Suchiterationen begrenzen (schont API-Kontingent):
set "ITER_CHOICE=%DEFAULT_MAX_ITER%"
set /p "ITER_INPUT=Max Iterationen [%DEFAULT_MAX_ITER%]: "
if not "%ITER_INPUT%"=="" set "ITER_CHOICE=%ITER_INPUT%"
set /a ITER_CHOICE=%ITER_CHOICE% >nul 2>&1 || set "ITER_CHOICE=%DEFAULT_MAX_ITER%"
if %ITER_CHOICE% LSS 1 set "ITER_CHOICE=1"
set "ITER_ARG=--max-iterations %ITER_CHOICE%"

echo(
echo [PROMPT] Treffer pro Query begrenzen:
set "RESULT_CHOICE=%DEFAULT_RESULTS_PER_QUERY%"
set /p "RESULT_INPUT=Results/Query [%DEFAULT_RESULTS_PER_QUERY%]: "
if not "%RESULT_INPUT%"=="" set "RESULT_CHOICE=%RESULT_INPUT%"
set /a RESULT_CHOICE=%RESULT_CHOICE% >nul 2>&1 || set "RESULT_CHOICE=%DEFAULT_RESULTS_PER_QUERY%"
if %RESULT_CHOICE% LSS 1 set "RESULT_CHOICE=1"
set "RESULTS_ARG=--results-per-query %RESULT_CHOICE%"

echo(
echo [PROMPT] Anzahl Anschreiben festlegen (wird 1:1 zum Kandidatenziel):
set "LETTER_CHOICE=3"
set /p "LETTER_INPUT=Briefe pro Lauf [3]: "
if not "%LETTER_INPUT%"=="" set "LETTER_CHOICE=%LETTER_INPUT%"
set /a LETTER_CHOICE=%LETTER_CHOICE% >nul 2>&1 || set "LETTER_CHOICE=3"
if %LETTER_CHOICE% LSS 1 set "LETTER_CHOICE=1"
set "LETTERS_ARG=--letters-per-run %LETTER_CHOICE%"

set "TARGET_DEFAULT=%LETTER_CHOICE%"
echo(
echo [PROMPT] Zielanzahl akzeptierter Kandidaten (Stoppt Suche sobald erreicht):
set "TARGET_CHOICE=%TARGET_DEFAULT%"
set /p "TARGET_INPUT=Ziel Kandidaten [%TARGET_DEFAULT%]: "
if not "%TARGET_INPUT%"=="" set "TARGET_CHOICE=%TARGET_INPUT%"
set /a TARGET_CHOICE=%TARGET_CHOICE% >nul 2>&1 || set "TARGET_CHOICE=%TARGET_DEFAULT%"
if %TARGET_CHOICE% LSS 1 set "TARGET_CHOICE=1"
set "TARGET_ARG=--target-candidates %TARGET_CHOICE%"

echo(
echo [PROMPT] Resume-Modus nutzen (bestehende Kandidaten ohne neue Suche)?
set /p "RESUME_CHOICE=Resume verwenden (y/N): "
set "RESUME_FILE="
if /I "%RESUME_CHOICE%"=="Y" (
    rem Hardcoded Standard-Snapshot (kein manuelles Edit nötig)
    set "RESUME_FILE=data/staging/candidates_selected.json"
)

set "PHASE_ARG=--phase %PIPELINE_PHASE%"
set "REGION_ARG=--region %PIPELINE_REGION%"
echo(
echo [INFO] Starte Recherche-Pipeline via uv ...
echo         Phase  : %PIPELINE_PHASE%
echo         Region : %PIPELINE_REGION%
echo         Briefe : %LETTER_CHOICE%
echo         Iter.  : %ITER_CHOICE%
echo         Hits/Q : %RESULT_CHOICE%
echo         Ziel   : %TARGET_CHOICE%
if defined RESUME_FILE (
    set "RESUME_DISPLAY=%RESUME_FILE%"
) else (
    set "RESUME_DISPLAY=nein"
)
echo         Resume : %RESUME_DISPLAY%
echo         (weitere Argumente aus CLI: %*)
echo.
if defined RESUME_FILE (
    uv run %ENV_FLAG% python workflows/research_pipeline.py %PHASE_ARG% %REGION_ARG% %ITER_ARG% %RESULTS_ARG% %LETTERS_ARG% %TARGET_ARG% --resume-candidates "%RESUME_FILE%" %*
) else (
    uv run %ENV_FLAG% python workflows/research_pipeline.py %PHASE_ARG% %REGION_ARG% %ITER_ARG% %RESULTS_ARG% %LETTERS_ARG% %TARGET_ARG% %*
)
if errorlevel 1 (
    echo [ERR] Pipeline beendet mit Fehlercode %errorlevel%.
) else (
    echo [OK] Pipeline erfolgreich abgeschlossen.
)
pause
:end
endlocal
