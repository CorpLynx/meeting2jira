@echo off
rem Heimdall from the command line. Try: heimdall help
setlocal EnableExtensions
set "PYEXE="
call :try_python py -3
if not defined PYEXE call :try_python py
if not defined PYEXE call :try_python python
if not defined PYEXE (
  echo Heimdall needs Python 3.9 or newer, and none was found.
  exit /b 2
)
if /i "%~1"=="help" (
  %PYEXE% "%~dp0cli.py" --help
) else (
  %PYEXE% "%~dp0cli.py" %*
)
exit /b %ERRORLEVEL%

:try_python
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=%*"
exit /b 0
