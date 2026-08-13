@echo off
title ComfyChat (ComfyUI Chat UI)
setlocal
rem ---- portable launcher: works from any drive/path (C:, D:, E:, ...) ----
cd /d "%~dp0"

rem Clear broken proxy env vars (e.g. a dead proxy injected on 127.0.0.1:33210)
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=

rem ---- locate Python (override with COMFYCHAT_PYTHON) ----
set "PYTHON_EXE="
if defined COMFYCHAT_PYTHON (
    if exist "%COMFYCHAT_PYTHON%" (set "PYTHON_EXE=%COMFYCHAT_PYTHON%" & goto :py_found)
)
for /f "delims=" %%i in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do (set "PYTHON_EXE=%%i" & goto :py_found)
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (set "PYTHON_EXE=%%i" & goto :py_found)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe" & goto :py_found)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe" & goto :py_found)
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe" & goto :py_found)
if exist "%ProgramFiles%\Python312\python.exe" (set "PYTHON_EXE=%ProgramFiles%\Python312\python.exe" & goto :py_found)
if exist "%ProgramFiles%\Python311\python.exe" (set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe" & goto :py_found)
for /f "delims=" %%i in ('where python 2^>nul') do (set "PYTHON_EXE=%%i" & goto :py_found)
echo.
echo [ERROR] Python not found. Install Python 3.10-3.12 and check "Add to PATH",
echo         or run:  set COMFYCHAT_PYTHON=C:\path\to\python.exe
echo.
pause
exit /b 1
:py_found

echo [INFO] Using Python: %PYTHON_EXE%

rem ---- dependency check: auto-install if missing (Aliyun mirror) ----
"%PYTHON_EXE%" -c "import flask, requests, PIL" >nul 2>&1
if errorlevel 1 (
    echo [..] Missing dependencies, installing from Aliyun mirror...
    "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt" -i https://mirrors.aliyun.com/pypi/simple/ -q
    "%PYTHON_EXE%" -c "import flask, requests, PIL" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependencies still missing. Install manually:
        echo   "%PYTHON_EXE%" -m pip install -r "%~dp0requirements.txt"
        echo.
        pause
        exit /b 1
    )
)

echo [INFO] Starting ComfyChat: http://127.0.0.1:5001/
"%PYTHON_EXE%" app.py
pause
