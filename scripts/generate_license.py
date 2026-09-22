#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Générateur de Clés de Licence Hors-Ligne Signées (Ed25519).

Utilisation (sur le poste de l'éditeur uniquement) :
    python3 scripts/generate_license.py --hwid C02XG2JGJGH7 --plan PRO --days 365
    python3 scripts/generate_license.py --hwid C02XG2JGJGH7 --permanent

Ce script utilise la clé privée Ed25519 du développeur (~/.kodo_signing/update_ed25519.key
ou ~/.kodo_signing/license_ed25519.key) pour signer un payload inviolable contenant le HWID,
le plan et la date d'expiration.
"""

import argparse
import datetime
import os
import stat
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import kodo_ed25519
from kodo_core.services.license import (
    generate_signed_license,
    verify_signed_license,
    LICENSE_TRUSTED_PUBLIC_KEYS,
)

DEFAULT_KEY_PATHS = [
    os.path.expanduser("~/.kodo_signing/license_ed25519.key"),
    os.path.expanduser("~/.kodo_signing/update_ed25519.key"),
]


def load_secret_key(custom_path: str = None) -> bytes:
    """Charge la clé privée Ed25519 depuis le stockage sécurisé du développeur."""
    paths = [custom_path] if custom_path else DEFAULT_KEY_PATHS
    chosen_path = None
    for p in paths:
        if p and os.path.exists(p):
            chosen_path = p
            break

    if not chosen_path:
        sys.exit(
            f"❌ Clé privée Ed25519 introuvable.\n"
            f"Emplacements cherchés : {', '.join(paths)}\n"
            f"Créez-en une avec : python3 scripts/generate_license.py --genkey"
        )

    mode = os.stat(chosen_path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"⚠️ Avertissement : permissions trop ouvertes sur {chosen_path}. Application de chmod 600...")
        os.chmod(chosen_path, 0o600)

    with open(chosen_path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    secret = bytes.fromhex(content)
    if len(secret) != 32:
        sys.exit(f"❌ La clé privée dans {chosen_path} est invalide (32 octets hex attendus).")

    return secret, chosen_path


def cmd_genkey(target_path: str = None):
    """Génère une nouvelle paire de clés Ed25519 pour la signature des licences."""
    path = target_path or DEFAULT_KEY_PATHS[0]
    if os.path.exists(path):
        sys.exit(f"❌ Une clé existe déjà : {path}\nOpération annulée pour éviter d'écraser la clé existante.")

    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    secret = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex() + "\n")

    pub_hex = kodo_ed25519.public_key_from_secret(secret).hex()
    print(f"✅ Nouvelle clé privée créée : {path} (permissions 600)")
    print(f"🔑 Clé publique correspondante (à ajouter dans LICENSE_TRUSTED_PUBLIC_KEYS) :")
    print(f"   \"{pub_hex}\"")


def main():
    parser = argparse.ArgumentParser(description="Générateur de clés de licence Ed25519 pour Kōdo POS")
    parser.add_argument("--hwid", type=str, help="Empreinte matérielle (HWID) de l'appareil client (16 car.)")
    parser.add_argument("--plan", type=str, default="PRO", help="Plan de licence (ex: PRO, STANDARD, MAX). Défaut: PRO")
    parser.add_argument("--days", type=int, default=365, help="Durée de validité en jours (défaut: 365)")
    parser.add_argument("--permanent", action="store_true", help="Générer une licence permanente (sans expiration)")
    parser.add_argument("--key-path", type=str, help="Chemin vers la clé privée Ed25519")
    parser.add_argument("--genkey", action="store_true", help="Générer une nouvelle clé privée Ed25519")

    args = parser.parse_args()

    if args.genkey:
        cmd_genkey(args.key_path)
        return

    if not args.hwid:
        parser.error("L'argument --hwid est obligatoire pour générer une licence.")

    clean_hwid = args.hwid.strip().upper()
    secret_key, key_file = load_secret_key(args.key_path)
    pub_hex = kodo_ed25519.public_key_from_secret(secret_key).hex()

    if pub_hex not in LICENSE_TRUSTED_PUBLIC_KEYS:
        print(f"⚠️ ATTENTION : La clé publique ({pub_hex}) ne figure pas encore dans LICENSE_TRUSTED_PUBLIC_KEYS !")
        print(f"   L'application refusera cette licence tant que sa clé publique ne sera pas intégrée.")

    if args.permanent:
        expiry_str = "PERMANENT"
    else:
        expiry_date = datetime.date.today() + datetime.timedelta(days=args.days)
        expiry_str = expiry_date.isoformat()

    license_key = generate_signed_license(
        secret_key=secret_key,
        fingerprint=clean_hwid,
        plan=args.plan,
        expiry_date=expiry_str,
    )

    # Auto-vérification immédiate
    valid, details, err = verify_signed_license(license_key, clean_hwid)
    if not valid:
        print(f"❌ Échec de vérification de la clé générée : {err}")
        sys.exit(1)

    print("\n" + "=" * 64)
    print("🎫 CLÉ DE LICENCE KŌDO POS GÉNÉRÉE AVEC SUCCÈS")
    print("=" * 64)
    print(f"Appareil (HWID) : {clean_hwid}")
    print(f"Plan            : {args.plan.upper()}")
    print(f"Expiration      : {expiry_str}")
    print(f"Signée avec     : {key_file} (pub: {pub_hex[:16]}...)")
    print("-" * 64)
    print("Clé d'activation (à transmettre à la commerçante) :")
    print(f"\n{license_key}\n")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    main()
