@echo off
cd /d "%~dp0"
pyinstaller --noconfirm --clean --onedir --windowed --name 小说处理中心 --distpath "发布版-最新" --workpath "build" --specpath "build" --hidden-import edge_tts --add-data "%~dp0updater-patch;updater-patch" --collect-data certifi --collect-binaries imageio_ffmpeg unified_app.py
