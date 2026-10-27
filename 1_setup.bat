@echo off
setlocal
cd /d "%~dp0"
title Code Checker - Setup

echo ==============================================
echo   Code Checker - Setup  [run this first]
echo ==============================================
echo.

where python >nul 2>nul
if errorlevel 1 goto no_python

echo [1/5] Python version:
python --version
echo.

python -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tk

echo [2/5] Preparing the private environment for this app...
if exist ".venv\Scripts\python.exe" goto venv_ready
python -m venv .venv
if errorlevel 1 goto venv_failed
goto venv_ready

:venv_ready
echo        Done.
echo.

echo [3/5] Installing pip, ruff and mypy. This can take a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install --upgrade ruff mypy
if errorlevel 1 goto pip_failed
echo.

echo [4/5] Installing bandit...
".venv\Scripts\python.exe" -m pip install --upgrade --only-binary=:all: bandit
if not errorlevel 1 goto bandit_ready
echo.
echo        A ready-made PyYAML [a part bandit needs] is not available for this Python.
echo        Building it again without the C++ part. This is normal and can take a minute...
set PYYAML_FORCE_LIBYAML=0
".venv\Scripts\python.exe" -m pip install --upgrade --no-binary pyyaml pyyaml
if errorlevel 1 goto pip_failed
".venv\Scripts\python.exe" -m pip install --upgrade bandit
if errorlevel 1 goto pip_failed

:bandit_ready
echo.

echo [5/5] Installed versions:
".venv\Scripts\python.exe" -m ruff --version
".venv\Scripts\python.exe" -m mypy --version
".venv\Scripts\python.exe" -m bandit --version
echo.
echo ==============================================
echo   Setup finished.
echo   Next: double-click 2_start.bat
echo ==============================================
echo.
pause
exit /b 0

:no_python
echo [ERROR] Python was not found.
echo Please install Python, and check the box "Add python.exe to PATH".
echo.
pause
exit /b 1

:no_tk
echo [ERROR] tkinter [the part that draws windows] is not available in this Python.
echo Please run the Python installer again, choose Modify, and turn on "tcl/tk and IDLE".
echo.
pause
exit /b 1

:venv_failed
echo [ERROR] Could not create the private environment.
echo Please copy all the messages above and send them to the AI.
echo.
pause
exit /b 1

:pip_failed
echo [ERROR] Installing the tools failed.
echo Please copy all the messages above and send them to the AI.
echo.
pause
exit /b 1
