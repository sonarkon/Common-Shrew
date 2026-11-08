@echo off
rem Common Shrew - launcher for Windows.
rem Checks for Python, ffmpeg and drag & drop support and offers to install what's missing.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ---------- Python (python.org installer includes Tkinter)
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3 is not installed.
  where winget >nul 2>nul || goto :nopython
  choice /M "Install Python 3 now via winget"
  if errorlevel 2 goto :nopython
  winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
  echo.
  echo Python installed. Please start "Common Shrew.bat" again.
  pause
  exit /b 0
)
%PY% -c "import tkinter" 2>nul || (
  echo Your Python has no Tkinter. Reinstall Python from python.org with "tcl/tk" enabled.
  pause
  exit /b 1
)

rem ---------- ffmpeg
where ffmpeg >nul 2>nul && where ffprobe >nul 2>nul && goto :haveffmpeg
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" goto :haveffmpeg
echo ffmpeg is not installed.
where winget >nul 2>nul || goto :noffmpeg
choice /M "Install ffmpeg now via winget"
if errorlevel 2 goto :noffmpeg
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto :noffmpeg
:haveffmpeg

rem ---------- Drag & drop (tkinterdnd2 in a local .venv)
set "VPY=.venv\Scripts\python.exe"
if exist "%VPY%" (
  "%VPY%" -c "import tkinter, tkinterdnd2" 2>nul && goto :run_venv
)
if exist ".no_dnd" goto :run_plain
echo Drag ^& drop needs the small Python package "tkinterdnd2".
choice /M "Install it now into a local .venv folder next to this script"
if errorlevel 2 (
  choice /M "Don't ask this again"
  if not errorlevel 2 type nul > .no_dnd
  goto :run_plain
)
if exist .venv rmdir /s /q .venv
%PY% -m venv .venv && "%VPY%" -m pip install -q --upgrade pip tkinterdnd2 && goto :run_venv
echo Couldn't install tkinterdnd2 - starting without drag ^& drop.

:run_plain
rem pyw/pythonw start the app without a console window
where pyw >nul 2>nul && (start "" pyw -3 common_shrew.py %* & exit /b 0)
start "" pythonw common_shrew.py %*
exit /b 0

:run_venv
start "" ".venv\Scripts\pythonw.exe" common_shrew.py %*
exit /b 0

:nopython
echo Please install Python 3 from https://www.python.org/downloads/ (tick "Add to PATH").
pause
exit /b 1

:noffmpeg
echo The compressor can't work without ffmpeg.
echo Install it with:  winget install Gyan.FFmpeg
echo or download it from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder to PATH.
pause
exit /b 1
