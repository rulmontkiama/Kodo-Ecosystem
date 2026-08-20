#!/bin/bash
# =================================================================
# Script de Build Final Pro & Création de Livrables pour Kōdo POS v2.0
# Temps d'exécution ultra-rapide (~25 secondes sur APFS)
# =================================================================

APP_NAME="Kodo_POS"
DMG_NAME="Installation_Kodo_POS.dmg"
WIN_ZIP="Kodo_POS_v1.0.18_Windows_Pack.zip"
SRC_DIR="$(pwd)"
APFS_BUILD="/tmp/kodo_build"

echo "----------------------------------------------------"
echo "🚀 Démarrage du Build Final Kōdo POS v2.0..."
echo "----------------------------------------------------"

# 1. RÉINITIALISATION USINE DE LA BDD (Règle Vierge)
echo "🧹 Réinitialisation usine de la base de données..."
rm -f "$SRC_DIR/kodo_pos.db" "$SRC_DIR/kodo_pos.db-shm" "$SRC_DIR/kodo_pos.db-wal" "$SRC_DIR/ladresse_b.db" "$SRC_DIR/ladresse_b.db-shm" "$SRC_DIR/ladresse_b.db-wal"

PYTHONPATH="$SRC_DIR" python3.12 -c "
import sys, os, sqlite3
sys.path.insert(0, '$SRC_DIR')
import database_manager
database_manager.DB_NAME='kodo_pos.db'
database_manager.initialiser_db()
database_manager.DB_NAME='ladresse_b.db'
database_manager.initialiser_db()

