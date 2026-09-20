#!/bin/bash
# =================================================================
# Script de Build Final Pro & Création de Livrables pour Kōdo POS v2.0
# Temps d'exécution ultra-rapide (~25 secondes sur APFS)
# =================================================================

APP_NAME="Kodo_POS"
DMG_NAME="Installation_Kodo_POS.dmg"
WIN_ZIP="Kodo_POS_v2.0.0_Windows_Pack.zip"
SRC_DIR="$(pwd)"
APFS_BUILD="/tmp/kodo_build"

echo "----------------------------------------------------"
echo "🚀 Démarrage du Build Final Kōdo POS v2.0..."
echo "----------------------------------------------------"

# 0. VÉRIFICATION INTÉGRITÉ ARBRE & VERSIONS (Correctif O - Audit)
# Un DMG se construit depuis l'arbre FINAL. Toute modification non commitée signifie que
# le binaire livré ne correspondra à aucun commit : make-patch --base <version> calcule
# ses empreintes contre l'arbre, et un patch backend ultérieur serait rejeté par le client.
if [ -n "$(git -C "$SRC_DIR" status --porcelain --untracked-files=no)" ]; then
  echo "❌ Arbre de travail non propre. Committer avant de construire le DMG :"
  git -C "$SRC_DIR" status --short --untracked-files=no
  exit 1
fi

# La version du socle, le tag attendu et le nom des archives doivent coïncider.
KODO_VERSION=$(grep -E '^BASE_VERSION' "$SRC_DIR/kodo_base.py" | sed -E 's/.*"([^"]+)".*/\1/')
UPDATER_VERSION=$(grep -E '^CURRENT_VERSION' "$SRC_DIR/kodo_core/services/updater.py" | sed -E 's/.*"([^"]+)".*/\1/')
if [ "$KODO_VERSION" != "$UPDATER_VERSION" ]; then
  echo "❌ Désalignement de version : kodo_base=$KODO_VERSION updater=$UPDATER_VERSION"
  exit 1
fi
if git -C "$SRC_DIR" rev-parse -q --verify "refs/tags/v$KODO_VERSION" >/dev/null 2>&1; then
  if [ "$(git -C "$SRC_DIR" rev-parse "v$KODO_VERSION")" != "$(git -C "$SRC_DIR" rev-parse HEAD)" ]; then
    echo "❌ Le tag v$KODO_VERSION existe déjà et désigne un AUTRE commit :"
    git -C "$SRC_DIR" show -s --format='   %H %ad %s' "v$KODO_VERSION"
    exit 1
  fi
fi

# 0.1 GARDE-FOUS (audit M4) : aucun livrable si les tests Python ou la vérification TypeScript échouent
FRONT_DIR="$(ls -d /Users/kiamarulmont/Desktop/*k*do-pos-3* 2>/dev/null | head -1)"
echo "🧪 Tests Python (pytest)..."
(cd "$SRC_DIR" && python3.12 -m pytest -q tests) || { echo "❌ Tests Python en échec (ou pytest absent : python3.12 -m pip install pytest). Build annulé."; exit 1; }
echo "🔎 Vérification TypeScript du frontend (npm run lint)..."
[ -n "$FRONT_DIR" ] || { echo "❌ Frontend kōdo-pos-3 introuvable sur le Bureau. Build annulé."; exit 1; }
(cd "$FRONT_DIR" && npm run lint) || { echo "❌ Erreurs TypeScript dans le frontend. Build annulé."; exit 1; }

# 1. RÉINITIALISATION USINE DE LA BDD (Règle Vierge)
echo "🧹 Réinitialisation usine de la base de données..."
rm -f "$SRC_DIR/kodo_pos.db" "$SRC_DIR/kodo_pos.db-shm" "$SRC_DIR/kodo_pos.db-wal"

PYTHONPATH="$SRC_DIR" python3.12 -c "
import sys, os, sqlite3
sys.path.insert(0, '$SRC_DIR')
import database_manager
database_manager.DB_NAME='kodo_pos.db'
database_manager.initialiser_db()

