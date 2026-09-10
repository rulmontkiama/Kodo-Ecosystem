@echo off
chcp 65001 >nul
cls
echo ========================================================
echo               DEMARRAGE DE KODO POS
echo ========================================================
echo.

where python >nul 2>&1
if %errorlevel% equ 0 goto run_python

where py >nul 2>&1
if %errorlevel% equ 0 goto run_py

echo [ATTENTION] Python n'est pas detecte sur ce PC Windows.
echo.
echo Tentative d'installation automatique via Windows Winget...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Si l'installation a reussi, fermez cette fenetre et relancez Lancer_Kodo.
echo Sinon, installez Python depuis le Microsoft Store ou python.org en cochant 'Add Python to PATH'.
goto end

:run_python
echo [OK] Python detecte. Lancement de Kodo POS...
python launch_app.py
goto end

:run_py
echo [OK] Python Launcher detecte. Lancement de Kodo POS...
py launch_app.py
goto end

:end
echo.
pause
