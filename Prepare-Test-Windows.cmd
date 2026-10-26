@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Install 64-bit Python 3.12 or newer from python.org and enable Add Python to PATH.
  exit /b 1
)
python -c "import sys,struct; sys.exit(0 if sys.platform=='win32' and sys.version_info.__ge__((3,12)) and sys.version_info.releaselevel=='final' and struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo A stable 64-bit Windows Python 3.12 or newer is required.
  exit /b 1
)
if not exist ".venv-test\Scripts\python.exe" (
  python -m venv .venv-test
  if errorlevel 1 exit /b 1
)
if not exist ".venv-test\beamer-ready" (
  ".venv-test\Scripts\python.exe" -m pip install -r win_app\requirements-win.txt
  if errorlevel 1 exit /b 1
  type nul > ".venv-test\beamer-ready"
)
exit /b 0
