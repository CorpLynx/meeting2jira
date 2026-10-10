@echo off
rem =============================================================================================
rem  meeting2jira-graph - entry point for the Microsoft Graph path (Path D).
rem
rem  The intended primary source: works with classic Outlook and "new Outlook" alike, needs no COM,
rem  no browser automation, and reads only documented APIs.
rem
rem    meeting2jira-graph                 daily run: export from Graph, push to Jira
rem    meeting2jira-graph preview         same, but create nothing
rem    meeting2jira-graph setup           first-run walkthrough (pip, config, sign-in, Jira)
rem    meeting2jira-graph init            write a starter graph.json
rem    meeting2jira-graph login           authenticate and cache the token
rem    meeting2jira-graph check           verify config, token, permission, then Jira
rem    meeting2jira-graph export          export only, keep the JSON, push nothing
rem    meeting2jira-graph forget-graph    delete the cached Graph token
rem    meeting2jira-graph status          last-run health, then recent sub-tasks
rem    meeting2jira-graph doctor          read-only environment report
rem    meeting2jira-graph schedule        register the weekday scheduled task
rem    meeting2jira-graph selftest        Graph tests plus the COM app's own tests
rem    meeting2jira-graph cli   [args]    Python CLI passthrough
rem
rem  This is an ADDITIONAL exporter, not a fork. Everything that is not Graph-specific - the whole
rem  Jira side, config, token storage, state, reporting - is delegated to the COM app's entry point
rem  rather than duplicated. So there is one Jira config, one Jira token, one state database, and
rem  switching between the COM, OWA and Graph paths cannot create duplicate sub-tasks: dedupe is on
rem  a content hash that does not depend on which source produced the export.
rem
rem  Set M2J_APP_DIR if the COM app does not sit at ..\app.
rem
rem  No -ExecutionPolicy Bypass here either.
rem =============================================================================================
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
if not defined M2J_APP_DIR set "M2J_APP_DIR=%ROOT%\..\app"
set "APPCMD=%M2J_APP_DIR%\meeting2jira.cmd"
set "EXPORTER=%ROOT%\export_graph.py"
set "TASKPS=%ROOT%\Register-GraphSyncTask.ps1"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
rem Odin's files live under Asgard's folder: ASGARD_HOME\odin when set, else %LOCALAPPDATA%\Asgard\odin.
rem A folder from before (%LOCALAPPDATA%\meeting2jira) moves there the first time, in one rename, so
rem the DPAPI token files come across unchanged. Odin doesn't need Asgard installed for this.
if defined ASGARD_HOME (set "ODINDATA=%ASGARD_HOME%\odin") else (set "ODINDATA=%LOCALAPPDATA%\Asgard\odin")
if not defined ASGARD_HOME if not exist "%ODINDATA%\" if exist "%LOCALAPPDATA%\meeting2jira\" (
    if not exist "%LOCALAPPDATA%\Asgard\" mkdir "%LOCALAPPDATA%\Asgard"
    move "%LOCALAPPDATA%\meeting2jira" "%ODINDATA%" >nul || (
        echo ERROR: Odin's files are moving to "%ODINDATA%", but "%LOCALAPPDATA%\meeting2jira" couldn't be moved.
        echo Close anything using that folder, then run this again.
        exit /b 2
    )
)
set "EXPORTDIR=%ODINDATA%\exports"

rem Collect the action and the rest separately: `shift` does not rewrite %*, so reusing %* would
rem pass the action through twice.
set "ACTION=%~1"
set "ARGS="
set "ARG1="
set "EXPORTFLAGS="
set "IDX=0"
if not "%ACTION%"=="" shift
:collect
if "%~1"=="" goto :dispatch
rem Recognised exporter flags are pulled out here rather than being treated as a day count.
if /i "%~1"=="-IncludeOrganizer" (set "EXPORTFLAGS=%EXPORTFLAGS% --include-organizer") else ^
if /i "%~1"=="--include-organizer" (set "EXPORTFLAGS=%EXPORTFLAGS% --include-organizer") else ^
if /i "%~1"=="-AttendeeCount" (set "EXPORTFLAGS=%EXPORTFLAGS% --attendee-count") else ^
if /i "%~1"=="--attendee-count" (set "EXPORTFLAGS=%EXPORTFLAGS% --attendee-count") else ^
if /i "%~1"=="-Verbose" (set "EXPORTFLAGS=%EXPORTFLAGS% -v") else ^
if /i "%~1"=="-v" (set "EXPORTFLAGS=%EXPORTFLAGS% -v") else (
    set /a IDX+=1
    if !IDX!==1 set "ARG1=%~1"
    set "ARGS=%ARGS% %1"
)
shift
goto :collect

