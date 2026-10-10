@echo off
rem =============================================================================================
rem  meeting2jira - single entry point for Windows.
rem
rem    meeting2jira                 do the daily thing: export yesterday+today, push to Jira
rem    meeting2jira preview         same, but create nothing (dry run)
rem    meeting2jira setup           first-run walkthrough: config, token, connectivity check
rem    meeting2jira check           verify config, token, Jira reachability, parents, sub-task type
rem    meeting2jira status          last-run health, then recently created sub-tasks
rem    meeting2jira doctor          read-only environment report (Test-Environment.ps1)
rem    meeting2jira schedule        register the weekday scheduled task
rem    meeting2jira unschedule      remove it
rem    meeting2jira csv <file>      push from an Outlook CSV export instead of COM
rem    meeting2jira selftest        unit tests plus the Windows-only checks
rem    meeting2jira sync  [args]    Invoke-MeetingSync.ps1 passthrough, e.g. sync -DaysBack 7
rem    meeting2jira cli   [args]    Python CLI passthrough, e.g. cli forget PROJ-501
rem
rem  There is deliberately no -ExecutionPolicy Bypass in here. If the scripts will not run, fix
rem  the cause (usually the internet-zone mark on downloaded files) rather than bypassing the
rem  policy: `meeting2jira doctor` reports what to do.
rem =============================================================================================
setlocal EnableExtensions

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PSEXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PSRUN=-NoProfile -NonInteractive -File"
set "SRC=%ROOT%\src"
set "SYNCPS=%SRC%\windows\Invoke-MeetingSync.ps1"
set "TASKPS=%SRC%\windows\Register-MeetingSyncTask.ps1"
set "DOCTORPS=%SRC%\windows\Test-Environment.ps1"
set "CHECKPS=%ROOT%\tools\Invoke-WindowsChecks.ps1"
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
if "%ACTION%"==""           goto :daily
if /i "%ACTION%"=="preview"    goto :preview
if /i "%ACTION%"=="setup"      goto :setup
if /i "%ACTION%"=="doctor"     goto :doctor
if /i "%ACTION%"=="schedule"   goto :schedule
if /i "%ACTION%"=="unschedule" goto :unschedule
if /i "%ACTION%"=="selftest"   goto :selftest
if /i "%ACTION%"=="csv"        goto :csv
if /i "%ACTION%"=="sync"       goto :syncthrough
if /i "%ACTION%"=="cli"        goto :clithrough
if /i "%ACTION%"=="check"      goto :simplecli
if /i "%ACTION%"=="status"     goto :simplecli
if /i "%ACTION%"=="init"       goto :simplecli
if /i "%ACTION%"=="set-token"  goto :simplecli
if /i "%ACTION%"=="forget"     goto :simplecli
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
rem is safe: anything already pushed is skipped via the local state database.
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%" -DaysBack 1
exit /b %ERRORLEVEL%

:preview
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%" -DaysBack 1 -DryRun
exit /b %ERRORLEVEL%

:syncthrough
call :requireconfig
if errorlevel 1 exit /b 2
"%PSEXE%" %PSRUN% "%SYNCPS%"%ARGS%
exit /b %ERRORLEVEL%

