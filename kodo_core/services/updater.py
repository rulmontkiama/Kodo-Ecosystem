"""
Kōdo POS - Moteur d'Auto-Update Git & GitHub Releases / Vercel Cloud
Contournement Cloudflare/Vercel (User-Agent navigateur réel), comparaison SemVer et overlay dist in-place.
"""

import os
import sys
import re
import json
import ssl
import shutil
import logging
import hashlib
import zipfile
import tempfile
import datetime
import sqlite3
import urllib.parse
import urllib.request
import urllib.error

import patch_loader

CURRENT_VERSION = "1.0.71"

BROWSER_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"
}

UPDATE_ENDPOINTS = [
    "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/latest.json",
    "https://kodo-solutions.vercel.app/api/version",
    "https://api.github.com/repos/rulmontkiama/Kodo-Ecosystem/releases/latest"
]

logger = logging.getLogger("kodo_core.services.updater")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[UPDATER Core] %(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class UpdateError(Exception):
    """Exception levée en cas d'erreur durant le processus de mise à jour."""
    pass


# Hôtes et préfixes de chemin autorisés pour télécharger un patch. Le corps de /api/apply-update
# est contrôlé par l'appelant (CORS ouvert sur le serveur local) : il ne doit jamais pouvoir
# désigner une URL arbitraire.
TRUSTED_PATCH_SOURCES = {
    "raw.githubusercontent.com": "/rulmontkiama/Kodo-Ecosystem/",
    "github.com": "/rulmontkiama/Kodo-Ecosystem/",
    "kodo-solutions-web.vercel.app": "/",
    "kodo-solutions.vercel.app": "/",
}


def is_trusted_patch_url(url: str) -> bool:
    """Vrai si l'URL est en HTTPS et pointe vers une source de patch autorisée."""
    try:
        parsed = urllib.parse.urlparse(str(url))
    except Exception:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    prefix = TRUSTED_PATCH_SOURCES.get((parsed.hostname or "").lower())
    return prefix is not None and parsed.path.startswith(prefix)


def build_ssl_context() -> ssl.SSLContext:
    """
    Contexte TLS avec vérification du certificat ET du nom d'hôte, toujours activée.
    Utilise le magasin de certificats certifi s'il est présent (Python macOS/PyInstaller n'embarque
    pas toujours les CA système) ; sans lui, on retombe sur le magasin par défaut. En cas d'échec de
    validation, la mise à jour échoue : on ne dégrade jamais vers une connexion non vérifiée.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def parse_version(v_str: str) -> tuple:
    """
    Extrait les composants numériques d'un tag de version (ex: 'v1.0.19' -> (1, 0, 19)).
    Permet la comparaison stricte de tuples SemVer.
    """
    if not v_str:
        return (0, 0, 0)
    digits = re.findall(r"\d+", str(v_str))
    return tuple(int(d) for d in digits)


def get_installed_version() -> str:
    """Renvoie la version actuellement installée du logiciel."""
    installed = CURRENT_VERSION

    # 1. Vérifier le fichier version.json dans le cache / documents
    version_files = [
        os.path.expanduser("~/Library/Caches/KodoPOS/version.json"),
        os.path.expanduser("~/.kodo_pos/version.json"),
        os.path.expanduser("~/Documents/Kodo_POS/version.json"),
    ]
    for vf in version_files:
        if os.path.exists(vf):
            try:
                with open(vf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("version"):
                        v_candidate = str(data["version"]).lstrip("v")
                        if parse_version(v_candidate) >= parse_version(installed):
                            installed = v_candidate
            except Exception:
                pass

    # 2. Vérifier dans la base SQLite Parametres
    try:
        from kodo_core.db.connection import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT valeur FROM Parametres WHERE cle='app_version'")
        row = cursor.fetchone()
        if row and row[0]:
            v_db = str(row[0]).lstrip("v")
            if parse_version(v_db) >= parse_version(installed):
                installed = v_db
        else:
            # Enregistrer la version courante dans la base si absente
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('app_version', ?)", (installed,))
            conn.commit()
        conn.close()
    except Exception:
        pass

    return installed


def get_target_dist_dir() -> str:
    """Détermine le dossier dist cible inscriptible pour l'application des assets IHM/Web."""
    # 1. En mode développement source (si dist/ local est inscriptible)
    if not getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        target = os.path.join(base_dir, "dist")
        if os.path.exists(target) and os.access(os.path.dirname(target), os.W_OK):
            return target

    # 2. En mode exécutable / production macOS ou Windows
    if sys.platform.startswith("win"):
        target_dir = os.path.expanduser("~/.kodo_pos/dist")
    else:
        target_dir = os.path.expanduser("~/Library/Caches/KodoPOS/dist")
    
    os.makedirs(target_dir, exist_ok=True)
    return target_dir


def check_for_updates_sync(current_version: str = None) -> dict:
    """
    Interroge les serveurs d'update (Vercel / GitHub Releases) avec un User-Agent navigateur réel.
    """
    curr_ver = current_version or get_installed_version()
    candidates = []
    last_err = "Aucun serveur de mise à jour joignable."

    ctx = build_ssl_context()

    for url in UPDATE_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw)

                # Format GitHub Releases
                if "tag_name" in parsed:
                    v_raw = parsed.get("tag_name", "").lstrip("v")
                    d_url = parsed.get("zipball_url") or parsed.get("html_url")
                    if "assets" in parsed and len(parsed["assets"]) > 0:
                        d_url = parsed["assets"][0].get("browser_download_url", d_url)
                    data_entry = {
                        "latest_version": v_raw,
                        "version": v_raw,
                        "download_url": d_url,
                        "dist_patch_url": d_url,
                        "changelog": parsed.get("body", "")
                    }
                    candidates.append((parse_version(v_raw), data_entry))
                elif isinstance(parsed, dict):
                    v_raw = parsed.get("latestVersion") or parsed.get("latest_version") or parsed.get("version")
                    if v_raw:
                        candidates.append((parse_version(v_raw), parsed))
        except Exception as e:
            last_err = str(e)
            logger.debug(f"Erreur d'interrogation du serveur update ({url}): {e}")
            continue

    # Vérifier aussi le fichier public/latest.json local si disponible
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        local_latest = os.path.join(repo_root, "public", "latest.json")
        if os.path.exists(local_latest):
            with open(local_latest, "r", encoding="utf-8") as f:
                parsed = json.load(f)
                v_raw = parsed.get("latestVersion") or parsed.get("latest_version") or parsed.get("version")
                if v_raw:
                    candidates.append((parse_version(v_raw), parsed))
    except Exception:
        pass

    if not candidates:
        return {"error": last_err, "has_update": False, "current_version": curr_ver}

    # Trier par version SemVer décroissante et prendre la plus récente
    candidates.sort(key=lambda c: c[0], reverse=True)
    best_tuple, data = candidates[0]

    latest = data.get("latestVersion") or data.get("latest_version") or data.get("version") or data.get("tag_name")
    if latest:
        data["latest_version"] = str(latest).lstrip("v")
        data["has_update"] = parse_version(latest) > parse_version(curr_ver)
    else:
        data["has_update"] = bool(data.get("has_update", False))

    # Un autre endpoint de MÊME version peut porter l'annonce du patch backend (ex. latest.json GitHub)
    if not (data.get("backendPatch") or data.get("backend_patch")):
        for _v, other in candidates:
            same = str(other.get("latestVersion") or other.get("latest_version") or other.get("version") or "").lstrip("v")
            if same == str(data.get("latest_version")) and (other.get("backendPatch") or other.get("backend_patch")):
                data["backendPatch"] = other.get("backendPatch") or other.get("backend_patch")
                break

    data["current_version"] = curr_ver
    return data


MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024
MAX_SIGNATURE_BYTES = 4096


def _download(url: str, ctx, cap: int = MAX_DOWNLOAD_BYTES, timeout: int = 45) -> bytes:
    """Télécharge `url` en mémoire (HTTPS vérifié), avec plafond de taille."""
    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    chunks, total = [], 0
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise UpdateError("Fichier de mise à jour trop volumineux.")
            chunks.append(chunk)
    return b"".join(chunks)


def _download_signed(urls: list, ctx) -> tuple:
    """Télécharge le premier miroir autorisé qui fournit l'archive ET sa signature `<url>.sig`."""
    last_err = None
    for url in urls:
        if not is_trusted_patch_url(url):
            continue
        try:
            payload = _download(url, ctx)
            signature = _download(url + ".sig", ctx, cap=MAX_SIGNATURE_BYTES).decode("ascii", "replace")
            logger.info(f"Téléchargement réussi depuis : {url}")
            return payload, signature, url
        except Exception as e:
            last_err = e
            logger.warning(f"Échec téléchargement depuis {url} ({e}), tentative suivante...")
    raise UpdateError(f"Impossible de télécharger la mise à jour ({last_err})")


def _backend_patch_info(clean_ver: str):
    """
    Patch backend annoncé par les serveurs de mise à jour pour cette version (ou None).
    L'URL vient du serveur de mise à jour, jamais de la requête HTTP locale.
    """
    data = check_for_updates_sync()
    # Les infos doivent décrire EXACTEMENT la version demandée. Sinon (serveurs injoignables, fichier
    # local plus ancien, nouvelle release entre-temps) on refuse plutôt que d'installer l'interface
    # sans le correctif backend qui l'accompagne.
    if str(data.get("latest_version") or "").lstrip("v") != clean_ver:
        raise UpdateError("Impossible de confirmer le contenu de la mise à jour (serveur injoignable ou version modifiée). Réessayez.")
    info = data.get("backendPatch") or data.get("backend_patch")
    if isinstance(info, dict) and str(info.get("version", "")).lstrip("v") == clean_ver and info.get("url"):
        return info
    return None


