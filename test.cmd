@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  fflab self-test (real ffmpeg execution)
echo ============================================
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo [ERROR] ffmpeg not found in PATH.
  pause
  exit /b 1
)
echo.
echo [1/2] unit tests
python -m unittest discover -s tests
if errorlevel 1 (
  echo [FAILED] unit tests
  pause
  exit /b 1
)
echo.
echo [2/2] end-to-end tutorial flow
python -m fflab.cli demo
if errorlevel 1 (
  echo [FAILED] demo flow
  pause
  exit /b 1
)
echo.
echo All checks passed.
pause
