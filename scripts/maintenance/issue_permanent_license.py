#!/usr/bin/env python3
"""
Émet manuellement une licence longue durée (ex. 30 ans) liée à un HWID.

Par défaut le script est en DRY-RUN : il affiche la clé et le document Firestore
qui seraient créés, sans aucun accès réseau. Ajouter --apply pour écrire dans
Firestore (collection `licenses`, avec create() : refuse d'écraser une clé existante).

Le format de clé et le checksum sont identiques à src/lib/license_generator.ts
(KODO-<PLAN>-<DURÉE>-<TOKEN>-<CHECKSUM>), donc verifyLicenseKey() l'accepte.
Le token fait 64 bits au lieu de 16 pour éviter les collisions et l'énumération.

Environnement :
  LICENSE_HMAC_SECRET          même valeur que la variable Vercel (obligatoire)
  KODO_FIREBASE_CREDENTIALS    chemin du JSON Admin SDK (--apply uniquement)

Exemple :
  LICENSE_HMAC_SECRET=... python3 scripts/maintenance/issue_permanent_license.py \\
      --plan PRO --hwid 7F01FB567EBAE9A2 --email client@example.com
"""

import argparse
import datetime
import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import sys

PLANS = ("STARTER", "PRO", "MAX")
HWID_RE = re.compile(r"^[0-9A-F]{16}$")


def compute_checksum(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:4].upper()


def generate_key(plan: str, duration: str, secret: str) -> str:
    token = secrets.token_hex(8).upper()
    return f"KODO-{plan}-{duration}-{token}-{compute_checksum(f'{plan}-{duration}-{token}', secret)}"


def verify_key(key: str, secret: str) -> bool:
    parts = key.split("-")
    if len(parts) != 5 or parts[0] != "KODO":
        return False
    _, plan, duration, token, checksum = parts
    return compute_checksum(f"{plan}-{duration}-{token}", secret) == checksum


def add_years(start: datetime.datetime, years: int) -> datetime.datetime:
    try:
        return start.replace(year=start.year + years)
    except ValueError:  # 29 février
        return start.replace(year=start.year + years, day=28)


def build_document(key, plan, duration, hwid, email, years, now):
    return {
        "license_key": key,
        "plan": plan,
        "duration": duration,
        "status": "active",
        "created_at": now.isoformat(),
        "expires_at": add_years(now, years).isoformat(),
        "hardware_id": hwid,
        "customer_email": email,
        "stripe_subscription_id": None,
        "issued_manually": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True, choices=PLANS)
    ap.add_argument("--hwid", required=True, help="Empreinte machine (16 caractères hexadécimaux)")
    ap.add_argument("--email", required=True, help="Email du client")
    ap.add_argument("--years", type=int, default=30)
    ap.add_argument("--apply", action="store_true", help="Écrit réellement dans Firestore")
    args = ap.parse_args()

    hwid = args.hwid.strip().upper()
    if not HWID_RE.match(hwid):
        print("HWID invalide : 16 caractères hexadécimaux attendus.", file=sys.stderr)
        return 2

    secret = os.environ.get("LICENSE_HMAC_SECRET", "")
    if not secret:
        print("LICENSE_HMAC_SECRET manquant.", file=sys.stderr)
        return 2

    duration = f"{args.years}Y"
    now = datetime.datetime.now(datetime.timezone.utc)
    key = generate_key(args.plan, duration, secret)
    assert verify_key(key, secret)
    doc = build_document(key, args.plan, duration, hwid, args.email, args.years, now)

    print(("APPLY" if args.apply else "DRY-RUN") + " — document licenses/" + key)
    print(json.dumps(doc, indent=2, ensure_ascii=False))

    if not args.apply:
        print("\nRien n'a été écrit. Relancer avec --apply pour créer le document.")
        return 0

    import firebase_admin
    from firebase_admin import credentials, firestore

    cred_path = os.environ.get("KODO_FIREBASE_CREDENTIALS") or next(iter(glob.glob("kodo-pos-firebase-adminsdk-*.json")), "")
    if not cred_path or not os.path.exists(cred_path):
        print("Clé Admin SDK introuvable (KODO_FIREBASE_CREDENTIALS).", file=sys.stderr)
        return 2
    firebase_admin.initialize_app(credentials.Certificate(cred_path))
    firestore.client().collection("licenses").document(key).create(doc)  # échoue si la clé existe déjà
    print(f"\nLicence créée. Clé à transmettre au client : {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
