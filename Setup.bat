@echo off
rem Setup.bat - one-time setup for Plex Tools. Double-click it; run it again any time to repair or update.
rem   1) checks Python (3.12 or newer from python.org)
rem   2) creates a private Python environment ".venv" in this folder and installs the packages into it
rem      (nothing is installed into your system Python)
rem   3) offers the helper programs MediaInfo and Tesseract OCR (via winget)
rem   4) creates your settings file config.json from config.example.json and opens it in Notepad
setlocal
cd /d "%~dp0"
title Plex Tools - setup
echo.
echo  Plex Tools - setup
echo  ------------------

rem ---- 1. find Python: the "py" launcher first, else python on PATH
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto nopython
for /f "delims=" %%v in ('%PY% --version') do echo  Found %%v

rem ---- 2. virtual environment + packages
echo.
if exist ".venv\Scripts\python.exe" (
    echo  1/3 Python environment .venv already exists - updating it.
) else (
    echo  1/3 Creating the Python environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo  Could not create .venv - see the message above.
        goto fail
    )
)
echo      Installing packages: requests, openai, openpyxl ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo  Package installation failed - check your internet connection and run Setup.bat again.
    goto fail
)
".venv\Scripts\python.exe" -c "import requests, openai, openpyxl, tkinter" 2>nul
if errorlevel 1 (
    echo  The packages or tkinter are missing. Reinstall Python from python.org with "tcl/tk" ticked.
    goto fail
)
echo      Packages OK.

rem ---- 3. helper programs
echo.
echo  2/3 Helper programs (installed with winget, the Windows package manager):
echo      MediaInfo - measures video resolution when Plex doesn't know it (recommended)
echo      Tesseract - reads text on posters, only for artwork method B (optional)
where winget >nul 2>&1
if errorlevel 1 (
    echo      winget is not available - install them by hand if you want them ^(see README^).
) else (
    choice /C YN /M "     Install MediaInfo"
    if not errorlevel 2 winget install --id MediaArea.MediaInfo -e --accept-source-agreements --accept-package-agreements
    choice /C YN /M "     Install Tesseract OCR"
    if not errorlevel 2 winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements
)

rem ---- 4. settings file
echo.
echo  3/3 Settings file
if exist config.json (
    echo      config.json already exists - kept. Use "Edit settings" in Plex Tools to change it.
) else (
    copy /y config.example.json config.json >nul
    echo      config.json created. It opens in Notepad now:
    echo      paste your TMDB key ^(and any other keys you have^) between the quotes, save, close.
    notepad config.json
)

echo.
echo  Setup finished. Start the tools by double-clicking "Plex Tools.bat".
echo.
pause
exit /b 0

:nopython
echo.
echo  Python 3.12 or newer was not found.
echo  Install it from https://www.python.org/downloads/  - in the installer tick
echo  "Add python.exe to PATH" - then run Setup.bat again.
start "" https://www.python.org/downloads/
:fail
echo.
pause
exit /b 1
