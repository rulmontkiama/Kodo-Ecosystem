#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Outil de release : clé de signature, signature des mises à jour, fabrication de patchs backend.

À lancer depuis la racine du dépôt, sur VOTRE Mac (la clé privée ne doit jamais le quitter).

  genkey                          Crée la paire de clés Ed25519 (une seule fois) dans ~/.kodo_signing/
  sign-dist ZIP --version X.Y.Z   Signe public/dist_vX.Y.Z.zip  -> public/dist_vX.Y.Z.zip.sig
  make-patch --version X.Y.Z --base A.B.C [--since REV | FICHIER...]
                                  Fabrique + signe public/backend_vX.Y.Z.zip (correctif Python à distance)
  make-patch ... --empty          Patch vide : ramène tous les clients au code de leur DMG
  verify ZIP --kind dist|backend --version X.Y.Z
                                  Vérifie une signature avec la clé publique de kodo_base.py
  stamp-base                      Aligne kodo_base.BASE_VERSION sur updater.CURRENT_VERSION (avant un DMG)
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import kodo_ed25519  # noqa: E402
import patch_loader as pl  # noqa: E402

DEFAULT_KEY = os.path.expanduser("~/.kodo_signing/update_ed25519.key")
NOT_MODULES = ("tests", "test_", "scripts/", "build/", "dist/", "public/", "src/", "docs/", "node_modules/")


def key_path() -> str:
    return os.environ.get("KODO_SIGNING_KEY") or DEFAULT_KEY


def load_secret() -> bytes:
    path = key_path()
    if not os.path.exists(path):
        sys.exit(f"Clé privée introuvable : {path}\nCréez-la d'abord avec : python3 scripts/release/kodo_release.py genkey")
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f"Permissions trop ouvertes sur {path} (chmod 600 requis).")
    secret = bytes.fromhex(open(path).read().strip())
    if len(secret) != 32:
        sys.exit("Clé privée invalide (32 octets attendus).")
    return secret


def sign_payload(kind: str, version: str, payload: bytes) -> str:
    sig = kodo_ed25519.sign(load_secret(), pl.signed_message(kind, version, payload))
    return base64.b64encode(sig).decode()


# --------------------------------------------------------------------------- commandes

def cmd_genkey(args):
    path = key_path()
    if os.path.exists(path):
        sys.exit(f"Une clé existe déjà : {path}\nJe ne l'écrase jamais (la perdre = impossible de publier des mises à jour signées).")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    secret = os.urandom(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex() + "\n")
    pub = kodo_ed25519.public_key_from_secret(secret).hex()
    print(f"Clé privée créée : {path}  (permissions 600)")
    print("SAUVEGARDEZ-LA maintenant (gestionnaire de mots de passe / disque chiffré hors ligne).")
    print("Ne la mettez JAMAIS dans le dépôt, sur GitHub, Vercel ou dans un message.\n")
    print(f"Clé publique (à placer dans kodo_base.py > TRUSTED_PUBLIC_KEYS) :\n  {pub}")
    if args.add_to_base:
        add_public_key(pub)


def add_public_key(pub_hex: str):
    p = os.path.join(ROOT, "kodo_base.py")
    s = open(p, encoding="utf-8").read()
    m = re.search(r"TRUSTED_PUBLIC_KEYS\s*=\s*\[(.*?)\]", s, re.S)
    if not m:
        sys.exit("TRUSTED_PUBLIC_KEYS introuvable dans kodo_base.py")
    existing = re.findall(r'"([0-9a-fA-F]{64})"', m.group(1))
    if pub_hex in existing:
        print("Clé publique déjà présente dans kodo_base.py.")
        return
    body = ",\n".join(f'    "{k}"' for k in existing + [pub_hex])
    s = s[:m.start()] + f"TRUSTED_PUBLIC_KEYS = [\n{body},\n]" + s[m.end():]
    open(p, "w", encoding="utf-8").write(s)
    print("Clé publique ajoutée à kodo_base.py (à embarquer dans le prochain DMG).")


def cmd_sign_dist(args):
    pl.parse_version(args.version)
    payload = open(args.zip, "rb").read()
    sig = sign_payload("dist", args.version, payload)
    out = args.zip + ".sig"
    open(out, "w").write(sig + "\n")
    assert pl.verify_signature("dist", args.version, payload, sig) or not pl_has_keys(), "auto-vérification échouée"
    print(f"Signature écrite : {out}")


def pl_has_keys() -> bool:
    import kodo_base
    return bool(kodo_base.TRUSTED_PUBLIC_KEYS)


def changed_python_files(rev: str):
    def git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    names = set(git("diff", "--name-only", rev)) | set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(n for n in names if n.endswith(".py") and os.path.exists(os.path.join(ROOT, n)))


