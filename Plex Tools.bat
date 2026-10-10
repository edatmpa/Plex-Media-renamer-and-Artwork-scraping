@echo off
rem Plex Tools - Plex rename and metadata repair tools.
rem Opens the start window: 1) rename a library folder  2) fix one film  3) artwork for a whole library.
rem Uses the Python environment made by Setup.bat (.venv in this folder); without it, the Python on PATH.
setlocal
set "PYTHONNOUSERSITE=1"
set "HERE=%~dp0"
if exist "%HERE%.venv\Scripts\pythonw.exe" (
    start "" "%HERE%.venv\Scripts\pythonw.exe" "%HERE%launcher.py"
    goto end
)
where pythonw >nul 2>&1 && (
    start "" pythonw "%HERE%launcher.py"
) || (
    where python >nul 2>&1 || (
        echo Python was not found. Run Setup.bat first.
        pause
        goto end
    )
    python "%HERE%launcher.py"
    if errorlevel 1 pause
)
:end
endlocal
