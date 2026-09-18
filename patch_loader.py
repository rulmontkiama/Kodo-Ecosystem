# -*- coding: utf-8 -*-
"""
Kōdo POS - Chargeur de patchs backend signés (correctifs Python à distance).

Principe
--------
* Un patch est un zip signé (Ed25519, clé privée hors ligne) contenant un `manifest.json` et des
  fichiers `.py` qui REMPLACENT, à l'import, les modules embarqués dans le DMG.
* Rien n'est jamais exécuté avant vérification : signature (liée au type "backend" et au numéro de
  version), plage de versions de base, version strictement croissante, chemins autorisés, empreintes
  SHA-256 et compilation de chaque fichier.
* Le zip vérifié est conservé tel quel sur disque et RE-VÉRIFIÉ à chaque démarrage ; le code est
  lu depuis la mémoire, jamais depuis des fichiers .py posés sur le disque (pas de fichier à
  falsifier, pas de cache .pyc).
* Retour arrière automatique : un patch dont le démarrage n'est pas confirmé (`mark_healthy`)
  MAX_PENDING_BOOTS fois de suite est mis en quarantaine et l'application revient à la version
  précédente, ou au code d'origine du DMG.
* Retour arrière à distance : publier un patch de version supérieure avec `files: {}` revient au code
  du DMG.

Ce module est la racine de confiance : il ne dépend d'aucun module kodo_core (il est activé AVANT
leur import) et ne peut pas être remplacé par un patch (DENIED_MODULES).
"""

import base64
import hashlib
import importlib.abc
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zipfile

import kodo_base
import kodo_ed25519

