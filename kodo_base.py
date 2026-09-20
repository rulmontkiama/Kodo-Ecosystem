# -*- coding: utf-8 -*-
"""
Kōdo POS - Constantes de la racine de confiance des mises à jour.

ATTENTION : ce fichier est compilé dans le DMG et N'EST JAMAIS remplaçable par un patch distant
(voir DENIED_MODULES dans patch_loader.py). Changer la clé publique ou la version de base impose
donc de livrer un nouveau DMG.
"""

# Version du DMG. Synchronisée automatiquement avec kodo_core/services/updater.py::CURRENT_VERSION
# par `python3 scripts/release/kodo_release.py stamp-base` (appelé par build_final_pro.sh).
# Un patch backend ne s'applique que si cette version est dans sa plage [base_min, base_max].
BASE_VERSION = "1.0.73"

# Clés publiques Ed25519 (hex, 32 octets) autorisées à signer les mises à jour.
# La clé privée correspondante reste HORS LIGNE sur le poste du développeur
# (~/.kodo_signing/), jamais dans le dépôt, sur GitHub ou sur Vercel.
# Liste vide = aucune mise à jour signée n'est acceptée (échec sécurisé).
# Ajouter une seconde clé de secours ici permet de faire une rotation sans perdre la main.
TRUSTED_PUBLIC_KEYS = [
    "6d511f281e615a7635526033676c4b8d71d00d9f107028c6e2a1af7c3d4e31f7",
]
