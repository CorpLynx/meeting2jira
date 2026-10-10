@echo off
rem =============================================================================================
rem  Odin from the command line. Odin is an Asgard app: it keeps its records in Muninn, Asgard's
rem  database, and its config, token, logs and exports in %LOCALAPPDATA%\Asgard\odin.
rem
rem    odin                 the daily run: export yesterday and today from Outlook, push meetings,
rem                         read Jira into Muninn, post the days you approved in Baldur
rem    odin preview         the same, but change nothing (dry run)
rem    odin setup           first-run walkthrough: config, token, connectivity check
rem    odin check           verify config, token, Muninn, Jira access, parents, sub-task type
rem    odin status          last-run health, recent sub-tasks, what waits to be posted
rem    odin doctor          read-only environment report (Test-Environment.ps1)
rem    odin schedule        register the weekday scheduled task
rem    odin unschedule      remove it
rem    odin csv <file>      the daily run from an Outlook CSV export instead of Outlook itself
rem    odin sync            read Jira into Muninn only
rem    odin post            post the days you approved in Baldur only (post --dry-run to list them)
rem    odin selftest        Odin's unit tests plus the Windows-only checks
rem    odin run   [args]    Invoke-MeetingSync.ps1 passthrough, e.g. odin run -DaysBack 7
rem    odin report          every meeting sub-task as a CSV for Power BI (report --no-subjects)
rem    odin cli   [args]    Python CLI passthrough, e.g. odin cli forget PROJ-501
rem
rem  There is deliberately no -ExecutionPolicy Bypass in here. If the scripts will not run, fix
rem  the cause (usually the internet-zone mark on downloaded files) rather than bypassing the
rem  policy: `odin doctor` reports what to do.
rem =============================================================================================
setlocal EnableExtensions

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PSRUN=-NoProfile -NonInteractive -File"
set "SYNCPS=%ROOT%\windows\Invoke-MeetingSync.ps1"
set "TASKPS=%ROOT%\windows\Register-MeetingSyncTask.ps1"
set "DOCTORPS=%ROOT%\windows\Test-Environment.ps1"
set "CHECKPS=%ROOT%\tools\Invoke-WindowsChecks.ps1"
set "CLI=%ROOT%\cli.py"
rem Odin's files live under Asgard's folder: ASGARD_HOME\odin when set, else %LOCALAPPDATA%\Asgard\odin.
rem A folder from before moves there the first time, in one rename, so the DPAPI token files come
rem across unchanged: %LOCALAPPDATA%\odin (the on-premises install), or %LOCALAPPDATA%\meeting2jira
rem before that. Two at once is not guessed: Odin says which to keep.
if defined ASGARD_HOME (set "ASGARDDATA=%ASGARD_HOME%") else (set "ASGARDDATA=%LOCALAPPDATA%\Asgard")
set "ODINDATA=%ASGARDDATA%\odin"
set "ODINOLD="
if not defined ASGARD_HOME if not exist "%ODINDATA%\" (
    if exist "%LOCALAPPDATA%\odin\" set "ODINOLD=%LOCALAPPDATA%\odin"
    if exist "%LOCALAPPDATA%\meeting2jira\" if defined ODINOLD (
        echo ERROR: Odin found two of its old folders, "%LOCALAPPDATA%\odin" and "%LOCALAPPDATA%\meeting2jira".
        echo Move the one with the newest state.db to "%ODINDATA%" yourself, then run this again.
        exit /b 2
    )
    if exist "%LOCALAPPDATA%\meeting2jira\" if not defined ODINOLD set "ODINOLD=%LOCALAPPDATA%\meeting2jira"
)
if defined ODINOLD (
    if not exist "%ASGARDDATA%\" mkdir "%ASGARDDATA%"
    move "%ODINOLD%" "%ODINDATA%" >nul || (
        echo ERROR: Odin's files are moving to "%ODINDATA%", but "%ODINOLD%" couldn't be moved.
        echo Close anything using that folder, then run this again.
        exit /b 2
    )
)
set "CONFIG=%ODINDATA%\config.json"

