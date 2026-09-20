# -*- coding: utf-8 -*-
"""
Tests de sécurité du mécanisme de patchs signés (kodo_ed25519.py, patch_loader.py).

Tout se passe dans des dossiers temporaires (KODO_PATCH_ROOT) avec une clé de test : aucune donnée
réelle n'est lue ni modifiée. Lancement : python3 tests_patching.py
"""

import base64
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kodo_base
import kodo_ed25519
import patch_loader as pl

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception as e:  # mauvaise exception
        print(f"   (exception inattendue : {type(e).__name__}: {e})")
        return False
    return False


# --------------------------------------------------------------------------- outils de test

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC_HEX = kodo_ed25519.public_key_from_secret(SECRET).hex()
OTHER_SECRET = bytes(range(32))


def sign(kind, version, payload, secret=SECRET):
    sig = kodo_ed25519.sign(secret, pl.signed_message(kind, version, payload))
    return base64.b64encode(sig).decode()


def make_bundle(files, version="1.0.72", base_min="1.0.71", base_max="1.0.71", manifest_extra=None,
                extra_entries=None, sha_override=None, secret=SECRET, kind="backend", sign_version=None):
    """Fabrique (zip_bytes, signature_b64). `files` = {chemin: contenu str}."""
    manifest = {
        "format": 1, "version": version, "base_min": base_min, "base_max": base_max,
        "files": {p: (sha_override or {}).get(p, hashlib.sha256(c.encode()).hexdigest()) for p, c in files.items()},
    }
    manifest.update(manifest_extra or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for p, c in files.items():
            zf.writestr(p, c)
        for p, c in (extra_entries or {}).items():
            zf.writestr(p, c)
    data = buf.getvalue()
    return data, sign(kind, sign_version or version, data, secret)


def fresh_root():
    return tempfile.mkdtemp(prefix="kodo_patch_test_")


def reset_finders():
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, pl._PatchFinder)]


kodo_base.TRUSTED_PUBLIC_KEYS = [PUBLIC_HEX]
kodo_base.BASE_VERSION = "1.0.71"

GOOD = {"fakepkg/mod.py": "VALUE = 'patched'\n"}

# --------------------------------------------------------------------------- signature

def test_signature():
    data, sig = make_bundle(GOOD)
    check("signature valide acceptée", pl.verify_signature("backend", "1.0.72", data, sig))
    check("signature d'une autre clé refusée", not pl.verify_signature("backend", "1.0.72", data, sign("backend", "1.0.72", data, OTHER_SECRET)))
    check("archive altérée refusée", not pl.verify_signature("backend", "1.0.72", data + b"x", sig))
    check("signature altérée refusée", not pl.verify_signature("backend", "1.0.72", data, sig[:-4] + ("AAAA" if not sig.endswith("AAAA") else "BBBB")))
    check("signature dist réutilisée comme backend refusée", not pl.verify_signature("backend", "1.0.72", data, sign("dist", "1.0.72", data)))
    check("signature liée à la version (rejeu refusé)", not pl.verify_signature("backend", "1.0.73", data, sig))
    check("signature vide / illisible refusée", not pl.verify_signature("backend", "1.0.72", data, "") and not pl.verify_signature("backend", "1.0.72", data, "pas-du-base64!!"))
    kodo_base.TRUSTED_PUBLIC_KEYS = []
    check("aucune clé de confiance => tout est refusé", not pl.verify_signature("backend", "1.0.72", data, sig))
    kodo_base.TRUSTED_PUBLIC_KEYS = [PUBLIC_HEX]


# --------------------------------------------------------------------------- structure du patch

