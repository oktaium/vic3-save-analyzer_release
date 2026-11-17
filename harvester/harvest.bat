@echo off
REM Victoria 3 save harvester - keeps a copy of every autosave of a running game.
REM
REM Double-click this file and answer the questions, or run it from a command prompt:
REM   harvest.bat <run_name> [saves] [start_save.v3 | attach]
REM     run_name    name of the folder the saves go to:  saves\<run_name>\  (next to compare.py)
REM     saves       stop after this many autosaves (0 or empty = until you close the game)
REM     start_save  save to continue from (empty = your newest save),
REM                 or the word  attach  to collect from a game that is already running
REM   Example: harvest.bat my_run 100 "C:\path\to\start.v3"
REM
REM Stop early: close this window (saves already copied stay).

setlocal
REM ---- Edit this line if Victoria 3 is not installed in the default Steam folder ----
set "GAME_BIN=C:\Program Files (x86)\Steam\steamapps\common\Victoria 3\binaries"
REM -------------------------------------------------------------------------------------

set "TAG=%~1"
set "COUNT=%~2"
set "START=%~3"
if not "%TAG%"=="" goto run

echo Victoria 3 save harvester
echo.
set /p "TAG=Run name (folder for the saves, e.g. my_run): "
if "%TAG%"=="" ( echo No run name given. & pause & exit /b 1 )
set /p "COUNT=Stop after how many autosaves? (Enter = until you close the game): "
set /p "START=Start save - drag a .v3 file here, or Enter = your newest save: "

:run
if "%COUNT%"=="" set "COUNT=0"
if defined START set "START=%START:"=%"
set "OUT=%~dp0..\saves\%TAG%"

if /i "%START%"=="attach" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vic3_run.ps1" -OutDir "%OUT%" -Saves %COUNT% -GameBin "%GAME_BIN%" -Attach
) else if "%START%"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vic3_run.ps1" -OutDir "%OUT%" -Saves %COUNT% -GameBin "%GAME_BIN%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0vic3_run.ps1" -OutDir "%OUT%" -Saves %COUNT% -GameBin "%GAME_BIN%" -StartSave "%START%"
)
echo.
echo Saves are in: %OUT%
pause
endlocal