for db_file in ['kodo_pos.db', 'ladresse_b.db']:
    conn = sqlite3.connect(os.path.join('$SRC_DIR', db_file))
    c = conn.cursor()
    user_tables = ['produits', 'clients', 'ventes', 'ligne_ventes', 'stocks', 'sessions_caisse', 'depenses_caisse', 'ledger_caisse', 'rapports_z', 'clotures_z', 'tickets_en_attente']
    existing = [row[0] for row in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
    total_user_rows = sum(c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in user_tables if t in existing)
    conn.close()
    if total_user_rows > 0:
        print(f'❌ ERREUR: La BDD usine {db_file} contient {total_user_rows} donnees utilisateur !')
        sys.exit(1)
    else:
        print(f'✅ BDD usine {db_file} verifiee : 0 donnee utilisateur.')
"

if [ $? -ne 0 ]; then
    echo "❌ Erreur de réinitialisation BDD usine."
    exit 1
fi

# 1.5 COMPILATION & COPIE DU FRONTEND REACT (VITE)
echo "⚡ Copie du frontend React Vite..."
python3 -c "import shutil, glob; src = glob.glob('/Users/kiamarulmont/Desktop/*k*do-pos-3*/dist')[0]; shutil.rmtree('$SRC_DIR/dist', ignore_errors=True); shutil.copytree(src, '$SRC_DIR/dist')" 2>/dev/null || true

# 2. PRÉPARATION DU DOSSIER DE BUILD APFS
echo "📦 Copie miroir vers APFS pour la compilation PyInstaller..."
rm -rf "$APFS_BUILD"
mkdir -p "$APFS_BUILD"
cp -R "$SRC_DIR/"* "$APFS_BUILD/" 2>/dev/null || true

# 3. COMPILATION PYINSTALLER SUR APFS
echo "📦 Compilation PyInstaller..."
DIST_DIR="/tmp/kodo_dist_$$"
WORK_DIR="/tmp/kodo_work_$$"
rm -rf "$DIST_DIR" "$WORK_DIR"

cd "$APFS_BUILD"
python3.12 -m PyInstaller --noconfirm --distpath "$DIST_DIR" --workpath "$WORK_DIR" Kodo_POS.spec

if [ $? -ne 0 ]; then
    echo "❌ Erreur lors de la compilation PyInstaller."
    exit 1
fi
echo "✅ Compilation PyInstaller réussie."

# 4. PRÉPARATION DU PACK D'INSTALLATION MACOS
echo "📂 Préparation du pack d'installation macOS..."
BUILD_DIR="/tmp/build_kodo_pack"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/Installation_Pack"
PACK_DIR="$BUILD_DIR/Installation_Pack"

cp -R "$DIST_DIR/$APP_NAME.app" "$PACK_DIR/"
ln -s /Applications "$PACK_DIR/Applications"

# Notice d'installation
cat <<INFO > "$PACK_DIR/IMPORTANT_LISEZ_MOI.txt"
===========================================================
        NOTICE D'INSTALLATION - KŌDO POS v2.0
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

# Installation directe dans /Applications
echo "📲 Installation directe dans /Applications..."
rm -rf "/Applications/$APP_NAME.app"
cp -R "$PACK_DIR/$APP_NAME.app" /Applications/
codesign --force --deep --sign - "/Applications/$APP_NAME.app" 2>/dev/null || true
xattr -cr "/Applications/$APP_NAME.app" || true

# 5. GÉNÉRATION DE INSTALLATION_KODO_POS_MACOS.ZIP VIA DITTO
echo "📦 Génération de Installation_Kodo_POS_macOS.zip..."
rm -f "$SRC_DIR/Installation_Kodo_POS_macOS.zip" ~/Desktop/Installation_Kodo_POS_macOS.zip
ditto -c -k --sequesterRsrc "$PACK_DIR" ~/Desktop/Installation_Kodo_POS_macOS.zip
cp ~/Desktop/Installation_Kodo_POS_macOS.zip "$SRC_DIR/Installation_Kodo_POS_macOS.zip" 2>/dev/null || true

# 6. GÉNÉRATION DU DMG MACOS
echo "💿 Création de l'image disque DMG macOS..."
rm -rf /tmp/dmg_build && mkdir -p /tmp/dmg_build
cp -R "$PACK_DIR/$APP_NAME.app" /tmp/dmg_build/
cp "$PACK_DIR/IMPORTANT_LISEZ_MOI.txt" /tmp/dmg_build/ 2>/dev/null || true
rm -f "$SRC_DIR/$DMG_NAME" ~/Desktop/"$DMG_NAME"
hdiutil create -volname "Kodo POS" -srcfolder /tmp/dmg_build -ov -format UDZO ~/Desktop/"$DMG_NAME"
cp ~/Desktop/"$DMG_NAME" "$SRC_DIR/$DMG_NAME" 2>/dev/null || true
rm -rf /tmp/dmg_build "$DIST_DIR" "$WORK_DIR" "$BUILD_DIR" "$APFS_BUILD"

# 7. GÉNÉRATION DU PACK WINDOWS (Kodo_POS_v1.0.18_Windows_Pack.zip)
echo "🪟 Préparation du pack de build Windows ($WIN_ZIP)..."
rm -f "$SRC_DIR/$WIN_ZIP" ~/Desktop/"$WIN_ZIP"
cd "$SRC_DIR" && zip -r -1 "$SRC_DIR/$WIN_ZIP" launch_app.py server_pos.py database_manager.py audit_trail.py backup_manager.py ticket_printer.py pdf_generator.py license_manager.py shopify_sync.py firebase_sync.py Kodo_POS_Windows.spec build_windows.bat logo.png logo_ticket.png instagram_block.png dist kodo_pos.db ladresse_b.db plan_permissions.json kodo_core core services views 2>/dev/null || true
cp "$SRC_DIR/$WIN_ZIP" ~/Desktop/"$WIN_ZIP" 2>/dev/null || true

echo "----------------------------------------------------"
echo "✨ LIVRAISON TERMINÉE AVEC SUCCÈS !"
echo "📍 macOS ZIP (ditto) : ~/Desktop/Installation_Kodo_POS_macOS.zip"
echo "📍 macOS DMG : ~/Desktop/$DMG_NAME"
echo "📍 Windows Build Pack : ~/Desktop/$WIN_ZIP"
echo "----------------------------------------------------"
