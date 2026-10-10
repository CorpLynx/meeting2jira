@echo off
rem =============================================================================================
rem  meeting2jira-owa - entry point for the Outlook-on-the-web path ("new Outlook").
rem
rem  Same surface as the COM app, so muscle memory carries over:
rem
rem    meeting2jira-owa                 daily run: export from OWA, push to Jira
rem    meeting2jira-owa preview         same, but create nothing
rem    meeting2jira-owa setup           first-run walkthrough (pip, browser, sign-in, config)
rem    meeting2jira-owa login           re-authenticate when the browser session expires
rem    meeting2jira-owa export          export only, keep the JSON, push nothing
rem    meeting2jira-owa discover        print the calendar API requests OWA makes (diagnosis)
rem    meeting2jira-owa check           verify config, token and Jira access
rem    meeting2jira-owa status          last-run health, then recent sub-tasks
rem    meeting2jira-owa settle [ID]     posts in doubt, and settling one (delegated)
rem    meeting2jira-owa doctor          read-only environment report
rem    meeting2jira-owa schedule        register the weekday scheduled task
rem    meeting2jira-owa unschedule      remove the scheduled task
rem    meeting2jira-owa selftest        mapping tests plus the COM app's own tests
rem    meeting2jira-owa cli   [args]    Python CLI passthrough
rem
rem  This is an ADDITIONAL exporter, not a fork. Everything that is not OWA-specific - the whole
rem  Jira side, config, token storage, state, reporting - is delegated to the COM app's entry point
rem  rather than duplicated here. So there is one config file, one token, one state database, and
rem  running both paths cannot create duplicate sub-tasks.
rem
rem  Set ODIN_APP_DIR if Odin (Asgard's apps\odin) is somewhere this can't find.
rem
rem  No -ExecutionPolicy Bypass here either.
rem =============================================================================================
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
rem Odin is an Asgard app: the Jira side is apps\odin\odin.cmd in Asgard's folder. ODIN_APP_DIR names
rem that folder when it is somewhere else (M2J_APP_DIR, its old name, still works); otherwise the
rem installed copy in %LOCALAPPDATA%\Asgard\app, then a checkout of the repository beside this one.
if defined M2J_APP_DIR if not defined ODIN_APP_DIR set "ODIN_APP_DIR=%M2J_APP_DIR%"
if defined ASGARD_HOME (set "ASGARDAPP=%ASGARD_HOME%\app") else (set "ASGARDAPP=%LOCALAPPDATA%\Asgard\app")
if not defined ODIN_APP_DIR if exist "%ASGARDAPP%\apps\odin\odin.cmd" set "ODIN_APP_DIR=%ASGARDAPP%\apps\odin"
if not defined ODIN_APP_DIR set "ODIN_APP_DIR=%ROOT%\..\..\Asgard\apps\odin"
set "APPCMD=%ODIN_APP_DIR%\odin.cmd"
set "EXPORTER=%ROOT%\export_owa.py"
set "TASKPS=%ROOT%\Register-OwaSyncTask.ps1"
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
rem Recognised exporter flags are pulled out here rather than being treated as a day count. The COM
rem path has -IncludeOrganizer and -Verbose, so this path offers the same.
if /i "%~1"=="-IncludeOrganizer" (set "EXPORTFLAGS=%EXPORTFLAGS% --include-organizer") else ^
if /i "%~1"=="--include-organizer" (set "EXPORTFLAGS=%EXPORTFLAGS% --include-organizer") else ^
if /i "%~1"=="-Verbose" (set "EXPORTFLAGS=%EXPORTFLAGS% -v") else ^
if /i "%~1"=="-v" (set "EXPORTFLAGS=%EXPORTFLAGS% -v") else (
    set /a IDX+=1
    if !IDX!==1 set "ARG1=%~1"
    set "ARGS=%ARGS% %1"
)
shift
goto :collect

:dispatch
if "%ACTION%"==""              goto :daily
if /i "%ACTION%"=="preview"    goto :preview
if /i "%ACTION%"=="setup"      goto :setup
if /i "%ACTION%"=="login"      goto :login
if /i "%ACTION%"=="export"     goto :exportonly
if /i "%ACTION%"=="discover"   goto :discover
if /i "%ACTION%"=="schedule"   goto :schedule
if /i "%ACTION%"=="unschedule" goto :unschedule
if /i "%ACTION%"=="selftest"   goto :selftest
rem Everything else belongs to the COM app: one config, one token, one state database.
if /i "%ACTION%"=="check"      goto :delegate
if /i "%ACTION%"=="sync"       goto :delegate
if /i "%ACTION%"=="post"       goto :delegate
if /i "%ACTION%"=="report"     goto :delegate
if /i "%ACTION%"=="status"     goto :delegate
if /i "%ACTION%"=="doctor"     goto :delegate
if /i "%ACTION%"=="forget"     goto :delegate
if /i "%ACTION%"=="settle"     goto :delegate
if /i "%ACTION%"=="set-token"  goto :delegate
if /i "%ACTION%"=="init"       goto :delegate
if /i "%ACTION%"=="csv"        goto :delegate
if /i "%ACTION%"=="cli"        goto :delegate
if /i "%ACTION%"=="help"       goto :usage
if /i "%ACTION%"=="-h"         goto :usage
if /i "%ACTION%"=="--help"     goto :usage
if /i "%ACTION%"=="/?"         goto :usage
echo Unknown command "%ACTION%".
echo.
goto :usage

rem ---------------------------------------------------------------------------------------------
:daily
rem Defaults to 1 day back, which re-scans yesterday and catches meetings that ended after the
rem previous run. An optional first argument widens it: "meeting2jira-owa 7". Safe to repeat and
rem safe to widen, because the pipeline dedupes on key or content hash.
call :requireapp
if errorlevel 1 exit /b 2
call :daysback "%ARG1%" 1
call :runexport %DAYS%
if errorlevel 1 exit /b %ERRORLEVEL%
call "%APPCMD%" cli daily --input "%EXPORTFILE%"
set "RC=%ERRORLEVEL%"
call :cleanexport %RC%
exit /b %RC%

:preview
call :requireapp
if errorlevel 1 exit /b 2
call :daysback "%ARG1%" 1
call :runexport %DAYS%
if errorlevel 1 exit /b %ERRORLEVEL%
call "%APPCMD%" cli daily --input "%EXPORTFILE%" --dry-run
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
echo Push it with:  meeting2jira-owa cli daily --input "%EXPORTFILE%" --dry-run
exit /b 0

:login
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --login
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:discover
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --debug-endpoints
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:schedule
"%PSEXE%" -NoProfile -NonInteractive -File "%TASKPS%"%ARGS%
exit /b %ERRORLEVEL%

:unschedule
"%PSEXE%" -NoProfile -NonInteractive -File "%TASKPS%" -Unregister
exit /b %ERRORLEVEL%

rem ---------------------------------------------------------------------------------------------
:setup
echo.
echo meeting2jira-owa setup
echo ======================
echo.
call :resolvepython
if errorlevel 1 exit /b 2
echo Step 1 of 4: install Playwright
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
echo Step 2 of 4: register a browser
echo Using the installed Edge avoids downloading one.
%PYCMD% -m playwright install msedge
if errorlevel 1 (
    echo Edge registration failed; falling back to bundled Chromium.
    %PYCMD% -m playwright install chromium
)
echo.
echo Step 3 of 4: sign in to Outlook on the web
echo A browser window will open. Sign in, including MFA, wait for the calendar, then close it.
%PYCMD% "%EXPORTER%" --login
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo.
    echo Sign-in did not complete. Re-run:  meeting2jira-owa login
    exit /b 2
)
echo.
echo Step 4 of 4: Jira config and token
call :requireapp
if errorlevel 1 exit /b 2
call "%APPCMD%" setup
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Setup complete. Next:
    echo   meeting2jira-owa preview     see what would be created
    echo   meeting2jira-owa             do it for real
    echo   meeting2jira-owa schedule    run it automatically on weekdays
) else (
    echo Jira setup reported problems above. Fix them, then:  meeting2jira-owa check
)
exit /b %RC%

