@echo off
setlocal
cd /d "%~dp0"
call Prepare-Test-Windows.cmd
if errorlevel 1 goto done
set "PYTHONPATH=%CD%;%CD%\win_app;%CD%\win_app\tests"
".venv-test\Scripts\python.exe" -m unittest core.tests.test_keyboard_sync core.tests.test_protocol_v6 core.tests.test_i18n core.tests.test_i18n_coverage
if errorlevel 1 goto done
".venv-test\Scripts\python.exe" -m unittest test_keyboard_language test_input_injector test_links test_app_config
if errorlevel 1 goto done
pushd win_app
"..\.venv-test\Scripts\python.exe" -m PyInstaller --clean --noconfirm --distpath ..\dist-windows --workpath ..\build-windows build.spec
popd
echo If the build succeeded, run Run-Test-Windows.cmd.
:done
pause