:dispatch
if "%ACTION%"==""                 goto :daily
if /i "%ACTION%"=="preview"       goto :preview
if /i "%ACTION%"=="setup"         goto :setup
if /i "%ACTION%"=="init"          goto :initcfg
if /i "%ACTION%"=="login"         goto :login
if /i "%ACTION%"=="check"         goto :check
if /i "%ACTION%"=="export"        goto :exportonly
if /i "%ACTION%"=="forget-graph"  goto :forgetgraph
if /i "%ACTION%"=="schedule"      goto :schedule
if /i "%ACTION%"=="unschedule"    goto :unschedule
if /i "%ACTION%"=="selftest"      goto :selftest
rem Everything else belongs to the COM app: one Jira config, one token, one state database.
if /i "%ACTION%"=="status"        goto :delegate
if /i "%ACTION%"=="doctor"        goto :delegate
if /i "%ACTION%"=="forget"        goto :delegate
if /i "%ACTION%"=="set-token"     goto :delegate
if /i "%ACTION%"=="csv"           goto :delegate
if /i "%ACTION%"=="cli"           goto :delegate
if /i "%ACTION%"=="help"          goto :usage
if /i "%ACTION%"=="-h"            goto :usage
if /i "%ACTION%"=="--help"        goto :usage
if /i "%ACTION%"=="/?"            goto :usage
echo Unknown command "%ACTION%".
echo.
goto :usage

rem ---------------------------------------------------------------------------------------------
:daily
rem Defaults to 1 day back, which re-scans yesterday and catches meetings that ended after the
rem previous run. An optional first argument widens it: "meeting2jira-graph 7". Safe to repeat and
rem safe to widen, because the pipeline dedupes on key or content hash.
call :requireapp
if errorlevel 1 exit /b 2
call :daysback "%ARG1%" 1
call :runexport %DAYS%
if errorlevel 1 exit /b %ERRORLEVEL%
call "%APPCMD%" cli push --input "%EXPORTFILE%"
set "RC=%ERRORLEVEL%"
call :cleanexport %RC%
exit /b %RC%

:preview
call :requireapp
if errorlevel 1 exit /b 2
call :daysback "%ARG1%" 1
call :runexport %DAYS%
if errorlevel 1 exit /b %ERRORLEVEL%
call "%APPCMD%" cli push --input "%EXPORTFILE%" --dry-run
set "RC=%ERRORLEVEL%"
call :cleanexport %RC%
exit /b %RC%

:exportonly
rem Keeps the file. Useful for inspecting it, or for diffing against a COM export.
call :daysback "%ARG1%" 7
call :runexport %DAYS%
if errorlevel 1 exit /b %ERRORLEVEL%
echo.
echo Kept: %EXPORTFILE%
echo It contains calendar data, including meeting subjects. Delete it when you are done.
echo.
echo Push it with:  meeting2jira-graph cli push --input "%EXPORTFILE%" --dry-run
exit /b 0

:initcfg
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --init
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:login
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --login
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:forgetgraph
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --forget
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:check
rem Graph first, then Jira. Both have to work, and the Graph half is the one that is new.
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
echo Checking Microsoft Graph...
%PYCMD% "%EXPORTER%" --check
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo.
    echo Graph check failed. If the token expired:  meeting2jira-graph login
    echo If it is a consent or permission error, the message above says what to ask IT for.
    exit /b %RC%
)
echo.
call :requireapp
if errorlevel 1 exit /b 2
echo Checking Jira...
call "%APPCMD%" check
exit /b %ERRORLEVEL%

:schedule
if not exist "%TASKPS%" (
    echo Not built yet: Register-GraphSyncTask.ps1
    echo.
    echo Until it exists, register the COM app's task and point it at this exporter, or run:
    echo   meeting2jira-graph             manually each morning
    exit /b 2
)
"%PSEXE%" -NoProfile -NonInteractive -File "%TASKPS%"%ARGS%
exit /b %ERRORLEVEL%

:unschedule
if not exist "%TASKPS%" (
    echo Not built yet: Register-GraphSyncTask.ps1
    exit /b 2
)
"%PSEXE%" -NoProfile -NonInteractive -File "%TASKPS%" -Unregister
exit /b %ERRORLEVEL%

