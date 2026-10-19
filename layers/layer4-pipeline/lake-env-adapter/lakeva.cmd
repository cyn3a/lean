@echo off
setlocal

set "LAKEVA_ROOT=%~dp0"
set "PYTHONPATH=%LAKEVA_ROOT%src;%PYTHONPATH%"

if defined LAKEVA_PYTHON if exist "%LAKEVA_PYTHON%" goto run_custom

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PYTHON%" goto run_codex

where py >nul 2>nul
if not errorlevel 1 goto run_py

where python >nul 2>nul
if not errorlevel 1 goto run_python

echo error: Python 3.11 or newer was not found. 1>&2
echo Set LAKEVA_PYTHON to the full path of a Python executable and retry. 1>&2
exit /b 1

:run_custom
"%LAKEVA_PYTHON%" -m lake_env_adapter %*
exit /b %errorlevel%

:run_codex
"%CODEX_PYTHON%" -m lake_env_adapter %*
exit /b %errorlevel%

:run_py
py -3 -m lake_env_adapter %*
exit /b %errorlevel%

:run_python
python -m lake_env_adapter %*
exit /b %errorlevel%