:csv
if "%ARG1%"=="" (
    echo Usage: meeting2jira csv "C:\path\to\calendar.csv" [-DryRun]
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
echo meeting2jira setup
echo ==================
echo.
call :resolvepython
if errorlevel 1 exit /b 2
echo Step 1 of 4: environment check
"%PSEXE%" %PSRUN% "%DOCTORPS%"
echo.
echo Step 2 of 4: create the config file
pushd "%ROOT%"
set "PYTHONPATH=%SRC%"
%PYCMD% -m meeting2jira init
popd
echo.
echo Step 3 of 4: edit the config. At minimum set jira.base_url and jira.default_parent,
echo and review filters.skip_subject_contains ^(OOO, PTO, holiday...^).
echo Opening the config in Notepad. Save and close it to continue.
echo.
start /wait notepad "%CONFIG%"
echo.
echo Step 4 of 4: store your Jira token, then verify the connection
pushd "%ROOT%"
set "PYTHONPATH=%SRC%"
%PYCMD% -m meeting2jira set-token
if errorlevel 1 (
    popd
    echo.
    echo Token was not stored. Re-run:  meeting2jira set-token
    exit /b 2
)
%PYCMD% -m meeting2jira check
set "RC=%ERRORLEVEL%"
popd
echo.
if "%RC%"=="0" (
    echo Setup complete. Next:
    echo   meeting2jira preview     see what would be created
    echo   meeting2jira             do it for real
    echo   meeting2jira schedule    run it automatically on weekdays
) else (
    echo Checks reported problems above. Fix them, then run:  meeting2jira check
)
exit /b %RC%

rem ---------------------------------------------------------------------------------------------
:simplecli
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
set "PYTHONPATH=%SRC%"
%PYCMD% -m meeting2jira %ACTION%%ARGS%
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:clithrough
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
set "PYTHONPATH=%SRC%"
%PYCMD% -m meeting2jira%ARGS%
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%

:selftest
call :resolvepython
if errorlevel 1 exit /b 2
pushd "%ROOT%"
set "PYTHONPATH=%SRC%"
echo Running unit tests...
%PYCMD% -m unittest discover -s tests
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" exit /b %RC%
echo.
echo Running the Windows-only checks...
"%PSEXE%" %PSRUN% "%CHECKPS%"
exit /b %ERRORLEVEL%

rem ---------------------------------------------------------------------------------------------
:requireconfig
if exist "%CONFIG%" exit /b 0
echo No config found at "%CONFIG%".
echo Run this first:  meeting2jira setup
exit /b 1

:resolvepython
rem Mirrors Resolve-Python in the .ps1 files. Existence is not proof: a real agency install often has
rem py.exe present with no 3.x registered (so `py -3` fails), or a working Python that was never added
rem to PATH, or a 2.x on PATH ahead of a 3.x. So every candidate is executed against a probe script
rem and must report 3.8+. The first that actually works wins, and PYTHONTRIED records the rest.
rem
rem PYCMD is quoted, because an install under "C:\Program Files\..." otherwise breaks on the space.
if defined PYCMD exit /b 0
set "PYCMD="
set "PYTHONTRIED="
set "PYPROBE=%TEMP%\m2j-pyprobe-%RANDOM%.py"
> "%PYPROBE%" echo import sys
>>"%PYPROBE%" echo sys.exit(0 if sys.version_info ^>= (3, 8) else 3)

rem 1. the launcher, verified to actually produce a 3.x
for /f "delims=" %%I in ('where py.exe 2^>nul') do call :trypython "%%I" "-3" && goto :resolvedpython
rem 2. anything on PATH
for /f "delims=" %%I in ('where python3.exe 2^>nul') do call :trypython "%%I" "" && goto :resolvedpython
for /f "delims=" %%I in ('where python.exe 2^>nul') do call :trypython "%%I" "" && goto :resolvedpython
rem 3. the registry, where an installer records itself even when PATH was left alone
for /f "tokens=2,*" %%A in ('reg query "HKCU\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| find /i "ExecutablePath"') do call :trypython "%%B" "" && goto :resolvedpython
rem 4. the usual directories, for an install that registered nothing
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython
for /d %%D in ("%ProgramFiles%\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython
for /d %%D in ("C:\Python3*") do call :trypython "%%~fD\python.exe" "" && goto :resolvedpython

del "%PYPROBE%" >nul 2>&1
echo ERROR: no working Python 3.8+ found. Candidates tried:
if defined PYTHONTRIED echo %PYTHONTRIED%
echo.
echo Searched PATH, the registry, and the usual install directories.
echo Install Python 3.8+ from your agency software catalog, then run:  meeting2jira doctor
exit /b 1

:resolvedpython
del "%PYPROBE%" >nul 2>&1
exit /b 0

:trypython
rem %1 = candidate exe, %2 = prefix ("-3" or ""). Sets PYCMD and returns 0 only if it really runs.
if defined PYCMD exit /b 0
set "CAND=%~1"
set "PRE=%~2"
if "%CAND%"=="" exit /b 1
rem The Store alias stub reports success to `where` but opens the Store instead of running Python.
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
echo meeting2jira - push ended Outlook meetings into Jira as sub-tasks
echo.
echo   meeting2jira                 daily run: export yesterday and today, push to Jira
echo   meeting2jira preview         same, but create nothing
echo   meeting2jira setup           first-run walkthrough
echo   meeting2jira check           verify config, token and Jira access
echo   meeting2jira status          last-run health, then recent sub-tasks
echo   meeting2jira doctor          read-only environment report
echo   meeting2jira schedule        register the weekday scheduled task
echo   meeting2jira unschedule      remove the scheduled task
echo   meeting2jira csv ^<file^>      push from an Outlook CSV export
echo   meeting2jira selftest        unit tests plus the Windows-only checks
echo   meeting2jira sync [args]     Invoke-MeetingSync.ps1 passthrough
echo   meeting2jira cli  [args]     Python CLI passthrough
echo.
echo Config: %CONFIG%
exit /b 0
