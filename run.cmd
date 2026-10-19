@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
echo [pycut] System: Windows

call :find_python
if not errorlevel 1 goto python_ready
echo [pycut] Python is missing. Installing Python 3.13...
where winget >nul 2>nul
if errorlevel 1 goto missing_winget
winget install --id Python.Python.3.13 --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_failed
call :refresh_path
call :find_python
if errorlevel 1 (
    echo [pycut] Python installation could not be verified. Reopen the terminal and retry.
    exit /b 1
)

:python_ready
echo [pycut] Python: "%PYCUT_PY%"
if not exist .env if exist .env.example (
    copy /y .env.example .env >nul
    if errorlevel 1 exit /b 1
    echo [pycut] Created .env. Configure your main gateway key there.
)

"%PYCUT_PY%" -c "import cv2,numpy,curses,textual,textual_fspicker; assert cv2.__version__.split('.')[0]=='4'; assert hasattr(cv2,'CascadeClassifier')" >nul 2>nul
if not errorlevel 1 goto dependencies_ready
echo [pycut] Installing Python dependencies...
"%PYCUT_PY%" -m pip --version >nul 2>nul
if not errorlevel 1 goto pip_ready
"%PYCUT_PY%" -m ensurepip --upgrade
if errorlevel 1 goto install_failed
:pip_ready
"%PYCUT_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto install_failed
"%PYCUT_PY%" -c "import cv2,numpy,curses,textual,textual_fspicker; assert cv2.__version__.split('.')[0]=='4'; assert hasattr(cv2,'CascadeClassifier')" >nul 2>nul
if errorlevel 1 goto install_failed

:dependencies_ready
echo [pycut] Python dependencies ready.
call :check_ffmpeg
if not errorlevel 1 goto environment_ready
echo [pycut] Installing FFmpeg and ffprobe...
where winget >nul 2>nul
if errorlevel 1 goto missing_winget
winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_failed
call :refresh_path
call :check_ffmpeg
if errorlevel 1 (
    echo [pycut] FFmpeg is still unavailable. Check PATH and PYCUT_FFMPEG/PYCUT_FFPROBE in .env.
    exit /b 1
)

:environment_ready
echo [pycut] FFmpeg and ffprobe ready.
"%PYCUT_PY%" -c "from pycut import config; print(config.gateway_summary())"
if errorlevel 1 exit /b 1
if /i "%~1"=="desktop-server" (
    "%PYCUT_PY%" -m pycut.desktop
    exit /b %errorlevel%
)
if /i "%~1"=="desktop-worker" (
    "%PYCUT_PY%" -m pycut.desktop --worker "%~2"
    exit /b %errorlevel%
)
"%PYCUT_PY%" pycut.py %*
exit /b %errorlevel%

:find_python
set "PYCUT_PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PYCUT_PY=%~dp0.venv\Scripts\python.exe"
if defined PYCUT_PY exit /b 0
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYCUT_PY=%%P"
if defined PYCUT_PY exit /b 0
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PYCUT_PY=%%P"
if defined PYCUT_PY exit /b 0
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYCUT_PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if defined PYCUT_PY exit /b 0
if exist "%ProgramFiles%\Python313\python.exe" set "PYCUT_PY=%ProgramFiles%\Python313\python.exe"
if defined PYCUT_PY exit /b 0
exit /b 1

:check_ffmpeg
"%PYCUT_PY%" -c "import subprocess; from pycut import config; [subprocess.run([config._media_tool(n),'-version'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15) for n in ('ffmpeg','ffprobe')]" >nul 2>nul
exit /b %errorlevel%

:refresh_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P;%PATH%"
set "PATH=%LocalAppData%\Microsoft\WinGet\Links;%PATH%"
exit /b 0

:missing_winget
echo [pycut] winget is missing. Install Microsoft App Installer and retry.
exit /b 1

:install_failed
echo [pycut] Environment installation failed. Check the installation log, network and permissions.
exit /b 1