if not exist "%PSEXE%" (
    echo ERROR: Windows PowerShell not found at "%PSEXE%".
    exit /b 2
)

rem Collect the action and the remaining arguments separately. `shift` does not rewrite %*, so the
rem trailing arguments have to be gathered by hand or the action would be passed through twice.
rem ARG1 is kept unquoted on its own because a path may contain spaces, which token-splitting a
rem combined string would mangle.
set "ACTION=%~1"
set "ARGS="
set "ARG1="
set "RESTARGS="
set "IDX=0"
if not "%ACTION%"=="" shift
:collect
if "%~1"=="" goto :dispatch
set /a IDX+=1
if %IDX%==1 (set "ARG1=%~1") else (set "RESTARGS=%RESTARGS% %1")
set "ARGS=%ARGS% %1"
shift
goto :collect

:dispatch
if "%ACTION%"==""              goto :daily
if /i "%ACTION%"=="preview"    goto :preview
if /i "%ACTION%"=="setup"      goto :setup
if /i "%ACTION%"=="doctor"     goto :doctor
if /i "%ACTION%"=="schedule"   goto :schedule
if /i "%ACTION%"=="unschedule" goto :unschedule
if /i "%ACTION%"=="selftest"   goto :selftest
if /i "%ACTION%"=="csv"        goto :csv
if /i "%ACTION%"=="run"        goto :runthrough
if /i "%ACTION%"=="cli"        goto :clithrough
if /i "%ACTION%"=="check"      goto :simplecli
if /i "%ACTION%"=="status"     goto :simplecli
if /i "%ACTION%"=="sync"       goto :simplecli
if /i "%ACTION%"=="post"       goto :simplecli
if /i "%ACTION%"=="init"       goto :simplecli
if /i "%ACTION%"=="set-token"  goto :simplecli
if /i "%ACTION%"=="forget"     goto :simplecli
if /i "%ACTION%"=="report"     goto :simplecli
if /i "%ACTION%"=="help"       goto :usage
if /i "%ACTION%"=="-h"         goto :usage
if /i "%ACTION%"=="--help"     goto :usage
if /i "%ACTION%"=="/?"         goto :usage
echo Unknown command "%ACTION%".
echo.
goto :usage

rem ---------------------------------------------------------------------------------------------
:daily
rem DaysBack 1 re-scans yesterday, catching meetings that ended after the previous run. Re-running
rem is safe: anything already pushed or posted is recognised in Muninn and skipped.
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%" -DaysBack 1
exit /b %ERRORLEVEL%

:preview
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%" -DaysBack 1 -DryRun
exit /b %ERRORLEVEL%

:runthrough
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%"%ARGS%
exit /b %ERRORLEVEL%

:csv
if "%ARG1%"=="" (
    echo Usage: odin csv "C:\path\to\calendar.csv" [-DryRun]
    exit /b 2
)
if not exist "%ARG1%" (
    echo ERROR: no such file: "%ARG1%"
    exit /b 2
)
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%" -Source Csv -CsvPath "%ARG1%"%RESTARGS%
exit /b %ERRORLEVEL%

:doctor
"%PSEXE%" %PSRUN% "%DOCTORPS%"%ARGS%
exit /b %ERRORLEVEL%

:schedule
"%PSEXE%" %PSRUN% "%TASKPS%"%ARGS%
exit /b %ERRORLEVEL%

:unschedule
"%PSEXE%" %PSRUN% "%TASKPS%" -Unregister
exit /b %ERRORLEVEL%

