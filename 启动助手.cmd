@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Missing .venv. Follow README.md to install Python and dependencies first.
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -c "import tkinter, aedat, numpy, imageio_ffmpeg; from PIL import Image"
if errorlevel 1 (
  echo Dependency check failed. See README.md.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0event_export_assistant.py"