:selftest
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
echo Running the OWA mapping tests...
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
echo Odin not found at "%ODIN_APP_DIR%"; skipped its tests.
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
    echo No OWA export has run yet.
    echo.
    exit /b 0
)
echo Last OWA export:
for /f "usebackq delims=" %%L in ("%LASTEXPORT%") do echo   %%L
echo.
exit /b 0

rem ---------------------------------------------------------------------------------------------
:runexport
rem %1 = days back. Sets EXPORTFILE.
call :resolvepython
if errorlevel 1 exit /b 2
if not exist "%EXPORTER%" (
    echo ERROR: export_owa.py not found next to this script.
    exit /b 2
)
if not exist "%EXPORTDIR%" mkdir "%EXPORTDIR%" >nul 2>&1
rem A per-run filename, so two runs cannot overwrite each other mid-flight.
set "EXPORTFILE=%EXPORTDIR%\owa_%RANDOM%%RANDOM%.json"
pushd "%ROOT%"
%PYCMD% "%EXPORTER%" --days-back %1 --out "%EXPORTFILE%"%EXPORTFLAGS%
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" (
    echo.
    echo Export failed. If the session expired:  meeting2jira-owa login
    echo If the calendar API could not be found:  meeting2jira-owa discover
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
echo ERROR: Odin ^(Asgard's apps\odin\odin.cmd^) was not found at "%ODIN_APP_DIR%".
echo.
echo This exporter handles the calendar only; the Jira side is Odin, an Asgard app.
echo Install Asgard, or set ODIN_APP_DIR to the folder containing odin.cmd.
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
echo Install Python 3.8+ from your agency software catalog, then run:  meeting2jira-owa doctor
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
echo meeting2jira-owa - push ended Outlook meetings into Jira, reading the calendar from
echo                    Outlook on the web ^(for "new Outlook", which has no COM^)
echo.
echo   meeting2jira-owa                 daily run: export from OWA, push to Jira
echo   meeting2jira-owa preview         same, but create nothing
echo   meeting2jira-owa setup           first-run walkthrough
echo   meeting2jira-owa login           re-authenticate an expired browser session
echo   meeting2jira-owa export          export only, keep the JSON
echo   meeting2jira-owa discover        print the calendar API requests OWA makes
echo   meeting2jira-owa check           verify config, token and Jira access
echo   meeting2jira-owa status          last-run health, then recent sub-tasks
echo   meeting2jira-owa settle [ID]     posts in doubt, and settling one
echo   meeting2jira-owa doctor          read-only environment report
echo   meeting2jira-owa schedule        register the weekday scheduled task
echo   meeting2jira-owa unschedule      remove the scheduled task
echo   meeting2jira-owa selftest        mapping tests plus the COM app's tests
echo   meeting2jira-owa cli  [args]     Python CLI passthrough
echo.
echo Extra flags accepted by the export commands:
echo   -IncludeOrganizer    include the organizer name ^(extra personal data; off by default^)
echo   -Verbose             more detail, including the endpoint that was discovered
echo.
echo Jira app:  %ODIN_APP_DIR%
echo Config:    %ODINDATA%\config.json
exit /b 0