def apply_remote_update_sync(patch_url: str, target_ver: str) -> dict:
    """
    Installe une mise à jour SIGNÉE : interface (dist) et, si la release en contient un, correctif
    backend. Tout est téléchargé et vérifié (signature, version, plage de base, chemins, empreintes)
    AVANT toute écriture. Le backend est appliqué en premier (atomique) ; si l'interface échoue
    ensuite, il est annulé. Le backend ne prend effet qu'après redémarrage (relancé automatiquement).
    """
    if not patch_url:
        return {"success": False, "error": "URL de patch/release manquante."}

    clean_ver = str(target_ver or "").strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", clean_ver):
        return {"success": False, "error": "Numéro de version invalide."}
    if not is_trusted_patch_url(patch_url):
        logger.warning(f"URL de patch refusée (source non autorisée) : {patch_url}")
        return {"success": False, "error": "URL de patch non autorisée."}

    logger.info(f"Début du téléchargement et installation de la mise à jour v{clean_ver} depuis {patch_url}...")
    dist_dir = get_target_dist_dir()
    ctx = build_ssl_context()

    tmp_path = None
    extract_dir = None
    backend = None
    backend_committed = False

    # Miroirs de la MÊME version (les signatures rendent le choix du miroir sans risque)
    urls_to_try = [patch_url]
    for fb in (
        f"https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v{clean_ver}.zip",
        f"https://github.com/rulmontkiama/Kodo-Ecosystem/raw/main/public/dist_v{clean_ver}.zip",
        f"https://kodo-solutions-web.vercel.app/dist_v{clean_ver}.zip",
        f"https://kodo-solutions.vercel.app/dist_v{clean_ver}.zip",
    ):
        if fb not in urls_to_try:
            urls_to_try.append(fb)

    try:
        # 1. Télécharger et VÉRIFIER tout, sans rien écrire
        dist_zip, dist_sig, _used = _download_signed(urls_to_try, ctx)
        if not patch_loader.verify_signature("dist", clean_ver, dist_zip, dist_sig):
            logger.error("Signature de l'interface invalide ou absente : mise à jour refusée.")
            return {"success": False, "error": "Signature de la mise à jour invalide ou absente : installation refusée."}

        info = _backend_patch_info(clean_ver)
        if info:
            b_zip, b_sig, _ = _download_signed([str(info["url"])], ctx)
            try:
                backend = (patch_loader.prepare_bundle(b_zip, b_sig, clean_ver), b_zip, b_sig)
            except patch_loader.PatchAlreadyInstalled:
                backend = None
            except patch_loader.PatchError as e:
                logger.error(f"Correctif backend refusé : {e}")
                return {"success": False, "error": f"Correctif backend refusé : {e}"}

        # 2. Correctif backend (écriture atomique, actif au redémarrage)
        restart_required = False
        if backend:
            patch_loader.commit_bundle(*backend)
            backend_committed = True
            restart_required = True

        # 3. Interface : extraction dans un répertoire temporaire
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            tmp.write(dist_zip)
            tmp_path = tmp.name
        extract_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(tmp_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        # 4. Recherche de la racine 'dist' dans les fichiers extraits
        dist_src = None
        for root, dirs, files in os.walk(extract_dir):
            if "dist" in dirs:
                dist_src = os.path.join(root, "dist")
                break
            elif "index.html" in files:
                dist_src = root
                break

        if not dist_src:
            dist_src = extract_dir

        os.makedirs(dist_dir, exist_ok=True)

        # 5. Overlay in-place sans suppression préalable pour éviter les verrous de fichiers
        shutil.copytree(dist_src, dist_dir, dirs_exist_ok=True)
        logger.info(f"Overlay in-place appliqué avec succès dans : {dist_dir}")

        # Si exécutable macOS .app, overlay direct dans Resources/dist
        try:
            for app_cand in [
                "/Applications/Kodo_POS.app/Contents/Resources/dist",
                os.path.join(os.path.dirname(sys.executable), "..", "Resources", "dist"),
            ]:
                norm_cand = os.path.normpath(app_cand)
                if os.path.exists(norm_cand) and os.access(norm_cand, os.W_OK):
                    shutil.copytree(dist_src, norm_cand, dirs_exist_ok=True)
        except Exception:
            pass

        # 6. Enregistrement persistant de la version installée
        ver_info = {
            "version": clean_ver,
            "installed_at": datetime.datetime.now().isoformat(),
            "patch_url": patch_url,
            "dist_dir": dist_dir
        }
        for vf in [
            os.path.expanduser("~/Library/Caches/KodoPOS/version.json"),
            os.path.expanduser("~/.kodo_pos/version.json"),
            os.path.expanduser("~/Documents/Kodo_POS/version.json"),
        ]:
            try:
                os.makedirs(os.path.dirname(vf), exist_ok=True)
                with open(vf, "w", encoding="utf-8") as f:
                    json.dump(ver_info, f, indent=2)
            except Exception:
                pass

        try:
            from kodo_core.db.connection import get_connection
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES ('app_version', ?)", (clean_ver,))
            conn.commit()
            conn.close()
        except Exception:
            pass

        message = f"Mise à jour v{clean_ver} installée avec succès dans {dist_dir} !"
        restarting = bool(restart_required and patch_loader.can_restart())
        if restarting:
            message += " Le logiciel va redémarrer pour appliquer le correctif."
            patch_loader.schedule_restart()
        return {
            "success": True,
            "message": message,
            "dist_dir": dist_dir,
            "version": clean_ver,
            "backend_patched": bool(backend),
            "restart_required": restart_required,
            "restarting": restarting,
        }
    except Exception as e:
        logger.error(f"Erreur durant l'application de la mise à jour : {e}")
        if backend_committed:
            try:
                patch_loader.rollback_last_install()
                logger.warning("Correctif backend annulé (l'installation de l'interface a échoué).")
            except Exception as rb_e:
                logger.error(f"Annulation du correctif backend impossible : {rb_e}")
        return {"success": False, "error": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        if extract_dir and os.path.exists(extract_dir):
            try:
                shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception:
                pass


class AppUpdateEngine:
    """Moteur de mise à jour transactionnel avec permutation atomique et rollback."""

    @classmethod
    def calculate_sha256(cls, filepath: str) -> str:
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()

    @classmethod
    def check_for_updates(cls, current_version: str = None) -> dict:
        return check_for_updates_sync(current_version=current_version)

    @classmethod
    def apply_update(cls, patch_url: str, target_ver: str) -> dict:
        return apply_remote_update_sync(patch_url, target_ver)
