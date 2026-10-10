@echo off
rem Ysildir, Asgard's MCP server for AI clients. Try: ysildir help
rem An AI client starts Python with cli.py serve directly (ysildir setup writes that); this file is for you.
setlocal EnableExtensions
set "PYEXE="
rem The packaged build brings its own Python: asgard-cli.exe, two folders up.
if exist "%~dp0..\..\asgard-cli.exe" set PYEXE="%~dp0..\..\asgard-cli.exe"
if not defined PYEXE call :try_python py -3
if not defined PYEXE call :try_python py
if not defined PYEXE call :try_python python
if not defined PYEXE (
  echo Ysildir needs Python 3.10 or newer, for the MCP SDK, and none was found. 1>&2
  echo Without Ysildir, an agent can still record its estimates with baldur.cmd ai record, and AI review still works through the clipboard: baldur.cmd ai pack DATE. 1>&2
  exit /b 2
)
if /i "%~1"=="help" (
  %PYEXE% "%~dp0cli.py" --help
) else (
  %PYEXE% "%~dp0cli.py" %*
)
exit /b %ERRORLEVEL%

:try_python
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYEXE=%*"
exit /b 0
