@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_EXE="

if exist "%~dp0..\.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"

if not defined PYTHON_EXE (
    echo [AutoSub] Python not found.
    echo [AutoSub] Expected one of:
    echo   1. %~dp0..\.venv\Scripts\python.exe
    echo   2. %~dp0.venv\Scripts\python.exe
    echo   3. %LocalAppData%\Programs\Python\Python312\python.exe
    pause
    exit /b 1
)

echo [AutoSub] Using Python: "%PYTHON_EXE%"
"%PYTHON_EXE%" main.py
if errorlevel 1 pause