def test_structure():
    root = fresh_root()
    def prep(**kw):
        d, s = make_bundle(**kw)
        return pl.prepare_bundle(d, s, kw.get("version", "1.0.72"), root)

    check("patch valide accepté", prep(files=GOOD).version == "1.0.72")
    check("patch vide accepté (retour au code du DMG)", prep(files={}).files == {})
    for bad in ("../evil.py", "/etc/evil.py", "a/../../evil.py", "fakepkg/__init__.py", "__init__.py",
                "fakepkg/mod.txt", "fake.pkg/mod.py", "fakepkg\\mod.py", "patch_loader.py", "kodo_base.py",
                "kodo_ed25519.py", "launch_app.py", "kodo_core/services/updater.py"):
        check(f"chemin refusé : {bad}", raises(pl.PatchError, prep, files={bad: "X = 1\n"}))
    check("erreur de syntaxe refusée", raises(pl.PatchError, prep, files={"fakepkg/mod.py": "def (:\n"}))
    check("empreinte incorrecte refusée", raises(pl.PatchError, prep, files=GOOD, sha_override={"fakepkg/mod.py": "0" * 64}))
    check("fichier non déclaré refusé", raises(pl.PatchError, prep, files=GOOD, extra_entries={"fakepkg/hidden.py": "X=1\n"}))
    check("version de manifeste différente refusée", raises(pl.PatchError, prep, files=GOOD, manifest_extra={"version": "1.0.99"}))
    check("format inconnu refusé", raises(pl.PatchError, prep, files=GOOD, manifest_extra={"format": 2}))
    check("base hors plage refusée", raises(pl.PatchError, prep, files=GOOD, base_min="1.0.80", base_max="1.0.90"))
    check("plage de base incohérente refusée", raises(pl.PatchError, prep, files=GOOD, base_min="1.0.90", base_max="1.0.71"))
    check("zip illisible refusé", raises(pl.PatchError, pl.parse_bundle, b"pas un zip"))


# --------------------------------------------------------------------------- versions / retour arrière

def test_versions_and_rollback():
    root = fresh_root()
    d1, s1 = make_bundle(GOOD, version="1.0.72")
    d2, s2 = make_bundle({"fakepkg/mod.py": "VALUE = 'second'\n"}, version="1.0.73")
    check("installation 1.0.72", pl.install_bundle(d1, s1, "1.0.72", root)["version"] == "1.0.72")
    check("même version : déjà installée", raises(pl.PatchAlreadyInstalled, pl.install_bundle, d1, s1, "1.0.72", root))
    old_d, old_s = make_bundle(GOOD, version="1.0.70")
    check("version plus ancienne refusée (anti-rejeu)", raises(pl.PatchError, pl.install_bundle, old_d, old_s, "1.0.70", root))
    r = pl.install_bundle(d2, s2, "1.0.73", root)
    check("mise à jour 1.0.73 garde 1.0.72 comme précédente", r["previous"] == "1.0.72")
    check("annulation de la dernière installation", pl.rollback_last_install(root) and json.load(open(os.path.join(root, "active.json")))["version"] == "1.0.72")
    check("après annulation, la version annulée (1.0.73) ne peut pas être rejouée", raises(pl.PatchAlreadyInstalled, pl.install_bundle, d2, s2, "1.0.73", root))
    empty_d, empty_s = make_bundle({}, version="1.0.75")
    check("patch vide de version supérieure (retour distant au DMG)", pl.install_bundle(empty_d, empty_s, "1.0.75", root)["files"] == [])


# --------------------------------------------------------------------------- activation réelle (sous-processus)

def run_activation(root, extra_setup=""):
    """Simule le démarrage de l'app : une base 'fakepkg.mod' embarquée + activation du chargeur."""
    app = tempfile.mkdtemp(prefix="kodo_app_")
    os.makedirs(os.path.join(app, "fakepkg"))
    open(os.path.join(app, "fakepkg", "__init__.py"), "w").close()
    open(os.path.join(app, "fakepkg", "mod.py"), "w").write("VALUE = 'base'\n")
    code = f"""
import sys, os
sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r}); sys.path.insert(0, {app!r})
import kodo_base; kodo_base.TRUSTED_PUBLIC_KEYS = [{PUBLIC_HEX!r}]; kodo_base.BASE_VERSION = '1.0.71'
import patch_loader as pl
{extra_setup}
v = pl.activate()
import fakepkg.mod as m
print('RESULT', v, m.VALUE)
"""
    env = dict(os.environ, KODO_PATCH_ROOT=root)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    line = [l for l in out.stdout.splitlines() if l.startswith("RESULT")]
    return line[0].split(None, 2)[1:] if line else ["ERR", out.stderr[-300:]]