rem ---------------------------------------------------------------------------------------------
:setup
echo.
echo meeting2jira-graph setup
echo ========================
echo.
call :resolvepython
if errorlevel 1 exit /b 2
echo Step 1 of 5: install msal
pushd "%ROOT%"
%PYCMD% -m pip install -r requirements.txt
if errorlevel 1 (
    popd
    echo.
    echo pip install failed. If you use an internal mirror, configure it first, e.g.
    echo   %PYCMD% -m pip install -i https://your-mirror/simple -r requirements.txt
    exit /b 2
)
echo.
echo Step 2 of 5: write graph.json
%PYCMD% "%EXPORTER%" --init
echo.
echo Step 3 of 5: fill in the client_id
echo Open %ODINDATA%\graph.json and set client_id to the Application
echo ^(client^) ID from IT. Set "cloud" too if you are in GCC High or DoD.
echo.
pause
echo.
echo Step 4 of 5: sign in
%PYCMD% "%EXPORTER%" --login
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo.
    echo Sign-in did not complete. The message above says what to fix. Then:
    echo   meeting2jira-graph login
    exit /b 2
)
echo.
echo Step 5 of 5: Jira config and token
call :requireapp
if errorlevel 1 exit /b 2
call "%APPCMD%" setup
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Setup complete. Next:
    echo   meeting2jira-graph check       prove Graph and Jira both work
    echo   meeting2jira-graph preview     see what would be created
    echo   meeting2jira-graph             do it for real
) else (
    echo Jira setup reported problems above. Fix them, then:  meeting2jira-graph check
)
exit /b %RC%

:selftest
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
echo Running the Graph tests...
%PYCMD% -m unittest discover -s tests
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" exit /b %RC%
echo.
if exist "%APPCMD%" (
    echo Running the COM app's tests and Windows checks...
    call "%APPCMD%" selftest
    exit /b %ERRORLEVEL%
)
echo COM app not found at "%M2J_APP_DIR%"; skipped its tests.
exit /b 0

rem ---------------------------------------------------------------------------------------------
:delegate
call :requireapp
if errorlevel 1 exit /b 2
rem The push writes last_run.json, but a failed EXPORT means the push never ran, so last_run.json
rem would still show yesterday looking healthy. Report the export outcome first.
if /i "%ACTION%"=="status" call :lastexport
call "%APPCMD%" %ACTION%%ARGS%
exit /b %ERRORLEVEL%

:lastexport
set "LASTEXPORT=%ODINDATA%\last_export.json"
if not exist "%LASTEXPORT%" (
    echo No Graph export has run yet.
    echo.
    exit /b 0
)
echo Last export:
for /f "usebackq delims=" %%L in ("%LASTEXPORT%") do echo   %%L
echo.
exit /b 0

rem ---------------------------------------------------------------------------------------------
:runexport
rem %1 = days back. Sets EXPORTFILE.
call :resolvepython
if errorlevel 1 exit /b 2
if not exist "%EXPORTER%" (
    echo ERROR: export_graph.py not found next to this script.
    exit /b 2
)
if not exist "%EXPORTDIR%" mkdir "%EXPORTDIR%" >nul 2>&1
rem A per-run filename, so two runs cannot overwrite each other mid-flight.
set "EXPORTFILE=%EXPORTDIR%\graph_%RANDOM%%RANDOM%.json"
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --days-back %1 --out "%EXPORTFILE%"%EXPORTFLAGS%
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo.
    echo Export failed. If the token expired:  meeting2jira-graph login
    echo To see the configuration and permission in detail:  meeting2jira-graph check
    exit /b %RC%
)
exit /b 0

:cleanexport
rem The export holds calendar data, so remove it after a clean push. Kept on failure for diagnosis.
if not "%~1"=="0" (
    echo.
    echo Export kept for diagnosis: %EXPORTFILE%
    exit /b 0
)
del "%EXPORTFILE%" >nul 2>&1
exit /b 0

:daysback
rem %1 = the user argument (may be empty or not a number), %2 = default. Sets DAYS.
set "DAYS=%~2"
set "CANDIDATE=%~1"
if "%CANDIDATE%"=="" exit /b 0
rem Accept only digits, so a stray flag is not silently treated as a day count.
echo %CANDIDATE%| findstr /r "^[0-9][0-9]*$" >nul && set "DAYS=%CANDIDATE%"
exit /b 0

