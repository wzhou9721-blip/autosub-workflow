@echo off
cd /d "%~dp0"
setlocal

where py >nul 2>nul
if %errorlevel%==0 (
    py -m app.desktop_ui
) else (
    python -m app.desktop_ui
)

if errorlevel 1 (
    echo.
    echo [ERROR] 启动失败，请把上面的报错截图给我。
    pause
)