rem ---------------------------------------------------------------------------------------------
:setup
echo.
echo Odin setup
echo ==========
echo.
call :resolvepython
if errorlevel 1 exit /b 2
echo Step 1 of 4: environment check
"%PSEXE%" %PSRUN% "%DOCTORPS%"
echo.
echo Step 2 of 4: create the config file
%PYEXE% "%CLI%" init
echo.
echo Step 3 of 4: edit the config. At minimum set jira.base_url and jira.default_parent,
echo and review filters.skip_subject_contains ^(OOO, PTO, holiday...^).
echo Opening the config in Notepad. Save and close it to continue.
echo.
start /wait notepad "%CONFIG%"
echo.
echo Step 4 of 4: store your Jira token, then verify the connection
%PYEXE% "%CLI%" set-token
if errorlevel 1 (
    echo.
    echo Token was not stored. Re-run:  odin set-token
    exit /b 2
)
%PYEXE% "%CLI%" check
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Setup complete. Next:
    echo   odin preview     see what would be created and posted
    echo   odin             do it for real
    echo   odin schedule    run it automatically on weekdays
) else (
    echo Checks reported problems above. Fix them, then run:  odin check
)
exit /b %RC%

rem ---------------------------------------------------------------------------------------------
:simplecli
call :resolvepython
if errorlevel 1 exit /b 2
%PYEXE% "%CLI%" %ACTION%%ARGS%
exit /b %ERRORLEVEL%

:clithrough
call :resolvepython
if errorlevel 1 exit /b 2
%PYEXE% "%CLI%"%ARGS%
exit /b %ERRORLEVEL%

:selftest
call :resolvepython
if errorlevel 1 exit /b 2
if defined FROZENPY (
    echo The packaged build: checking that every part of it loads on this computer...
    %PYEXE% --self-test
) else if exist "%ROOT%\..\..\tests\test_odin_pipeline.py" (
    echo Running Odin's unit tests...
    pushd "%ROOT%\..\.."
    %PYEXE% -m unittest discover -s tests -p "test_odin_*.py"
    popd
) else (
    echo This copy has no tests folder; skipping the unit tests.
)
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" exit /b %RC%
echo.
echo Running the Windows-only checks...
"%PSEXE%" %PSRUN% "%CHECKPS%"
exit /b %ERRORLEVEL%

rem ---------------------------------------------------------------------------------------------
:requireconfig
if exist "%CONFIG%" exit /b 0
echo No config found at "%CONFIG%".
echo Run this first:  odin setup
exit /b 1

:resolvepython
rem The Python Odin runs on, in order:
rem   1. the packaged build's own program, two folders up, which brings everything with it;
rem   2. the Python Asgard was installed with (install-ledger.json), which Muninn already works on;
rem   3. any other Python 3.9+ whose SQLite is 3.37 or newer with FTS5, which Muninn needs (Windows
rem      Python has that from 3.11). Mirrors Resolve-Python in the .ps1 files: existence is not
rem      proof, so every candidate is run against a probe, and PYTHONTRIED records the rest.
rem PYEXE is quoted, because an install under "C:\Program Files\..." otherwise breaks on the space.
if defined PYEXE exit /b 0
set "FROZENPY="
if exist "%~dp0..\..\asgard-cli.exe" set PYEXE="%~dp0..\..\asgard-cli.exe"
if defined PYEXE (
    set "FROZENPY=1"
    exit /b 0
)
set "PYTHONTRIED="
set "PYPROBE=%TEMP%\odin-pyprobe-%RANDOM%.py"
> "%PYPROBE%" echo import sqlite3, sys
>>"%PYPROBE%" echo con = sqlite3.connect(":memory:")
>>"%PYPROBE%" echo fts5 = any("FTS5" in row[0] for row in con.execute("pragma compile_options"))
>>"%PYPROBE%" echo ok = sys.version_info ^>= (3, 9) and sqlite3.sqlite_version_info ^>= (3, 37, 0) and fts5
>>"%PYPROBE%" echo sys.exit(0 if ok else 3)

