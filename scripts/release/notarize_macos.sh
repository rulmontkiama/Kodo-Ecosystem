#!/bin/bash
# ==============================================================================
# Kōdo POS — Signature Developer ID & Notarisation Apple (macOS)
#
# Prérequis (une seule fois sur le Mac de build) :
#   1. Certificat "Developer ID Application: <Nom> (8Q2G5M5C2B)" dans Trousseau
#   2. Profil notarytool enregistré dans le Trousseau :
#      xcrun notarytool store-credentials "kodo-notary" \
#        --apple-id "<votre@apple.id>" \
#        --team-id "8Q2G5M5C2B" \
#        --password "<mot-de-passe-app-specifique>"
#
# Utilisation (auto-détection du certificat 8Q2G5M5C2B dans le Trousseau) :
#   ./scripts/release/notarize_macos.sh [chemin/Kodo_POS.app] [chemin/Installation_Kodo_POS.dmg]
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENTITLEMENTS="${APPLE_ENTITLEMENTS:-$SCRIPT_DIR/entitlements.plist}"
TEAM_ID="${APPLE_TEAM_ID:-8Q2G5M5C2B}"
IDENTITY="${APPLE_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:-kodo-notary}"

APP_PATH="${1:-$ROOT_DIR/Installation_Pack/Kodo_POS.app}"
DMG_PATH="${2:-$ROOT_DIR/Installation_Kodo_POS.dmg}"

if [ -z "$IDENTITY" ] && command -v security >/dev/null 2>&1; then
    IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
        | grep "Developer ID Application" \
        | grep "(${TEAM_ID})" \
        | head -1 \
        | sed -E 's/.*"([^"]+)".*/\1/' || true)
fi

if [ -z "$IDENTITY" ]; then
    echo "❌ Aucun certificat 'Developer ID Application' trouvé pour l'équipe $TEAM_ID dans le Trousseau."
    echo "   Installez votre certificat depuis developer.apple.com ou définissez APPLE_SIGN_IDENTITY."
    security find-identity -v -p codesigning 2>/dev/null || true
    exit 1
fi

if [ ! -f "$ENTITLEMENTS" ]; then
    echo "❌ Fichier entitlements introuvable : $ENTITLEMENTS"
    exit 1
fi

sign_app() {
    local app="$1"
    [ -d "$app" ] || return 0
    echo "🔏 Signature Developer ID des binaires internes dans $app..."
    find "$app/Contents" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -111 \) ! -path "*.framework/*" -print0 \
        | while IFS= read -r -d '' bin; do
            codesign --force --options runtime --timestamp \
                --entitlements "$ENTITLEMENTS" \
                --sign "$IDENTITY" "$bin"
        done
    echo "🔏 Signature Developer ID du bundle $app..."
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" \
        --sign "$IDENTITY" "$app"
    codesign --verify --deep --strict --verbose=2 "$app"
    echo "✅ Signature .app validée."
}

sign_dmg() {
    local dmg="$1"
    [ -f "$dmg" ] || return 0
    echo "🔏 Signature Developer ID du DMG $dmg..."
    codesign --force --timestamp --sign "$IDENTITY" "$dmg"
    if [ -n "$NOTARY_PROFILE" ]; then
        echo "🍏 Soumission notarisation Apple (notarytool)..."
        xcrun notarytool submit "$dmg" --keychain-profile "$NOTARY_PROFILE" --wait
        echo "📎 Agrafage du ticket de notarisation (staple)..."
        xcrun stapler staple "$dmg"
        xcrun stapler validate "$dmg"
        spctl --assess --type open --context context:primary-signature -v "$dmg" || true
        echo "✅ DMG signé, notarisé et validé Gatekeeper."
    else
        echo "⚠️ APPLE_NOTARY_PROFILE non défini : DMG signé sans notarisation Apple."
    fi
}

[ -d "$APP_PATH" ] && sign_app "$APP_PATH"
[ -f "$DMG_PATH" ] && sign_dmg "$DMG_PATH"