conn = sqlite3.connect(os.path.join('$SRC_DIR', 'kodo_pos.db'))
c = conn.cursor()
user_tables = ['produits', 'clients', 'ventes', 'ligne_ventes', 'stocks', 'sessions_caisse', 'depenses_caisse', 'ledger_caisse', 'rapports_z', 'clotures_z', 'tickets_en_attente']
existing = [row[0] for row in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
total_user_rows = sum(c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in user_tables if t in existing)
conn.close()
if total_user_rows > 0:
    print(f'❌ ERREUR: La BDD usine kodo_pos.db contient {total_user_rows} donnees utilisateur !')
    sys.exit(1)
else:
    print(f'✅ BDD usine kodo_pos.db verifiee : 0 donnee utilisateur.')
"

if [ $? -ne 0 ]; then
    echo "❌ Erreur de réinitialisation BDD usine."
    exit 1
fi

# 1.5 COMPILATION & COPIE DU FRONTEND REACT (VITE)
echo "⚡ Copie du frontend React Vite..."
python3 -c "import shutil, glob; src = glob.glob('/Users/kiamarulmont/Desktop/*k*do-pos-3*/dist')[0]; shutil.rmtree('$SRC_DIR/dist', ignore_errors=True); shutil.copytree(src, '$SRC_DIR/dist')" 2>/dev/null || true

# 1.6 VERSION DE BASE DU DMG (référence des patchs backend signés : kodo_base.BASE_VERSION)
echo "🔏 Alignement de la version de base des correctifs backend..."
(cd "$SRC_DIR" && python3 scripts/release/kodo_release.py stamp-base) || { echo "❌ Impossible d'aligner kodo_base.BASE_VERSION."; exit 1; }

# 2. PRÉPARATION DU DOSSIER DE BUILD APFS
echo "📦 Copie miroir vers APFS pour la compilation PyInstaller..."
rm -rf "$APFS_BUILD"
mkdir -p "$APFS_BUILD"
rsync -a --exclude='.git' --exclude='Installation_Pack' --exclude='public' --exclude='releases' --exclude='*.dmg' --exclude='*.zip' --exclude='Backups_*' --exclude='Exports_*' --exclude='.npm-cache' --exclude='__pycache__' "$SRC_DIR/" "$APFS_BUILD/"

# 3. COMPILATION PYINSTALLER SUR APFS
echo "📦 Compilation PyInstaller..."
DIST_DIR="/tmp/kodo_dist_$$"
WORK_DIR="/tmp/kodo_work_$$"
rm -rf "$DIST_DIR" "$WORK_DIR"

cd "$APFS_BUILD"
export PYINSTALLER_CONFIG_DIR="/tmp/pyi_cache_$$"
python3.12 -m PyInstaller --clean --noconfirm --distpath "$DIST_DIR" --workpath "$WORK_DIR" Kodo_POS.spec

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
rm -rf "/Applications/$APP_NAME.app" 2>/dev/null || true
cp -R "$PACK_DIR/$APP_NAME.app" /Applications/ 2>/dev/null || true
codesign --force --deep --sign - "/Applications/$APP_NAME.app" 2>/dev/null || true
xattr -cr "/Applications/$APP_NAME.app" 2>/dev/null || true

# 5. GÉNÉRATION DE INSTALLATION_KODO_POS_MACOS.ZIP VIA DITTO
echo "📦 Génération de Installation_Kodo_POS_macOS.zip..."
rm -f "$SRC_DIR/Installation_Kodo_POS_macOS.zip" "$SRC_DIR/public/Installation_Kodo_POS_macOS.zip"
ditto -c -k --sequesterRsrc "$PACK_DIR" "$SRC_DIR/Installation_Kodo_POS_macOS.zip"
cp "$SRC_DIR/Installation_Kodo_POS_macOS.zip" "$SRC_DIR/public/Installation_Kodo_POS_macOS.zip" 2>/dev/null || true
cp "$SRC_DIR/Installation_Kodo_POS_macOS.zip" ~/Desktop/Installation_Kodo_POS_macOS.zip 2>/dev/null || true

# 6. GÉNÉRATION DU DMG MACOS
echo "💿 Création de l'image disque DMG macOS..."
rm -rf /tmp/dmg_build && mkdir -p /tmp/dmg_build
cp -R "$PACK_DIR/$APP_NAME.app" /tmp/dmg_build/
cp "$PACK_DIR/IMPORTANT_LISEZ_MOI.txt" /tmp/dmg_build/ 2>/dev/null || true
rm -f "$SRC_DIR/$DMG_NAME" "$SRC_DIR/public/$DMG_NAME"
hdiutil create -volname "Kodo POS" -srcfolder /tmp/dmg_build -ov -format UDZO "$SRC_DIR/$DMG_NAME"
cp "$SRC_DIR/$DMG_NAME" "$SRC_DIR/public/$DMG_NAME" 2>/dev/null || true
cp "$SRC_DIR/$DMG_NAME" ~/Desktop/"$DMG_NAME" 2>/dev/null || true
# Le volume de développement est exFAT : les métadonnées macOS deviennent de vrais fichiers
# ._* sur le disque. « zip -X » retire les attributs étendus mais PAS ces fichiers, qui
# partiraient alors dans le paquet signé soumis à la vérification de chemins côté client.
(cd "$SRC_DIR/dist" && rm -f "$SRC_DIR/public/dist_v${KODO_VERSION}.zip" && zip -r -X "$SRC_DIR/public/dist_v${KODO_VERSION}.zip" . \
    -x '._*' -x '*/._*' -x '__MACOSX/*' -x '.DS_Store' -x '*/.DS_Store') 2>/dev/null || true

# L'archive OTA ne doit contenir que l'IHM courante : un dist/ non purgé y empile les
# anciens bundles, double la charge sur la connexion de la boutique et fait signer des
# fichiers qui n'ont rien à y faire.
NB=$(unzip -l "$SRC_DIR/public/dist_v${KODO_VERSION}.zip" | grep -cE '\.(js|css|html)$')
[ "$NB" -eq 3 ] || { echo "❌ Archive OTA : $NB fichiers au lieu de 3. dist/ n'était pas purgé."; exit 1; }
python3 "$SRC_DIR/scripts/release/kodo_release.py" sign-dist "$SRC_DIR/public/dist_v${KODO_VERSION}.zip" --version "$KODO_VERSION"
python3 "$SRC_DIR/scripts/release/kodo_release.py" verify "$SRC_DIR/public/dist_v${KODO_VERSION}.zip" --kind dist --version "$KODO_VERSION" \
  || { echo "❌ Signature invalide : les clients refuseraient cette archive."; exit 1; }

rm -rf /tmp/dmg_build "$DIST_DIR" "$WORK_DIR" "$BUILD_DIR" "$APFS_BUILD"

# 7. GÉNÉRATION DU PACK WINDOWS (Kodo_POS_v1.0.45_Windows_Pack.zip)
echo "🪟 Préparation du pack de build Windows ($WIN_ZIP)..."
rm -f "$SRC_DIR/$WIN_ZIP" ~/Desktop/"$WIN_ZIP"
cd "$SRC_DIR" && zip -r -1 "$SRC_DIR/$WIN_ZIP" launch_app.py Lancer_Kodo.bat server_pos.py database_manager.py export_manager.py audit_trail.py backup_manager.py ticket_printer.py pdf_generator.py license_manager.py shopify_sync.py firebase_sync.py Kodo_POS_Windows.spec build_windows.bat logo.png logo_ticket.png instagram_block.png dist kodo_pos.db plan_permissions.json kodo_core core services views 2>/dev/null || true
cp "$SRC_DIR/$WIN_ZIP" ~/Desktop/"$WIN_ZIP" 2>/dev/null || true

echo "----------------------------------------------------"
echo "✨ LIVRAISON TERMINÉE AVEC SUCCÈS !"
echo "📍 macOS ZIP (ditto) : ~/Desktop/Installation_Kodo_POS_macOS.zip"
echo "📍 macOS DMG : ~/Desktop/$DMG_NAME"
echo "📍 Windows Build Pack : ~/Desktop/$WIN_ZIP"
echo "----------------------------------------------------"
