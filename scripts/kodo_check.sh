#!/bin/bash
# ============================================================================
#  Kōdo POS — Diagnostic d'installation (LECTURE SEULE)
#
#  N'écrit RIEN dans les données du commerçant. La base est copiée dans un
#  dossier temporaire et tous les contrôles sont faits sur la copie : la base
#  d'origine n'est jamais ouverte par SQLite, seulement lue octet par octet.
# ============================================================================

APP="/Applications/Kodo_POS.app"
DB="$HOME/Documents/Kodo_POS/db/kodo_pos.db"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PROBLEMES=0
AVERTS=0

ok()    { printf "  \033[32m✅\033[0m %s\n" "$1"; }
warn()  { printf "  \033[33m⚠️ \033[0m %s\n" "$1"; AVERTS=$((AVERTS+1)); }
bad()   { printf "  \033[31m❌\033[0m %s\n" "$1"; PROBLEMES=$((PROBLEMES+1)); }
titre() { printf "\n\033[1m%s\033[0m\n" "$1"; }

printf "\n\033[1m═══ DIAGNOSTIC KŌDO POS ═══\033[0m\n"
printf "  %s · lecture seule, aucune donnée modifiée\n" "$(date '+%d/%m/%Y %H:%M')"

# --- 0. L'application doit être fermée -------------------------------------
titre "0. État de l'application"
if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  warn "Kōdo POS est EN COURS D'EXÉCUTION. Fermez-le et relancez ce diagnostic"
  warn "pour un contrôle fiable de la base."
else
  ok "Application fermée — contrôle fiable possible."
fi

# --- 1. Version installée ---------------------------------------------------
titre "1. Application installée"
if [ ! -d "$APP" ]; then
  bad "Aucune application dans /Applications/Kodo_POS.app"
  VER="?"
else
  VER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" 2>/dev/null)
  [ -n "$VER" ] && ok "Version installée : $VER" || bad "Version illisible dans Info.plist"
  BUNDLE=$(ls "$APP/Contents/Resources/dist/assets/"*.js 2>/dev/null | head -1)
  if [ -n "$BUNDLE" ]; then
    if grep -qF "$VER" "$BUNDLE" 2>/dev/null; then
      ok "Interface embarquée cohérente avec la version ($(basename "$BUNDLE"))"
    else
      bad "L'interface embarquée ne correspond PAS à la version $VER"
    fi
    if grep -qF "TVA: BE 0123.456.789" "$BUNDLE" 2>/dev/null; then
      bad "L'interface contient encore le numéro de TVA de démonstration"
    else
      ok "Aucun numéro de TVA de démonstration dans l'interface"
    fi
  else
    bad "Interface (dist/assets) absente de l'application"
  fi
fi

# --- 2. Base de données -----------------------------------------------------
titre "2. Base de données du commerce"
if [ ! -f "$DB" ]; then
  bad "Base introuvable : $DB"
else
  TAILLE=$(( $(stat -f %z "$DB") / 1024 ))
  ok "Base présente ($TAILLE Ko) — $DB"
  # Copie octet à octet, WAL et SHM compris : l'originale n'est jamais ouverte.
  cp "$DB" "$TMP/c.db" 2>/dev/null
  [ -f "$DB-wal" ] && cp "$DB-wal" "$TMP/c.db-wal" 2>/dev/null
  [ -f "$DB-shm" ] && cp "$DB-shm" "$TMP/c.db-shm" 2>/dev/null

  INTEG=$(sqlite3 "$TMP/c.db" "PRAGMA integrity_check;" 2>&1 | head -1)
  [ "$INTEG" = "ok" ] && ok "Intégrité SQLite : ok" || bad "Intégrité SQLite : $INTEG"

  Q() { sqlite3 "$TMP/c.db" "$1" 2>/dev/null; }
  P=$(Q "SELECT COUNT(*) FROM Produits;");        P=${P:-0}
  S=$(Q "SELECT COUNT(*) FROM Stocks;");          S=${S:-0}
  U=$(Q "SELECT COALESCE(SUM(quantite_actuelle),0) FROM Stocks;"); U=${U:-0}
  T=$(Q "SELECT COUNT(*) FROM Tickets;");         T=${T:-0}
  C=$(Q "SELECT COUNT(*) FROM Clients;");         C=${C:-0}
  L=$(Q "SELECT COUNT(*) FROM Ledger_Caisse;");   L=${L:-0}
  printf "     produits=%s  lignes de stock=%s  unités=%s\n" "$P" "$S" "$U"
  printf "     tickets=%s  clients=%s  mouvements de caisse=%s\n" "$T" "$C" "$L"
  [ "$P" -gt 0 ] && ok "Catalogue présent" || bad "CATALOGUE VIDE — ne pas continuer, appeler le support"
  NEG=$(Q "SELECT COUNT(*) FROM Stocks WHERE quantite_actuelle < 0;"); NEG=${NEG:-0}
  [ "$NEG" = "0" ] && ok "Aucun stock négatif" || bad "$NEG ligne(s) de stock NÉGATIVE"

  # --- 3. Déclinaisons ------------------------------------------------------
  titre "3. Déclinaisons (tailles)"
  # Les déclinaisons sont une ligne de Stocks par taille, pas une colonne de Produits.
  D=$(Q "SELECT COUNT(*) FROM (SELECT id_produit FROM Stocks WHERE COALESCE(taille,'') <> '' GROUP BY id_produit HAVING COUNT(*) > 1);")
  D=${D:-0}
  if [ "$D" -gt 0 ]; then
    ok "$D article(s) à déclinaisons"
    Q "SELECT '     ' || p.nom || '  ->  ' || GROUP_CONCAT(s.taille || ':' || s.quantite_actuelle, ' | ')
       FROM Stocks s JOIN Produits p ON p.id = s.id_produit
       WHERE COALESCE(s.taille,'') <> ''
       GROUP BY p.id HAVING COUNT(*) > 1 LIMIT 3;"
  else
    warn "Aucun article à déclinaisons détecté (normal si le commerce n'en utilise pas)"
  fi

  # --- 4. Réglages boutique -------------------------------------------------
  titre "4. Identité fiscale de la boutique"
  G() { sqlite3 "$TMP/c.db" "SELECT COALESCE(valeur,'') FROM Parametres WHERE cle='$1';" 2>/dev/null; }
  NOM=$(G shop_name); TVA=$(G shop_tva); BCE=$(G shop_bce); IBAN=$(G shop_iban)
  [ -n "$NOM" ]  && ok "Nom : $NOM" || warn "Nom de boutique non renseigné"
  if [ -n "$TVA" ]; then
    case "$TVA" in
      *0123.456.789*) bad "Le n° de TVA enregistré est le numéro de DÉMONSTRATION : $TVA" ;;
      *) ok "N° TVA : $TVA" ;;
    esac
  else
    warn "N° TVA NON RENSEIGNÉ — les tickets afficheront l'avertissement. À compléter dans Paramètres > Boutique"
  fi
  [ -n "$BCE" ] && ok "N° BCE : $BCE" || warn "N° BCE non renseigné"
  case "$IBAN" in
    "")            warn "IBAN non renseigné (nécessaire uniquement pour les virements Live Shopping)" ;;
    *BE68\ 0000*)  bad "L'IBAN enregistré est l'IBAN de DÉMONSTRATION : $IBAN" ;;
    *)             ok "IBAN : $IBAN" ;;
  esac
