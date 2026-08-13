#!/bin/zsh
# Script de lancement du POS L'ADRESSE B
# Utilise Python 3.12 (avec Tk 9.0 fonctionnel) au lieu du Python 3.9 des CommandLineTools

cd "$(dirname "$0")"
echo "🚀 Démarrage de L'ADRESSE B POS..."
python3.12 main_app.py
