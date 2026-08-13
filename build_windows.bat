@echo off
echo ===================================================
echo   COMPILATION WINDOWS KODO POS (Kodo_POS.exe)
echo ===================================================

echo [1/3] Initialisation de la base de donnees usine...
python -c "import database_manager; database_manager.DB_NAME='kodo_pos.db'; database_manager.initialiser_db(); database_manager.DB_NAME='ladresse_b.db'; database_manager.initialiser_db()"

echo [2/3] Compilation PyInstaller Windows...
pyinstaller --noconfirm Kodo_POS_Windows.spec

echo [3/3] Creation du ZIP d'installation Windows...
powershell Compress-Archive -Path dist\Kodo_POS\* -DestinationPath Kodo_POS_v1.0.3_Windows_Portable.zip -Force

echo ===================================================
echo   COMPILATION WINDOWS TERMINÉE AVEC SUCCÈS !
echo ===================================================
pause
