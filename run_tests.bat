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
%PYTHON% -m py_compile unified_app.py voice_monitor.py test_pipeline.py test_scene_compliance.py test_scene_parallel.py test_text_preparation.py novel_monitor\scene_document.py novel_monitor\scene_model.py novel_monitor\scene_review.py novel_monitor\text_preparation.py test_release_manifest.py test_rollback_v125.py tools\build_github_release.py
if errorlevel 1 exit /b %errorlevel%
%PYTHON% -m unittest test_pipeline.py test_scene_compliance.py test_scene_parallel.py test_text_preparation.py test_release_manifest.py test_rollback_v125.py
exit /b %errorlevel%
