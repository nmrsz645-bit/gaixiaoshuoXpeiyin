@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)
%PYTHON% -m py_compile unified_app.py voice_monitor.py test_pipeline.py
if errorlevel 1 exit /b %errorlevel%
%PYTHON% -m unittest test_pipeline.py
exit /b %errorlevel%
