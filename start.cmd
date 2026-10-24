@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  fflab - FFmpeg filtergraph lab
echo ============================================
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [ERROR] ffmpeg not found in PATH.
  echo Install FFmpeg and make sure "ffmpeg -version" works.
  pause
  exit /b 1
)
echo Starting local server at http://127.0.0.1:8765/
echo Press Ctrl+C to stop.
python -m fflab.cli serve --port 8765
pause