rem 2. the Python Asgard was installed with, read by PowerShell (CLM-safe: a cmdlet and a property)
set "ODIN_LEDGER=%ASGARDDATA%\install-ledger.json"
set "LEDGERPY="
if exist "%ODIN_LEDGER%" for /f "usebackq delims=" %%P in (`"%PSEXE%" -NoProfile -NonInteractive -Command "(Get-Content -Raw -LiteralPath $env:ODIN_LEDGER | ConvertFrom-Json).python"`) do set "LEDGERPY=%%P"
if defined LEDGERPY call :trypython "%LEDGERPY%" "" && goto :resolvedpython
rem 3. the launcher, verified to actually produce a working 3.x
for /f "delims=" %%I in ('where py.exe 2^>nul') do call :trypython "%%I" "-3" && goto :resolvedpython
rem    anything on PATH
for /f "delims=" %%I in ('where python3.exe 2^>nul') do call :trypython "%%I" "" && goto :resolvedpython
for /f "delims=" %%I in ('where python.exe 2^>nul') do call :trypython "%%I" "" && goto :resolvedpython
rem    the registry, where an installer records itself even when PATH was left alone
for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
rem    the usual directories, for an install that registered nothing
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython
for /d %%D in ("%ProgramFiles%\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython
for /d %%D in ("C:\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython

del "%PYPROBE%" >nul 2>&1
echo ERROR: no Python that can run Odin was found. Odin needs Python 3.9 or newer with SQLite 3.37
echo or newer (Python 3.11 or newer on Windows), as Asgard does. Candidates tried:
if defined PYTHONTRIED echo %PYTHONTRIED%
echo.
echo Searched Asgard's install record, PATH, the registry, and the usual install directories.
echo Run Odin from Asgard's own folder, or install Python 3.11+ from your agency software catalog.
exit /b 1

:resolvedpython
del "%PYPROBE%" >nul 2>&1
exit /b 0

:trypython
rem %1 = candidate exe, %2 = prefix ("-3" or ""). Sets PYEXE and returns 0 only if it really runs.
if defined PYEXE exit /b 0
set "CAND=%~1"
set "PRE=%~2"
if "%CAND%"=="" exit /b 1
rem The Store alias stub reports success to `where` but opens the Store instead of running Python.
echo "%CAND%" | find /i "\WindowsApps\" >nul && exit /b 1
if not exist "%CAND%" exit /b 1
"%CAND%" %PRE% "%PYPROBE%" >nul 2>&1
set "PYRC=%ERRORLEVEL%"
if "%PYRC%"=="0" (
    set PYEXE="%CAND%" %PRE%
    exit /b 0
)
if "%PYRC%"=="3" (
    set "PYTHONTRIED=%PYTHONTRIED%  old  %CAND% %PRE% ^(needs 3.9+ with SQLite 3.37+^)"
) else (
    set "PYTHONTRIED=%PYTHONTRIED%  fail %CAND% %PRE% ^(exit %PYRC%^)"
)
exit /b 1

:usage
echo Odin - meetings to Jira sub-tasks, Jira into Muninn, approved Baldur days to Jira
echo.
echo   odin                 daily run: export yesterday and today, push, sync, post
echo   odin preview         same, but change nothing
echo   odin setup           first-run walkthrough
echo   odin check           verify config, token, Muninn and Jira access
echo   odin status          last-run health, recent sub-tasks, what waits to be posted
echo   odin doctor          read-only environment report
echo   odin schedule        register the weekday scheduled task
echo   odin unschedule      remove the scheduled task
echo   odin csv ^<file^>      daily run from an Outlook CSV export
echo   odin sync            read Jira into Muninn only
echo   odin post            post approved Baldur days only
echo   odin selftest        unit tests plus the Windows-only checks
echo   odin run [args]      Invoke-MeetingSync.ps1 passthrough
echo   odin report          meeting sub-tasks as a CSV for Power BI
echo   odin cli [args]      Python CLI passthrough
echo.
echo Config: %CONFIG%
exit /b 0
