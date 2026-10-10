@echo off
rem Asgard setup: installs Asgard for the current user. No admin rights needed.
rem Options are passed to the installer: --desktop adds a desktop shortcut,
rem --no-launch skips opening Asgard at the end.
setlocal EnableExtensions
title Asgard setup

if not exist "%~dp0asgard\install.py" goto not_extracted

set "PYEXE="
rem The packaged build brings its own Python: asgard-cli.exe, beside this file.
if exist "%~dp0asgard-cli.exe" set PYEXE="%~dp0asgard-cli.exe"
if not defined PYEXE call :try_python py -3
if not defined PYEXE call :try_python py
if not defined PYEXE call :try_python python
if not defined PYEXE goto no_python

%PYEXE% "%~dp0asgard\install.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Setup did not finish. The messages above say why.
pause
exit /b %RC%

:try_python
rem Sets PYEXE to the command given, if it runs Python 3.9 or newer.
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=%*"
exit /b 0

:not_extracted
echo.
echo Setup can't find its files. Extract the whole zip first:
echo right-click the zip, choose Extract All, then run setup-Asgard.cmd
echo from the extracted folder.
echo.
pause
exit /b 1

:no_python
echo.
echo Asgard needs Python 3.9 or newer, with Tcl/Tk, and none was found.
echo Request Python from your software catalog, then run setup again.
echo.
pause
exit /b 2
