@echo off
cd /d "%~dp0"
title Code Checker

if not exist ".venv\Scripts\python.exe" goto need_setup

".venv\Scripts\python.exe" code_checker.py
if errorlevel 1 goto app_error
exit /b 0

:need_setup
echo Setup has not been done yet.
echo Please double-click 1_setup.bat first.
echo.
if not "%~1"=="hidden" pause
exit /b 1

:app_error
echo.
echo Code Checker stopped with an error.
echo Please copy all the messages above and send them to the AI.
echo.
if not "%~1"=="hidden" pause
exit /b 1
