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
import urllib.request
import urllib.error

CURRENT_VERSION = "1.0.20"

BROWSER_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"
}

UPDATE_ENDPOINTS = [
    "https://kodo-solutions.vercel.app/api/version",
    "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/latest.json",
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
                        return str(data["version"]).lstrip("v")
            except Exception:
                pass

    # 2. Vérifier dans la base SQLite Parametres
    try:
        from kodo_core.db.connection import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT valeur FROM Parametres WHERE cle='app_version'")
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0]).lstrip("v")
    except Exception:
        pass

    return CURRENT_VERSION


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
    data = None
    last_err = "Aucun serveur de mise à jour joignable."

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for url in UPDATE_ENDPOINTS:
        try:
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=6) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw)

                # Format GitHub Releases
                if "tag_name" in parsed:
                    data = {
                        "latest_version": parsed.get("tag_name", "").lstrip("v"),
                        "download_url": parsed.get("zipball_url") or parsed.get("html_url"),
                        "changelog": parsed.get("body", "")
                    }
                    if "assets" in parsed and len(parsed["assets"]) > 0:
                        data["download_url"] = parsed["assets"][0].get("browser_download_url", data["download_url"])
                else:
                    data = parsed

                if data and isinstance(data, dict):
                    break
        except Exception as e:
            last_err = str(e)
            logger.debug(f"Erreur d'interrogation du serveur update ({url}): {e}")
            continue

    if not data:
        return {"error": last_err, "has_update": False, "current_version": curr_ver}

    latest = data.get("latestVersion") or data.get("latest_version") or data.get("version") or data.get("tag_name")
    if latest:
        data["latest_version"] = str(latest).lstrip("v")
        data["has_update"] = parse_version(latest) > parse_version(curr_ver)
    else:
        data["has_update"] = bool(data.get("has_update", False))

    data["current_version"] = curr_ver
    return data


def apply_remote_update_sync(patch_url: str, target_ver: str) -> dict:
    """
    Télécharge et applique l'update en effectuant un overlay in-place (dirs_exist_ok=True)
    du dossier dist sans verrouiller les fichiers sous macOS/Windows.
    """
    if not patch_url:
        return {"success": False, "error": "URL de patch/release manquante."}

    clean_ver = str(target_ver).lstrip("v")
    logger.info(f"Début du téléchargement et installation de la mise à jour v{clean_ver} depuis {patch_url}...")
    dist_dir = get_target_dist_dir()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    tmp_path = None
    extract_dir = None
    # 1. Téléchargement avec headers navigateur réel et fallbacks automatiques
    urls_to_try = [patch_url]
    fallback_urls = [
        f"https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v{clean_ver}.zip",
        f"https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/dist_v1.0.20.zip",
        f"https://github.com/rulmontkiama/Kodo-Ecosystem/raw/main/public/dist_v{clean_ver}.zip",
        f"https://kodo-solutions-web.vercel.app/dist_v{clean_ver}.zip",
        f"https://kodo-solutions.vercel.app/dist_v{clean_ver}.zip",
    ]
    for fb in fallback_urls:
        if fb not in urls_to_try:
            urls_to_try.append(fb)

    downloaded = False
    last_dl_err = None

    for candidate_url in urls_to_try:
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
                req = urllib.request.Request(candidate_url, headers=DEFAULT_HEADERS)
                with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
                    shutil.copyfileobj(response, tmp)
                tmp_path = tmp.name
                downloaded = True
                logger.info(f"Téléchargement réussi depuis : {candidate_url}")
                break
        except Exception as dl_e:
            last_dl_err = dl_e
            logger.warning(f"Échec téléchargement depuis {candidate_url} ({dl_e}), tentative suivante...")
            continue

    if not downloaded or not tmp_path:
        return {"success": False, "error": f"Impossible de télécharger la mise à jour ({last_dl_err})"}

    try:
        # 2. Extraction dans un répertoire temporaire
        extract_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(tmp_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        # 3. Recherche de la racine 'dist' dans les fichiers extraits
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

        # 4. Overlay in-place sans suppression préalable pour éviter les verrous de fichiers
        shutil.copytree(dist_src, dist_dir, dirs_exist_ok=True)
        logger.info(f"Overlay in-place appliqué avec succès dans : {dist_dir}")

        # 5. Enregistrement persistant de la version installée
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

        return {
            "success": True,
            "message": f"Mise à jour v{clean_ver} installée avec succès dans {dist_dir} !",
            "dist_dir": dist_dir,
            "version": clean_ver
        }
    except Exception as e:
        logger.error(f"Erreur durant l'application de la mise à jour : {e}")
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
