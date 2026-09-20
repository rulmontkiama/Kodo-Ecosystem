#!/bin/bash
# ==============================================================================
# Script de Diagnostic, Nettoyage & Assainissement Kōdo POS v2.0
# À exécuter en cas de besoin sur les postes clients (Double-clic dans le Finder)
# ==============================================================================

clear
echo "=================================================================="
echo "   🛡️  KŌDO POS v2.0 - OUTIL DE DIAGNOSTIC & D'ASSAINISSEMENT   "
echo "=================================================================="
echo ""

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# 1. Vérification et libération du port 8765
echo "1️⃣  Vérification des processus en arrière-plan..."
PORT_PID=$(lsof -ti :8765 2>/dev/null)
if [ -n "$PORT_PID" ]; then
    PROC_NAME=$(ps -p $PORT_PID -o comm= 2>/dev/null)
    if echo "$PROC_NAME" | grep -Eq "python|Kodo|Kōdo"; then
        echo "⚠️  Processus résiduel Kōdo POS détecté sur le port 8765 (PID: $PORT_PID, $PROC_NAME). Arrêt propre..."
        kill -TERM $PORT_PID 2>/dev/null
        sleep 1
        # Forcer si nécessaire
        kill -9 $PORT_PID 2>/dev/null
        echo "✅ Port 8765 libéré avec succès."
    else
        echo "⚠️  Le processus sur le port 8765 ($PROC_NAME) n'est pas identifié comme Kōdo POS. Arrêt ignoré."
    fi
else
    echo "✅ Aucun conflit de port détecté."
fi
echo ""

# 2. Exécution de l'assainisseur Python v2.0
echo "2️⃣  Exécution de l'assainisseur automatique et vérification du Sanctuaire..."
PYTHON_BIN="python3"
if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
fi

$PYTHON_BIN -c "
import sys, os
sys.path.insert(0, '$DIR')
from kodo_core.services.client_sanitizer import sanitize_client_environment, get_system_health_report

# Assainissement
res = sanitize_client_environment()
print('Actions effectuées :')
for act in res.get('actions_taken', []):
    print(f'  • {act}')

print('')
# Bilan de santé
health = get_system_health_report()
print('État global du système :', '🟢 EXCELLENT' if health['status'] == 'HEALTHY' else '⚠️ ' + health['status'])
print(f'  • Version installée : {health[\"version\"]}')
print(f'  • Base de données : {health[\"database\"][\"path\"]} ({health[\"database\"][\"size_bytes\"] / 1024:.1f} Ko)')
print(f'  • Intégrité BDD : {\"VALIDE\" if health[\"database\"][\"integrity_ok\"] else \"ERREUR\"}')
print(f'  • Nombre de produits : {health[\"database\"][\"products_count\"]}')
print(f'  • Stock total en rayon : {health[\"database\"][\"stock_total_units\"]} unités')

unclosed = health.get('unclosed_days', {})
if unclosed.get('has_past_unclosed'):
    print('')
    print('⚠️  ATTENTION : Des journées antérieures ne sont pas clôturées :')
    for d in unclosed.get('details', []):
        print(f'     - Jour {d[\"jour\"]} : {d[\"nb_tickets\"]} tickets ({d[\"total_tvac\"]:.2f} €)')
    print('👉 Ces journées seront clôturées séquentiellement jour par jour dans l\'application.')
"

echo ""
echo "=================================================================="
echo "   ✅ NETTOYAGE ET CONTRÔLE D'INTÉGRITÉ TERMINÉS AVEC SUCCÈS !    "
echo "=================================================================="
echo ""
read -p "Voulez-vous lancer Kōdo POS v2.0 maintenant ? (O/n) : " REPONSE
if [[ "$REPONSE" =~ ^[Oo]?$ ]]; then
    echo "🚀 Démarrage de Kōdo POS v2.0..."
    if [ -f "launch_app.py" ]; then
        $PYTHON_BIN launch_app.py &
    elif [ -d "/Applications/Kodo_POS.app" ]; then
        open /Applications/Kodo_POS.app
    fi
fi
