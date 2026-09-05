@echo off
setlocal

rem Script folder
set "dp0=%~dp0"

rem Change into the script folder
rem The path is quoted because percent expansion happens before cmd looks for command separators, so an unquoted
rem ampersand in the checkout path would end the echo and run the rest of the line as a command.
echo -- Entering directory "%dp0%"
pushd "%dp0%" || exit /b 1

rem Configure git message template
git config commit.template .gitmessage || goto :fail
git config commit.status false || goto :fail

rem Activate Python virtual environment (create if necessary)
echo -- Checking Python virtual environment...
if not exist .venv\Scripts\activate.bat (
    python -m venv .venv --upgrade-deps || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail

rem "pip install --upgrade pip" cannot replace pip.exe while pip.exe is running; go through python -m instead.
python -m pip install --upgrade pip || goto :fail
pip install -e .[dev] || goto :fail

rem Install pre-commit hooks if necessary
echo -- Checking pre-commit hooks...
pre-commit install || goto :fail

rem Activate a nodejs virtual environment (create if necessary)
echo -- Checking Node virtual environment...
rem HACK: Filter out a stray debug print() left inside nodeenv; see the matching comment in ./bootstrap for the full
rem       story. Unlike the shell pipeline there, a cmd pipeline reports only the last command's exit status, which
rem       would hide a nodeenv failure behind findstr's; capture stdout to a file first so nodeenv's own status stays
rem       observable, then filter the file. findstr /V exits 1 when every line was filtered out, which is the normal
rem       case here, so its status is deliberately not checked. nodeenv's own progress messages go to stderr, which
rem       is not redirected and stays live.
rem
rem       The capture file name carries two %RANDOM% draws so that concurrent bootstraps get one file each instead of
rem       clobbering a shared name, and falls back to the script folder when %TEMP% is unset, which would otherwise
rem       leave the file at the root of the current drive.
set "nodeenv_dir=%TEMP%"
if not defined nodeenv_dir set "nodeenv_dir=."
set "nodeenv_out=%nodeenv_dir%\repospace-nodeenv-%RANDOM%-%RANDOM%.out"
nodeenv -c -p -r scripts\nodeenv\requirements.txt 1>"%nodeenv_out%" || goto :fail
findstr /V /B /C:"{'x86':" "%nodeenv_out%"
del "%nodeenv_out%" 2>nul

rem Change back to the original working directory
echo -- Leaving directory "%dp0%"
popd
exit /b 0

:fail
set "rc=%errorlevel%"
if defined nodeenv_out del "%nodeenv_out%" 2>nul
popd
exit /b %rc%
