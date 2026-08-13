@echo off
title One-Click Start: ComfyUI + ComfyChat
setlocal

rem ================================================================
rem  Portable one-click launcher. Works from any drive/path (C:, D:, E:)
rem    ComfyUI root = parent of this folder (or set COMFYUI_ROOT)
rem    ComfyChat    = this folder
rem    Python       = auto-detected (override with COMFYCHAT_PYTHON)
rem ================================================================

rem Clear broken proxy env vars (inherited by child processes)
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
set ALL_PROXY=
set all_proxy=

set "SELF=%~dp0"
set "COMFYUI_DIR=%~dp0.."
if defined COMFYUI_ROOT set "COMFYUI_DIR=%COMFYUI_ROOT%"
if not exist "%COMFYUI_DIR%\main.py" (
    echo.
    echo [ERROR] ComfyUI not found: %COMFYUI_DIR%\main.py
    echo         Put this script inside the comfy_chat folder, with ComfyUI
    echo         as its sibling folder; or run:  set COMFYUI_ROOT=D:\path\to\comfyui
    echo.
    pause
    exit /b 1
)

rem ---- locate Python ----
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

echo [INFO] ComfyUI dir: %COMFYUI_DIR%
echo [INFO] Python: %PYTHON_EXE%

echo ================================================
echo   ComfyUI + ComfyChat One-Click Start
echo   Services run in the background of this window.
echo   Closing this window stops both services.
echo ================================================

rem --- 1. Start ComfyUI (skip if already running on 8188) ---
netstat -ano | findstr ":8188 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] ComfyUI already running on 8188
) else (
    echo [..] Starting ComfyUI...
    pushd "%COMFYUI_DIR%"
    start "ComfyUI" /b "%PYTHON_EXE%" main.py --listen 127.0.0.1 --port 8188 --enable-manager
    popd
)

rem --- 2. Wait for ComfyUI to be ready (max ~90s) ---
echo [..] Waiting for ComfyUI...
set /a tries=0
:wait_comfy
>nul 2>&1 curl -s --noproxy "*" -o NUL --max-time 2 http://127.0.0.1:8188/system_stats
if %errorlevel%==0 goto comfy_ready
set /a tries+=1
if %tries% geq 30 (
    echo [!!] ComfyUI did not become ready in time, continuing anyway...
    goto comfy_ready
)
timeout /t 3 /nobreak >nul
goto wait_comfy

:comfy_ready
echo [OK] ComfyUI ready on http://127.0.0.1:8188

rem ---- dependency check: auto-install if missing (Aliyun mirror) ----
"%PYTHON_EXE%" -c "import flask, requests, PIL" >nul 2>&1
if errorlevel 1 (
    echo [..] Missing dependencies, installing from Aliyun mirror...
    "%PYTHON_EXE%" -m pip install -r "%SELF%requirements.txt" -i https://mirrors.aliyun.com/pypi/simple/ -q
    "%PYTHON_EXE%" -c "import flask, requests, PIL" >nul 2>&1
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependencies still missing. Install manually:
        echo   "%PYTHON_EXE%" -m pip install -r "%SELF%requirements.txt"
        echo.
        pause
        exit /b 1
    )
)

rem --- 3. Start ComfyChat (skip if already running on 5001) ---
netstat -ano | findstr ":5001 " | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo [OK] ComfyChat already running on 5001
) else (
    echo [..] Starting ComfyChat...
    pushd "%SELF%"
    start "ComfyChat" /b "%PYTHON_EXE%" app.py
    popd
)

rem --- 4. Open browser ---
timeout /t 2 /nobreak >nul
echo [OK] Opening browser at http://127.0.0.1:5001
start "" http://127.0.0.1:5001

echo.
echo ================================================
echo   All done! ComfyUI + ComfyChat are running
echo   in the background of this window.
echo     ComfyUI   - http://127.0.0.1:8188
echo     ComfyChat - http://127.0.0.1:5001
echo   Closing this window stops both services.
echo ================================================
pause
