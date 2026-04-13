@echo off
cd /d "%~dp0"
setlocal

where pyw >nul 2>nul && goto run_pyw
where pythonw >nul 2>nul && goto run_pythonw
where py >nul 2>nul && goto run_py
goto run_python

:run_pyw
pyw -m app.desktop_ui
goto done

:run_pythonw
pythonw -m app.desktop_ui
goto done

:run_py
py -m app.desktop_ui
if errorlevel 1 goto failed
goto done

:run_python
python -m app.desktop_ui
if errorlevel 1 goto failed
goto done

:failed
echo.
echo [ERROR] Launch failed. Please send me the error shown above.
pause

:done
endlocal
