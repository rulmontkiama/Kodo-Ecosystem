#!/bin/bash
# ==============================================================================
# SCRIPT DE DÉPLOIEMENT ET MISE À JOUR À DISTANCE KŌDO POS & KŌDO WEB
# ==============================================================================

set -e

POS_DIR="/Volumes/Extreme SSD/KIAMA/Kōdo POS"
WEB_DIR="/Users/kiamarulmont/Desktop/kodo-solutions-web"

echo "======================================================================"
echo "      🚀 KŌDO POS — PIPELINE DE DÉPLOIEMENT À DISTANCE (VERCEL)      "
echo "======================================================================"

cd "$POS_DIR"

# 1. Demander le numéro de version et le changelog
read -p "📌 Entrez le nouveau numéro de version (ex: 1.0.1) : " NEW_VERSION
if [ -z "$NEW_VERSION" ]; then
    echo "❌ Erreur: La version ne peut pas être vide."
    exit 1
fi

read -p "📝 Entrez les nouveautés / changelog de cette mise à jour : " CHANGELOG
if [ -z "$CHANGELOG" ]; then
    CHANGELOG="Version $NEW_VERSION : Améliorations générales des performances et de la stabilité."
fi

RELEASE_DATE=$(date +"%Y-%m-%d")

echo ""
echo "⚙️ [1/4] Mise à jour des numéros de version..."

# Mettre à jour services/update_checker.py
python3.12 -c "
with open('services/update_checker.py', 'r') as f:
    content = f.read()
import re
content = re.sub(r'CURRENT_VERSION = \"[^\"]+\"', f'CURRENT_VERSION = \"$NEW_VERSION\"', content)
with open('services/update_checker.py', 'w') as f:
    f.write(content)
"

# Mettre à jour l'API version sur kodo-solutions-web
WEB_VERSION_API="$WEB_DIR/src/app/api/version/route.ts"
cat <<EOF > "$WEB_VERSION_API"
import { NextResponse } from 'next/server';

export async function GET() {
  return NextResponse.json({
    latestVersion: "$NEW_VERSION",
    releaseDate: "$RELEASE_DATE",
    downloadUrl: "https://kōdo-solutions.com/Installation_Kodo_POS.dmg",
    mandatory: false,
    changelog: "$CHANGELOG",
  });
}
EOF
echo "✅ API version mise à jour vers v$NEW_VERSION"

echo ""
echo "🔨 [2/4] Compilation du logiciel Kōdo POS en version d'usine (DMG)..."
./build_final_pro.sh

echo ""
echo "📦 [3/4] Copie de l'installeur DMG vers le site Web Kōdo..."
if [ -f "Installation_Kodo_POS.dmg" ]; then
    cp "Installation_Kodo_POS.dmg" "$WEB_DIR/public/Installation_Kodo_POS.dmg"
    echo "✅ Fichier DMG copié dans $WEB_DIR/public/Installation_Kodo_POS.dmg"
else
    echo "⚠️ Avertissement: Installation_Kodo_POS.dmg introuvable."
fi

echo ""
echo "🌐 [4/4] Déploiement automatique sur Vercel via Git..."
cd "$WEB_DIR"
git add .
git commit -m "Release v$NEW_VERSION : $CHANGELOG" || true
git push origin main || git push

echo ""
echo "======================================================================"
echo "🎉 DÉPLOIEMENT À DISTANCE TERMINÉ AVEC SUCCÈS !"
echo "======================================================================"
echo "1. Le site https://kōdo-solutions.com sera mis à jour en ~30s sur Vercel."
echo "2. Les clients qui ouvrent Kōdo POS recevront automatiquement la notification :"
echo "   'Mise à jour v$NEW_VERSION disponible' avec bouton de téléchargement 1-clic."
echo "======================================================================"
