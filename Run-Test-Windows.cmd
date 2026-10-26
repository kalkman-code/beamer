@echo off
setlocal
cd /d "%~dp0"
echo Quit the regular Beamer before starting this test version.
if exist "dist-windows\Beamer.exe" (
  "dist-windows\Beamer.exe" --config "%CD%\test-data\settings.json"
  goto done
)
call Prepare-Test-Windows.cmd
if errorlevel 1 goto done
set "PYTHONPATH=%CD%"
".venv-test\Scripts\python.exe" win_app\kvm_bridge_win.py --config "%CD%\test-data\settings.json"
:done
pause