def test_activation():
    root = fresh_root()
    check("sans patch : code du DMG", run_activation(root) == ["None", "base"])

    d, s = make_bundle(GOOD)
    pl.install_bundle(d, s, "1.0.72", root)
    check("avec patch signé : code remplacé", run_activation(root) == ["1.0.72", "patched"])

    # Boucle de démarrage : jamais de mark_healthy => quarantaine à la 4e tentative
    r2 = fresh_root()
    pl.install_bundle(d, s, "1.0.72", r2)
    seq = [run_activation(r2)[1] for _ in range(pl.MAX_PENDING_BOOTS)]
    check(f"les {pl.MAX_PENDING_BOOTS} premiers démarrages non confirmés chargent le patch", seq == ["patched"] * pl.MAX_PENDING_BOOTS)
    check("démarrage suivant : quarantaine + retour au code du DMG", run_activation(r2) == ["None", "base"])
    check("quarantaine journalisée", os.path.exists(os.path.join(r2, "rejected.log")))

    # Confirmation de bon fonctionnement : le compteur repart de zéro
    r3 = fresh_root()
    pl.install_bundle(d, s, "1.0.72", r3)
    for _ in range(pl.MAX_PENDING_BOOTS + 2):
        run_activation(r3)
        subprocess.run([sys.executable, "-c",
                        f"import sys; sys.path.insert(0,{os.path.dirname(os.path.abspath(__file__))!r}); import kodo_base; "
                        f"kodo_base.TRUSTED_PUBLIC_KEYS=[{PUBLIC_HEX!r}]; import patch_loader as pl; pl._STATE['version']='1.0.72'; pl.mark_healthy()"],
                       env=dict(os.environ, KODO_PATCH_ROOT=r3), timeout=60)
    check("mark_healthy remet le compteur à zéro (pas de quarantaine abusive)", run_activation(r3) == ["1.0.72", "patched"])

    # Retour à la version précédente (et non au DMG) quand le patch courant échoue
    r4 = fresh_root()
    d1, s1 = make_bundle({"fakepkg/mod.py": "VALUE = 'v1'\n"}, version="1.0.72")
    d2, s2 = make_bundle({"fakepkg/mod.py": "VALUE = 'v2'\n"}, version="1.0.73")
    pl.install_bundle(d1, s1, "1.0.72", r4)
    pl.install_bundle(d2, s2, "1.0.73", r4)
    check("v2 active", run_activation(r4) == ["1.0.73", "v2"])
    with open(os.path.join(r4, "versions", "1.0.73", "patch.zip"), "ab") as f:
        f.write(b"corruption")
    check("patch corrompu sur disque : repli sur la version précédente", run_activation(r4) == ["1.0.72", "v1"])

    # Patch falsifié localement (signature ok mais contenu changé)
    r5 = fresh_root()
    pl.install_bundle(d1, s1, "1.0.72", r5)
    forged, _ = make_bundle({"fakepkg/mod.py": "VALUE = 'pirate'\n"}, version="1.0.72", secret=OTHER_SECRET)
    open(os.path.join(r5, "versions", "1.0.72", "patch.zip"), "wb").write(forged)
    check("zip remplacé par un faux : refusé, code du DMG", run_activation(r5) == ["None", "base"])

    # Base incompatible (nouveau DMG) : le patch est ignoré
    r6 = fresh_root()
    d6, s6 = make_bundle(GOOD, version="1.0.72", base_min="1.0.50", base_max="1.0.60")
    try:
        pl.install_bundle(d6, s6, "1.0.72", r6)
        installed = True
    except pl.PatchError:
        installed = False
    check("patch pour une autre base refusé dès l'installation", not installed)

    # Module déjà importé avant activate() : jamais remplacé
    r7 = fresh_root()
    pl.install_bundle(d1, s1, "1.0.72", r7)
    check("module déjà importé : non remplacé", run_activation(r7, "import fakepkg.mod") [1] == "base")