:requireapp
if exist "%APPCMD%" exit /b 0
echo ERROR: the meeting2jira app was not found at "%M2J_APP_DIR%".
echo.
echo This exporter handles the calendar only; the Jira side lives in the main app.
echo Put them side by side, or set M2J_APP_DIR to the folder containing meeting2jira.cmd.
exit /b 1

:resolvepython
rem Mirrors Resolve-Python in the .ps1 files and :resolvepython in the COM app's entry point.
rem Existence is not proof: a real agency install often has py.exe present with no 3.x registered
rem (so `py -3` fails), or a working Python that was never added to PATH, or a 2.x on PATH ahead of
rem a 3.x. So every candidate is executed against a probe script and must report 3.8+.
rem
rem PYCMD is quoted, because an install under "C:\Program Files\..." otherwise breaks on the space.
if defined PYCMD exit /b 0
set "PYCMD="
set "PYTHONTRIED="
set "PYPROBE=%TEMP%\m2j-pyprobe-%RANDOM%.py"
> "%PYPROBE%" echo import sys
>>"%PYPROBE%" echo sys.exit(0 if sys.version_info ^>= (3, 8) else 3)

for /f "delims=" %%I in ('where py.exe 2^>nul') do call :trypython "%%I" "-3" && goto :resolvedpython
for /f "delims=" %%I in ('where python3.exe 2^>nul') do call :trypython "%%I" "" && goto :resolvedpython
for /f "delims=" %%I in ('where python.exe 2^>nul') do call :trypython "%%I" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython
for /d %%D in ("%ProgramFiles%\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython
for /d %%D in ("C:\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython

del "%PYPROBE%" >nul 2>&1
echo ERROR: no working Python 3.8+ found. Candidates tried:
if defined PYTHONTRIED echo %PYTHONTRIED%
echo.
echo Searched PATH, the registry, and the usual install directories.
echo Install Python 3.8+ from your agency software catalog, then run:  meeting2jira-graph doctor
exit /b 1

:resolvedpython
del "%PYPROBE%" >nul 2>&1
exit /b 0

:trypython
if defined PYCMD exit /b 0
set "CAND=%~1"
set "PRE=%~2"
if "%CAND%"=="" exit /b 1
echo "%CAND%" | find /i "\WindowsApps\" >nul && exit /b 1
if not exist "%CAND%" exit /b 1
"%CAND%" %PRE% "%PYPROBE%" >nul 2>&1
set "PYRC=%ERRORLEVEL%"
if "%PYRC%"=="0" (
    set "PYCMD="%CAND%" %PRE%"
    exit /b 0
)
if "%PYRC%"=="3" (
    set "PYTHONTRIED=%PYTHONTRIED%  old  %CAND% %PRE% ^(needs 3.8+^)"
) else (
    set "PYTHONTRIED=%PYTHONTRIED%  fail %CAND% %PRE% ^(exit %PYRC%^)"
)
exit /b 1

:usage
echo meeting2jira-graph - push ended meetings into Jira, reading the calendar from
echo                      Microsoft Graph ^(works with classic and "new" Outlook^)
echo.
echo   meeting2jira-graph                 daily run: export from Graph, push to Jira
echo   meeting2jira-graph preview         same, but create nothing
echo   meeting2jira-graph setup           first-run walkthrough
echo   meeting2jira-graph init            write a starter graph.json
echo   meeting2jira-graph login           authenticate and cache the token
echo   meeting2jira-graph check           verify Graph, then Jira
echo   meeting2jira-graph export          export only, keep the JSON
echo   meeting2jira-graph forget-graph    delete the cached Graph token
echo   meeting2jira-graph status          last-run health, then recent sub-tasks
echo   meeting2jira-graph doctor          read-only environment report
echo   meeting2jira-graph schedule        register the weekday scheduled task
echo   meeting2jira-graph selftest        Graph tests plus the COM app's tests
echo   meeting2jira-graph cli  [args]     Python CLI passthrough
echo.
echo Extra flags accepted by the export commands:
echo   -IncludeOrganizer    include the organizer name ^(extra personal data; off by default^)
echo   -AttendeeCount       request attendees so appointments can be told from meetings
echo   -Verbose             more detail
echo.
echo Jira app:     %M2J_APP_DIR%
echo Jira config:  %ODINDATA%\config.json
echo Graph config: %ODINDATA%\graph.json
exit /b 0
