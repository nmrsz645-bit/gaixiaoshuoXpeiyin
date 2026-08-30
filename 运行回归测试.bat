@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
python -m py_compile unified_app.py voice_monitor.py test_pipeline.py
if errorlevel 1 exit /b %errorlevel%
python -m unittest test_pipeline.py
exit /b %errorlevel%