logger = logging.getLogger("kodo.patch")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[PATCH] %(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

FORMAT_VERSION = 1
MAX_PENDING_BOOTS = 3            # démarrages non confirmés avant quarantaine
MAX_ZIP_BYTES = 30 * 1024 * 1024
MAX_FILES = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
KEEP_VERSIONS = 3                # anciennes versions conservées sur disque

# Modules qu'un patch ne peut jamais remplacer : ils constituent la racine de confiance et le
# mécanisme de mise à jour lui-même (un patch défectueux ne doit pas pouvoir les casser).
DENIED_MODULES = frozenset({
    "patch_loader",
    "kodo_ed25519",
    "kodo_base",
    "launch_app",
    "kodo_core.services.updater",
})

_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*\.py$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

_STATE = {"activated": False, "version": None}


class PatchError(Exception):
    """Patch invalide, non signé, incompatible ou refusé."""


class PatchAlreadyInstalled(PatchError):
    """Cette version de patch est déjà installée."""


# --------------------------------------------------------------------------- utilitaires

def get_patch_root() -> str:
    override = os.environ.get("KODO_PATCH_ROOT")  # utilisé par les tests
    if override:
        return override
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Kodo_POS/patches")
    return os.path.expanduser("~/.kodo_pos/patches")


def parse_version(v: str) -> tuple:
    v = str(v).strip().lstrip("v")
    if not _VERSION_RE.fullmatch(v):
        raise PatchError(f"Numéro de version invalide : {v!r}")
    return tuple(int(x) for x in v.split("."))


def module_name(rel_path: str) -> str:
    return rel_path[:-3].replace("/", ".")


def validate_module_path(rel: str) -> str:
    """Chemin relatif autorisé pour un fichier de patch. Retourne le nom de module."""
    if not isinstance(rel, str) or not _PATH_RE.fullmatch(rel):
        raise PatchError(f"Chemin de fichier non autorisé : {rel!r}")
    if rel == "__init__.py" or rel.endswith("/__init__.py"):
        raise PatchError(f"Les fichiers __init__.py ne sont pas patchables : {rel}")
    name = module_name(rel)
    if name in DENIED_MODULES:
        raise PatchError(f"Module protégé, non patchable à distance : {name}")
    return name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_atomic(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_json(path: str, obj):
    _write_atomic(path, json.dumps(obj, indent=2, sort_keys=True).encode("utf-8"))


def _state_path(root):
    return os.path.join(root, "state.json")


def _active_path(root):
    return os.path.join(root, "active.json")


def _read_state(root) -> dict:
    st = _read_json(_state_path(root))
    return st if isinstance(st, dict) else {}


def _update_state(root, **changes):
    st = _read_state(root)
    st.update(changes)
    _write_json(_state_path(root), st)


# --------------------------------------------------------------------------- signature

def signed_message(kind: str, version: str, payload: bytes) -> bytes:
    """Message réellement signé : sépare les usages (dist / backend) et lie la version."""
    return b"KODO-UPDATE-V1\x00" + kind.encode() + b"\x00" + version.encode() + b"\x00" + payload


def verify_signature(kind: str, version: str, payload: bytes, signature_b64) -> bool:
    """Vrai si `payload` est signé pour (kind, version) par l'une des clés de confiance."""
    try:
        if isinstance(signature_b64, bytes):
            signature_b64 = signature_b64.decode("ascii")
        sig = base64.b64decode(str(signature_b64).strip(), validate=True)
    except Exception:
        return False
    if len(sig) != 64:
        return False
    message = signed_message(kind, str(version), payload)
    for key_hex in list(kodo_base.TRUSTED_PUBLIC_KEYS):
        try:
            if kodo_ed25519.verify(bytes.fromhex(key_hex), message, sig):
                return True
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------- bundle

class Bundle:
    def __init__(self, manifest, files, zip_sha256):
        self.manifest = manifest
        self.files = files            # {chemin_relatif: bytes}
        self.zip_sha256 = zip_sha256

    @property
    def version(self) -> str:
        return self.manifest["version"]


def parse_bundle(zip_bytes: bytes) -> Bundle:
    """Ouvre et valide la structure d'un zip de patch (sans vérifier la signature)."""
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise PatchError("Archive de patch trop volumineuse.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise PatchError("Archive de patch illisible.")

    infos = [i for i in zf.infolist() if not i.is_dir()]
    names = [i.filename for i in infos]
    if len(infos) > MAX_FILES + 1:
        raise PatchError("Trop de fichiers dans le patch.")
    if len(set(names)) != len(names):
        raise PatchError("Entrées dupliquées dans le patch.")
    if "manifest.json" not in names:
        raise PatchError("manifest.json manquant.")
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES or any(i.file_size > MAX_FILE_BYTES for i in infos):
        raise PatchError("Patch trop volumineux.")

    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except Exception:
        raise PatchError("manifest.json invalide.")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_VERSION:
        raise PatchError("Format de manifeste non supporté.")
    version = manifest.get("version")
    base_min = manifest.get("base_min")
    base_max = manifest.get("base_max")
    parse_version(version), parse_version(base_min), parse_version(base_max)
    if parse_version(base_min) > parse_version(base_max):
        raise PatchError("Plage de versions de base incohérente.")
    declared = manifest.get("files")
    if not isinstance(declared, dict):
        raise PatchError("Liste de fichiers du manifeste invalide.")

    files = {}
    for rel, expected in declared.items():
        validate_module_path(rel)
        if rel not in names:
            raise PatchError(f"Fichier déclaré mais absent de l'archive : {rel}")
        data = zf.read(rel)
        if not isinstance(expected, str) or _sha256(data) != expected.lower():
            raise PatchError(f"Empreinte SHA-256 incorrecte : {rel}")
        try:
            compile(data, rel, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            raise PatchError(f"Erreur de syntaxe dans {rel} : {e}")
        files[rel] = data

    extra = set(names) - set(files) - {"manifest.json"}
    if extra:
        raise PatchError(f"Fichiers non déclarés dans le manifeste : {sorted(extra)[:3]}")
    return Bundle(manifest, files, _sha256(zip_bytes))


def check_base(manifest: dict):
    base = parse_version(kodo_base.BASE_VERSION)
    if not (parse_version(manifest["base_min"]) <= base <= parse_version(manifest["base_max"])):
        raise PatchError(
            f"Patch incompatible avec cette version de base ({kodo_base.BASE_VERSION}) : "
            f"attendu {manifest['base_min']} à {manifest['base_max']}."
        )


def _highest_version(root) -> tuple:
    best = (0, 0, 0)
    active = _read_json(_active_path(root))
    if isinstance(active, dict) and active.get("version"):
        try:
            best = max(best, parse_version(active["version"]))
        except PatchError:
            pass
    try:
        best = max(best, parse_version(_read_state(root).get("highest_installed", "0.0.0")))
    except PatchError:
        pass
    return best


# --------------------------------------------------------------------------- installation

def prepare_bundle(zip_bytes: bytes, signature_b64, expected_version: str, root: str = None) -> Bundle:
    """
    Toutes les vérifications, SANS rien écrire. Lève PatchError (ou PatchAlreadyInstalled).
    """
    root = root or get_patch_root()
    parse_version(expected_version)
    if not verify_signature("backend", expected_version, zip_bytes, signature_b64):
        raise PatchError("Signature du patch backend absente ou invalide.")
    bundle = parse_bundle(zip_bytes)
    if bundle.version != expected_version:
        raise PatchError("La version du manifeste ne correspond pas à la version annoncée.")
    check_base(bundle.manifest)
    highest = _highest_version(root)
    wanted = parse_version(expected_version)
    if wanted == highest:
        raise PatchAlreadyInstalled(f"Patch {expected_version} déjà installé.")
    if wanted < highest:
        raise PatchError("Version de patch plus ancienne que celle déjà installée (retour arrière refusé).")
    return bundle


def commit_bundle(bundle: Bundle, zip_bytes: bytes, signature_b64, root: str = None) -> dict:
    """Écrit le patch vérifié et le rend actif au prochain démarrage (écriture atomique)."""
    root = root or get_patch_root()
    if isinstance(signature_b64, bytes):
        signature_b64 = signature_b64.decode("ascii")
    vdir = os.path.join(root, "versions", bundle.version)
    _write_atomic(os.path.join(vdir, "patch.zip"), zip_bytes)
    _write_atomic(os.path.join(vdir, "patch.sig"), str(signature_b64).strip().encode("ascii"))

    current = _read_json(_active_path(root))
    previous = None
    if isinstance(current, dict) and current.get("version"):
        previous = {"version": current["version"], "sha256": current.get("sha256")}
    _write_json(_active_path(root), {
        "version": bundle.version,
        "sha256": bundle.zip_sha256,
        "previous": previous,
    })
    _update_state(root, pending_boots=0, highest_installed=bundle.version)
    _prune_versions(root)
    logger.info(f"Patch backend {bundle.version} installé ({len(bundle.files)} fichier(s)) — actif au redémarrage.")
    return {
        "version": bundle.version,
        "files": sorted(bundle.files),
        "previous": previous["version"] if previous else None,
        "restart_required": True,
    }


def install_bundle(zip_bytes: bytes, signature_b64, expected_version: str, root: str = None) -> dict:
    root = root or get_patch_root()
    bundle = prepare_bundle(zip_bytes, signature_b64, expected_version, root)
    return commit_bundle(bundle, zip_bytes, signature_b64, root)


def rollback_last_install(root: str = None) -> bool:
    """Annule la dernière installation (utilisé si la mise à jour de l'interface échoue ensuite)."""
    root = root or get_patch_root()
    active = _read_json(_active_path(root))
    if not isinstance(active, dict):
        return False
    _promote_previous(root, active)
    return True


def _prune_versions(root):
    vroot = os.path.join(root, "versions")
    try:
        entries = [d for d in os.listdir(vroot) if _VERSION_RE.fullmatch(d)]
    except OSError:
        return
    active = _read_json(_active_path(root)) or {}
    keep = {active.get("version"), (active.get("previous") or {}).get("version")}
    for d in sorted(entries, key=parse_version, reverse=True)[:KEEP_VERSIONS]:
        keep.add(d)
    for d in entries:
        if d not in keep:
            try:
                for f in os.listdir(os.path.join(vroot, d)):
                    os.remove(os.path.join(vroot, d, f))
                os.rmdir(os.path.join(vroot, d))
            except OSError:
                pass


# --------------------------------------------------------------------------- activation

def _load_entry(root, entry) -> Bundle:
    """Recharge un patch installé en re-vérifiant tout (empreinte, signature, structure, base)."""
    version = entry.get("version")
    parse_version(version)
    vdir = os.path.join(root, "versions", version)
    with open(os.path.join(vdir, "patch.zip"), "rb") as f:
        zip_bytes = f.read()
    with open(os.path.join(vdir, "patch.sig"), "r", encoding="ascii") as f:
        sig = f.read()
    if _sha256(zip_bytes) != entry.get("sha256"):
        raise PatchError("Empreinte du patch installé modifiée.")
    if not verify_signature("backend", version, zip_bytes, sig):
        raise PatchError("Signature du patch installé invalide.")
    bundle = parse_bundle(zip_bytes)
    if bundle.version != version:
        raise PatchError("Version du manifeste incohérente.")
    check_base(bundle.manifest)
    return bundle


def _quarantine(root, entry, reason):
    logger.error(f"Patch {entry.get('version')} mis en quarantaine : {reason}")
    try:
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "rejected.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": entry.get("version"),
                                "reason": str(reason)}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _promote_previous(root, entry):
    """Remplace le patch actif par le précédent (ou par le code du DMG s'il n'y en a pas)."""
    prev = entry.get("previous") if isinstance(entry, dict) else None
    if isinstance(prev, dict) and prev.get("version") and prev.get("sha256"):
        new = {"version": prev["version"], "sha256": prev["sha256"], "previous": None}
        _write_json(_active_path(root), new)
        _update_state(root, pending_boots=0)
        return new
    try:
        os.remove(_active_path(root))
    except OSError:
        pass
    _update_state(root, pending_boots=0)
    return None


class _PatchLoader(importlib.abc.Loader):
    def __init__(self, rel: str, source: bytes):
        self._rel = rel
        self._source = source

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # `__file__` imite l'emplacement du fichier d'origine pour le code qui s'en sert.
        fake = os.path.join(_source_root(), *self._rel.split("/"))
        module.__file__ = fake
        exec(compile(self._source, fake, "exec", dont_inherit=True), module.__dict__)

    def get_source(self, fullname):
        return self._source.decode("utf-8", "replace")


class _PatchFinder(importlib.abc.MetaPathFinder):
    def __init__(self, version: str, modules: dict):
        self.version = version
        self._modules = modules  # {nom_module: (chemin_relatif, source)}

    def find_spec(self, fullname, path=None, target=None):
        item = self._modules.get(fullname)
        if item is None:
            return None
        rel, source = item
        return importlib.util.spec_from_loader(
            fullname, _PatchLoader(rel, source),
            origin=f"kodo-patch:{self.version}/{rel}", is_package=False,
        )


def _source_root() -> str:
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def activate(root: str = None):
    """
    À appeler au tout début de launch_app.py, AVANT l'import de server_pos / kodo_core.
    Ne lève jamais : en cas de problème l'application démarre avec le code du DMG.
    Retourne la version du patch activé, ou None.
    """
    _STATE["activated"] = True
    try:
        return _activate(root or get_patch_root())
    except Exception as e:  # jamais bloquer le démarrage
        logger.error(f"Activation des patchs ignorée : {e}")
        return None


def _activate(root):
    active = _read_json(_active_path(root))
    if not isinstance(active, dict) or not active.get("version"):
        return None

    entry = active
    pending = int(_read_state(root).get("pending_boots", 0) or 0)
    if pending >= MAX_PENDING_BOOTS:
        _quarantine(root, entry, f"{pending} démarrages consécutifs sans confirmation de bon fonctionnement")
        entry = _promote_previous(root, entry)
        if entry is None:
            return None

    bundle = None
    for _ in range(2):  # patch courant, puis version précédente
        try:
            bundle = _load_entry(root, entry)
            break
        except Exception as e:
            _quarantine(root, entry, e)
            entry = _promote_previous(root, entry)
            if entry is None:
                return None
    if bundle is None:
        return None

    modules = {}
    for rel, source in bundle.files.items():
        name = module_name(rel)
        if name in sys.modules:
            logger.warning(f"Module {name} déjà importé avant l'activation : patch ignoré pour ce module.")
            continue
        modules[name] = (rel, source)

    _update_state(root, pending_boots=int(_read_state(root).get("pending_boots", 0) or 0) + 1)
    if modules:
        sys.meta_path.insert(0, _PatchFinder(bundle.version, modules))
        importlib.invalidate_caches()
    _STATE["version"] = bundle.version
    logger.info(f"Patch backend {bundle.version} activé ({len(modules)} module(s) remplacé(s)).")
    return bundle.version


def mark_healthy(root: str = None):
    """À appeler quand le serveur a répondu correctement : confirme que le patch fonctionne."""
    if not _STATE["version"]:
        return
    try:
        root = root or get_patch_root()
        _update_state(root, pending_boots=0, last_good=_STATE["version"])
    except Exception as e:
        logger.warning(f"mark_healthy : {e}")


def active_version():
    return _STATE["version"]


def is_active() -> bool:
    """Vrai si le chargeur a été activé par le lanceur (donc un redémarrage propre est possible)."""
    return _STATE["activated"]


def status(root: str = None) -> dict:
    root = root or get_patch_root()
    st = _read_state(root)
    return {
        "base_version": kodo_base.BASE_VERSION,
        "loaded_version": _STATE["version"],
        "pending_boots": st.get("pending_boots", 0),
        "last_good": st.get("last_good"),
        "highest_installed": st.get("highest_installed"),
    }


# --------------------------------------------------------------------------- redémarrage

def relaunch_command():
    exe = sys.executable
    if getattr(sys, "frozen", False):
        idx = exe.find(".app/Contents/MacOS/")
        if idx != -1:
            return ["/bin/sh", "-c", 'sleep 4; open -n "$0"', exe[:idx + 4]]
        return ["/bin/sh", "-c", 'sleep 4; exec "$@"', "sh", exe] + sys.argv[1:]
    return ["/bin/sh", "-c", 'sleep 4; exec "$@"', "sh", exe] + sys.argv


def can_restart() -> bool:
    return _STATE["activated"] and os.name == "posix"


def schedule_restart(delay: float = 2.0):
    """Relance l'application après `delay` s (laisse le temps de répondre à la requête HTTP)."""
    def _go():
        try:
            subprocess.Popen(relaunch_command(), start_new_session=True, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            os._exit(0)
    t = threading.Timer(delay, _go)
    t.daemon = True
    t.start()