def cmd_make_patch(args):
    pl.parse_version(args.version)
    base_min = args.base
    base_max = args.base_max or args.base
    pl.parse_version(base_min), pl.parse_version(base_max)

    if args.empty:
        candidates = []
    elif args.since:
        candidates = changed_python_files(args.since)
    else:
        candidates = [os.path.relpath(os.path.abspath(f), ROOT) for f in args.files]
    if not candidates and not args.empty:
        sys.exit("Aucun fichier. Donnez des fichiers, ou --since <tag/commit>, ou --empty.")

    files, skipped = {}, []
    for rel in candidates:
        rel = rel.replace(os.sep, "/")
        if args.since and rel.startswith(NOT_MODULES):
            continue  # fichiers de dev, jamais des modules de l'app
        try:
            pl.validate_module_path(rel)
        except pl.PatchError as e:
            skipped.append((rel, str(e)))
            continue
        files[rel] = open(os.path.join(ROOT, rel), "rb").read()

    for rel, why in skipped:
        print(f"  IGNORÉ  {rel} — {why}")
    if skipped and not args.since:
        sys.exit("Certains fichiers ne sont pas patchables à distance : ils demandent un nouveau DMG.")

    manifest = {
        "format": pl.FORMAT_VERSION, "version": args.version, "base_min": base_min, "base_max": base_max,
        "notes": args.notes or "",
        "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in sorted(files.items())},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for rel, data in sorted(files.items()):
            zf.writestr(rel, data)
    payload = buf.getvalue()
    pl.parse_bundle(payload)  # validation complète de la structure avant signature
    sig = sign_payload("backend", args.version, payload)

    out_dir = os.path.join(ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"backend_v{args.version}.zip")
    open(zip_path, "wb").write(payload)
    open(zip_path + ".sig", "w").write(sig + "\n")

    print(f"\nPatch backend v{args.version} (bases {base_min} → {base_max}) : {len(files)} fichier(s)")
    for rel in sorted(files):
        print(f"   • {rel}")
    print(f"\nÉcrit : {os.path.relpath(zip_path, ROOT)}  (+ .sig)")
    print("\nÀ ajouter dans public/latest.json ET src/app/api/version/route.ts (même version que le dist) :")
    print(json.dumps({"backendPatch": {
        "version": args.version,
        "url": f"https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/backend_v{args.version}.zip",
    }}, indent=2))


def cmd_verify(args):
    payload = open(args.zip, "rb").read()
    sig = open(args.zip + ".sig").read()
    ok = pl.verify_signature(args.kind, args.version, payload, sig)
    print("Signature VALIDE" if ok else "Signature INVALIDE")
    if ok and args.kind == "backend":
        b = pl.parse_bundle(payload)
        print(f"Manifeste OK : v{b.version}, bases {b.manifest['base_min']}→{b.manifest['base_max']}, {len(b.files)} fichier(s)")
    sys.exit(0 if ok else 1)


def cmd_stamp_base(_args):
    upd = open(os.path.join(ROOT, "kodo_core", "services", "updater.py"), encoding="utf-8").read()
    m = re.search(r'^CURRENT_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', upd, re.M)
    if not m:
        sys.exit("CURRENT_VERSION introuvable dans updater.py")
    p = os.path.join(ROOT, "kodo_base.py")
    s = open(p, encoding="utf-8").read()
    s2, n = re.subn(r'^BASE_VERSION\s*=\s*"[^"]*"', f'BASE_VERSION = "{m.group(1)}"', s, flags=re.M)
    if n != 1:
        sys.exit("BASE_VERSION introuvable dans kodo_base.py")
    open(p, "w", encoding="utf-8").write(s2)
    print(f"kodo_base.BASE_VERSION = {m.group(1)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("genkey"); g.add_argument("--add-to-base", action="store_true", help="ajoute la clé publique à kodo_base.py")
    g.set_defaults(fn=cmd_genkey)

    d = sub.add_parser("sign-dist"); d.add_argument("zip"); d.add_argument("--version", required=True)
    d.set_defaults(fn=cmd_sign_dist)

    m = sub.add_parser("make-patch")
    m.add_argument("files", nargs="*"); m.add_argument("--version", required=True)
    m.add_argument("--base", required=True, help="version du DMG visé (kodo_base.BASE_VERSION des clients)")
    m.add_argument("--base-max", help="dernière version de DMG visée (défaut : = --base)")
    m.add_argument("--since", help="inclure les .py modifiés depuis ce tag/commit (ex. v1.0.71)")
    m.add_argument("--empty", action="store_true", help="patch vide = retour au code du DMG")
    m.add_argument("--notes"); m.add_argument("--out", default="public")
    m.set_defaults(fn=cmd_make_patch)

    v = sub.add_parser("verify"); v.add_argument("zip"); v.add_argument("--kind", choices=["dist", "backend"], required=True)
    v.add_argument("--version", required=True)
    v.set_defaults(fn=cmd_verify)

    sub.add_parser("stamp-base").set_defaults(fn=cmd_stamp_base)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