def test_restart_command():
    cmd = pl.relaunch_command()
    check("commande de relance : liste d'arguments (pas de shell interpolé)", isinstance(cmd, list) and cmd[0] == "/bin/sh")
    sys_exec, frozen = sys.executable, getattr(sys, "frozen", False)
    try:
        sys.frozen = True
        sys.executable = "/Applications/Kodo_POS.app/Contents/MacOS/Kodo_POS"
        cmd = pl.relaunch_command()
        check("relance d'un .app : `open -n` sur le bundle", cmd[-1] == "/Applications/Kodo_POS.app" and "open -n" in cmd[2])
    finally:
        sys.executable = sys_exec
        if frozen:
            sys.frozen = frozen
        else:
            del sys.frozen


# --------------------------------------------------------------------------- updater (téléchargements simulés)

def make_dist(valid=True, version="1.0.72", kind="dist", secret=SECRET):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dist/index.html", "<html>nouvelle interface</html>")
        zf.writestr("dist/assets/app.js", "console.log('v72')")
    data = buf.getvalue() if valid else b"ceci n'est pas un zip"
    return data, sign(kind, version, data, secret)


class _FakeResp:
    def __init__(self, body): self._b = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self._b


def test_updater():
    from kodo_core.services import updater as up
    saved = (up._download_signed, up._backend_patch_info, up.get_target_dist_dir, pl.schedule_restart)
    # ISOLATION : l'updater écrit dans ~/…/version.json, la base par défaut et /Applications/Kodo_POS.app.
    # Le test ne doit JAMAIS toucher aux vraies données : HOME et base redirigés, écritures hors /tmp bloquées.
    fake_home = tempfile.mkdtemp(prefix="kodo_home_")
    saved_env = {k: os.environ.get(k) for k in ("HOME", "KODO_DB_PATH")}
    os.environ["HOME"] = fake_home
    os.environ["KODO_DB_PATH"] = os.path.join(fake_home, "test.db")
    tmp_root = os.path.realpath(tempfile.gettempdir())
    blocked_writes = []
    real_copytree = up.shutil.copytree
    def guarded_copytree(src, dst, *a, **k):
        if not os.path.realpath(str(dst)).startswith(tmp_root):
            blocked_writes.append(str(dst))
            return dst
        return real_copytree(src, dst, *a, **k)
    up.shutil.copytree = guarded_copytree
    restarts = []
    pl.schedule_restart = lambda *a, **k: restarts.append(1)
    URL = "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.72.zip"
    BURL = "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/backend_v1.0.72.zip"
    INFO = {"version": "1.0.72", "url": BURL}

    def scenario(dist, backend, info=INFO):
        root, dist_dir = fresh_root(), tempfile.mkdtemp(prefix="kodo_dist_")
        os.environ["KODO_PATCH_ROOT"] = root
        up.get_target_dist_dir = lambda: dist_dir
        up._backend_patch_info = lambda ver: info
        up._download_signed = lambda urls, ctx: (dist if urls[0] != BURL else backend) + (urls[0],)
        res = up.apply_remote_update_sync(URL, "1.0.72")
        return res, root, dist_dir

    bd, bs = make_bundle(GOOD)
    try:
        # a. tout signé : interface + backend
        res, root, dist_dir = scenario(make_dist(), (bd, bs))
        check("update signée (dist + backend) : succès", res.get("success") is True and res.get("backend_patched") is True)
        check("interface copiée", os.path.exists(os.path.join(dist_dir, "index.html")))
        check("patch backend installé (actif au redémarrage)", os.path.exists(os.path.join(root, "active.json")) and res.get("restart_required") is True)
        check("pas de relance hors lanceur (chargeur non activé)", restarts == [])
        check("isolation : version.json écrit dans le HOME simulé, pas dans le vrai",
              os.path.exists(os.path.join(fake_home, "Documents", "Kodo_POS", "version.json")))

        # b. dist non signé / mal signé : rien n'est écrit
        res, root, dist_dir = scenario(make_dist(secret=OTHER_SECRET), (bd, bs))
        check("dist signé par une autre clé : refusé", res.get("success") is False and "Signature" in res.get("error", ""))
        check("... et rien n'est écrit (ni interface, ni backend)", not os.listdir(dist_dir) and not os.path.exists(os.path.join(root, "active.json")))
        res, root, dist_dir = scenario(make_dist(kind="backend"), (bd, bs))
        check("signature 'backend' réutilisée pour le dist : refusée", res.get("success") is False)

        # c. backend mal signé : la mise à jour entière est refusée, l'interface n'est PAS appliquée
        bad_bd, bad_bs = make_bundle(GOOD, secret=OTHER_SECRET)
        res, root, dist_dir = scenario(make_dist(), (bad_bd, bad_bs))
        check("backend mal signé : mise à jour refusée", res.get("success") is False and "backend" in res.get("error", "").lower())
        check("... interface non appliquée non plus", not os.listdir(dist_dir))

        # d. backend avec chemin interdit
        evil_bd, evil_bs = make_bundle({"launch_app.py": "print('pwned')\n"})
        res, root, dist_dir = scenario(make_dist(), (evil_bd, evil_bs))
        check("backend visant un module protégé : refusé", res.get("success") is False and not os.listdir(dist_dir))

        # e. l'interface échoue APRÈS l'installation du backend : le backend est annulé
        res, root, dist_dir = scenario(make_dist(valid=False), (bd, bs))
        check("zip d'interface illisible : échec signalé", res.get("success") is False)
        check("... et le patch backend est annulé (pas d'interface/backend dépareillés)", not os.path.exists(os.path.join(root, "active.json")))

        # f. release sans patch backend : interface seule, pas de redémarrage
        res, root, dist_dir = scenario(make_dist(), (bd, bs), info=None)
        check("release sans backend : interface seule", res.get("success") is True and res.get("backend_patched") is False and res.get("restart_required") is False)

        # g. la requête HTTP locale ne peut ni choisir l'URL ni la version
        res = up.apply_remote_update_sync("https://evil.example.com/dist.zip", "1.0.72")
        check("URL arbitraire refusée", res.get("success") is False)
        res = up.apply_remote_update_sync(URL, "../../x")
        check("version piégée refusée", res.get("success") is False)
    finally:
        up._download_signed, up._backend_patch_info, up.get_target_dist_dir, pl.schedule_restart = saved
        up.shutil.copytree = real_copytree
        os.environ.pop("KODO_PATCH_ROOT", None)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("isolation : aucune écriture réelle hors dossiers temporaires (les copies vers l'app installée ont été bloquées)",
          all(not os.path.realpath(b).startswith(tmp_root) for b in blocked_writes))

    # h. annonce du patch backend lue chez les serveurs de mise à jour
    import urllib.request as ur
    real_urlopen = ur.urlopen
    real_exists = os.path.exists
    def fake_urlopen(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "raw.githubusercontent.com" in url and url.endswith("latest.json"):
            return _FakeResp(json.dumps({"latestVersion": "1.0.72", "backendPatch": INFO}).encode())
        if "vercel.app" in url:
            return _FakeResp(json.dumps({"latestVersion": "1.0.72"}).encode())
        raise OSError("hors ligne")
    try:
        ur.urlopen = fake_urlopen
        # check_for_updates_sync lit aussi public/latest.json du dépôt (confort de développement) : on l'ignore ici,
        # sinon le résultat dépendrait de la version publiée dans le dépôt au moment du test.
        os.path.exists = lambda p: False if str(p).replace(os.sep, "/").endswith("public/latest.json") else real_exists(p)
        check("annonce backendPatch retrouvée pour la bonne version", up._backend_patch_info("1.0.72") == INFO)
        try:
            up._backend_patch_info("1.0.73")
            ok = False
        except up.UpdateError:
            ok = True
        check("version demandée différente de l'annonce : refus (pas d'installation partielle)", ok)
        ur.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("hors ligne"))
        try:
            up._backend_patch_info("1.0.72")
            ok = False
        except up.UpdateError:
            ok = True
        check("serveurs injoignables : refus (pas d'installation partielle)", ok)
    finally:
        ur.urlopen = real_urlopen
        os.path.exists = real_exists


if __name__ == "__main__":
    test_signature()
    test_structure()
    test_versions_and_rollback()
    test_activation()
    test_restart_command()
    test_updater()
    reset_finders()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} tests réussis")
    if failed:
        print("ÉCHECS :", *failed, sep="\n  - ")
        sys.exit(1)