fi

# --- 5. Cache d'interface (peut masquer celle du DMG) -----------------------
titre "5. Cache d'interface"
CACHE_JSON="$HOME/Library/Caches/KodoPOS/version.json"
CACHE_DIST="$HOME/Library/Caches/KodoPOS/dist"
if [ ! -d "$CACHE_DIST" ]; then
  ok "Aucun cache — l'interface du DMG est utilisée."
else
  CV=$(sed -n 's/.*"version"[^"]*"\([^"]*\)".*/\1/p' "$CACHE_JSON" 2>/dev/null | head -1)
  if [ -z "$CV" ]; then
    ok "Cache présent sans version lisible — ignoré, l'interface du DMG est utilisée."
  elif [ "$(printf '%s\n%s\n' "$VER" "$CV" | sort -V | tail -1)" = "$CV" ] && [ "$CV" != "$VER" ]; then
    warn "Le cache ($CV) est PLUS RÉCENT que l'app ($VER) : c'est lui qui s'affichera."
  else
    ok "Cache ($CV) antérieur ou égal à l'app ($VER) — l'interface du DMG l'emporte."
  fi
fi

# --- 6. Correctifs backend installés ---------------------------------------
titre "6. Correctifs backend"
PATCHES="$HOME/Library/Application Support/Kodo_POS/patches"
if [ -d "$PATCHES" ] && [ -n "$(ls -A "$PATCHES" 2>/dev/null)" ]; then
  N=$(find "$PATCHES" -name 'manifest*.json' 2>/dev/null | wc -l | tr -d ' ')
  warn "$N correctif(s) présent(s) — ils seront refusés s'ils ne visent pas la $VER."
  find "$PATCHES" -name 'manifest*.json' 2>/dev/null | while read -r m; do
    PV=$(sed -n 's/.*"version"[^"]*"\([^"]*\)".*/\1/p' "$m" | head -1)
    BN=$(sed -n 's/.*"base_min"[^"]*"\([^"]*\)".*/\1/p' "$m" | head -1)
    BX=$(sed -n 's/.*"base_max"[^"]*"\([^"]*\)".*/\1/p' "$m" | head -1)
    printf "     correctif %s (socle %s → %s)\n" "$PV" "$BN" "$BX"
  done
else
  ok "Aucun correctif backend installé."
fi

# --- 7. Sauvegardes ---------------------------------------------------------
titre "7. Sauvegardes"
BK="$HOME/Documents/Kodo_POS/backups/sanctuary_pre_v2"
if [ -d "$BK" ]; then
  N=$(find "$BK" -name '*.db' 2>/dev/null | wc -l | tr -d ' ')
  DER=$(find "$BK" -name '*.db' -exec stat -f '%Sm %N' -t '%d/%m %H:%M' {} \; 2>/dev/null | sort | tail -1)
  ok "$N sauvegarde(s) Sanctuaire · dernière : ${DER%% /*}"
else
  warn "Aucune sauvegarde Sanctuaire (normale avant le premier lancement de la v2)"
fi

# --- Verdict ----------------------------------------------------------------
printf "\n\033[1m═══ VERDICT ═══\033[0m\n"
if [ "$PROBLEMES" -gt 0 ]; then
  printf "  \033[31m❌ %s PROBLÈME(S) BLOQUANT(S)\033[0m — ne pas déployer sur la 2e boutique.\n" "$PROBLEMES"
  [ "$AVERTS" -gt 0 ] && printf "     (et %s avertissement(s))\n" "$AVERTS"
  exit 1
elif [ "$AVERTS" -gt 0 ]; then
  printf "  \033[33m⚠️  Installation saine, %s point(s) à compléter\033[0m (voir ci-dessus).\n" "$AVERTS"
  exit 0
else
  printf "  \033[32m✅ TOUT EST BON — installation saine et complète.\033[0m\n"
  exit 0
fi
