@echo off
chcp 65001 >nul
title Kōdo POS
echo ========================================================
echo               DÉMARRAGE DE KŌDO POS
echo ========================================================
echo.
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Python n'est pas détecté sur ce PC !
    echo Veuillez installer Python (version 3.10 ou supérieure)
    echo en cochant bien la case "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo [INFO] Démarrage du serveur et de l'interface Kōdo POS...
python launch_app.py
if %errorlevel% neq 0 (
    echo.
    echo [ERREUR] L'application s'est arrêtée avec une erreur.
    pause
)
