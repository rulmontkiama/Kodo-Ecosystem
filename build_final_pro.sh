#!/bin/bash
# =================================================================
# Script de Build Rapide & Création de DMG pour Kōdo POS
# Temps d'exécution : ~20 secondes
# =================================================================

APP_NAME="Kodo_POS"
DMG_NAME="Installation_Kodo_POS.dmg"
SRC_DIR="$(pwd)"
BUILD_DIR="/tmp/build_kodo"

echo "----------------------------------------------------"
echo "🚀 Démarrage du Build Rapide Kōdo POS..."
echo "----------------------------------------------------"

# 1. RÉINITIALISATION USINE DE LA BDD (Règle Vierge)
echo "🧹 Réinitialisation de la base de données usine..."
rm -f "$SRC_DIR/kodo_pos.db" "$SRC_DIR/kodo_pos.db-shm" "$SRC_DIR/kodo_pos.db-wal" "$SRC_DIR/ladresse_b.db"
python3.12 -c "import database_manager; database_manager.DB_NAME='kodo_pos.db'; database_manager.initialiser_db(); database_manager.DB_NAME='ladresse_b.db'; database_manager.initialiser_db()"

# 1.5 COMPILATION DU FRONTEND REACT (VITE)
echo "⚡ Compilation du frontend React Vite..."
cd "/Users/kiamarulmont/Desktop/kōdo-pos-3" && bun run build
rm -rf "$SRC_DIR/dist"
cp -XR "/Users/kiamarulmont/Desktop/kōdo-pos-3/dist" "$SRC_DIR/dist" || true
cd "$SRC_DIR"

# 2. COMPILATION PYINSTALLER HAUTE VITESSE
echo "📦 Nettoyage pré-compilation..."
rm -rf "$SRC_DIR/build"
dot_clean "$SRC_DIR" 2>/dev/null || true

echo "📦 Compilation PyInstaller..."
DIST_DIR="/tmp/kodo_dist_$$"
WORK_DIR="/tmp/kodo_work_$$"
rm -rf "$DIST_DIR" "$WORK_DIR"
python3.12 -m PyInstaller --noconfirm --distpath "$DIST_DIR" --workpath "$WORK_DIR" Kodo_POS.spec

if [ $? -ne 0 ]; then
    echo "❌ Erreur lors de la compilation."
    exit 1
fi
echo "✅ Compilation réussie."

# 3. PRÉPARATION DU DOSSIER D'INSTALLATION
echo "📂 Préparation du pack d'installation..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/Installation_Pack"
PACK_DIR="$BUILD_DIR/Installation_Pack"

cp -R "$DIST_DIR/$APP_NAME.app" "$PACK_DIR/"
ln -s /Applications "$PACK_DIR/Applications"

# Notice d'installation
cat <<INFO > "$PACK_DIR/IMPORTANT_LISEZ_MOI.txt"
===========================================================
        NOTICE D'INSTALLATION - KŌDO POS
===========================================================

Bienvenue dans votre système de caisse Kōdo POS.

POUR INSTALLER :
1. Faites glisser l'icône "Kodo_POS" vers le dossier "Applications".

PROCÉDURE DE PREMIER LANCEMENT (macOS) :
1. Allez dans le dossier /Applications.
2. Faites un CLIC DROIT sur "Kodo_POS".
3. Choisissez "OUVRIR".
4. Cliquez sur "OUVRIR" dans la fenêtre de sécurité.

IDENTIFIANTS PAR DÉFAUT :
Code PIN : 0000
===========================================================
INFO

# Signature ad-hoc & nettoyage des attributs étendus
codesign --force --deep --sign - "$PACK_DIR/$APP_NAME.app" 2>/dev/null || true
xattr -cr "$PACK_DIR" || true


# 4. GÉNÉRATION DU DMG
echo "💿 Création de l'image disque .dmg..."
rm -f "$SRC_DIR/$DMG_NAME" ~/Desktop/"$DMG_NAME"
hdiutil create -volname "Kodo POS" -srcfolder "$PACK_DIR" -ov -format UDRW "$SRC_DIR/$DMG_NAME"
cp "$SRC_DIR/$DMG_NAME" ~/Desktop/"$DMG_NAME" 2>/dev/null || true

# 6. GÉNÉRATION DU PACK WINDOWS (Pack prêt à exécuter / compiler sur Windows)
echo "🪟 Préparation du pack de build Windows..."
rm -f "$SRC_DIR/Kodo_POS_Windows_Pack.zip" ~/Desktop/Kodo_POS_v1.0.3_Windows_Pack.zip
cd "$SRC_DIR" && zip -r -1 "$SRC_DIR/Kodo_POS_Windows_Pack.zip" launch_app.py server_pos.py database_manager.py audit_trail.py backup_manager.py ticket_printer.py pdf_generator.py Kodo_POS_Windows.spec build_windows.bat logo.png logo_ticket.png instagram_block.png dist kodo_pos.db ladresse_b.db plan_permissions.json 2>/dev/null || true
cp "$SRC_DIR/Kodo_POS_Windows_Pack.zip" ~/Desktop/Kodo_POS_v1.0.3_Windows_Pack.zip 2>/dev/null || true

echo "----------------------------------------------------"
echo "✨ LIVRAISON TERMINÉE AVEC SUCCÈS !"
echo "📍 Apple macOS DMG : ~/Desktop/$DMG_NAME"
echo "📍 Windows Build Pack : ~/Desktop/Kodo_POS_v1.0.3_Windows_Pack.zip"
echo "----------------------------------------------------"
