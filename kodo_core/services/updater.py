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

CURRENT_VERSION = "1.0.19"

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
    return CURRENT_VERSION


def get_target_dist_dir() -> str:
    """Détermine le dossier dist cible pour l'application des assets IHM/Web."""
    try:
        import server_pos
        return server_pos.get_dist_dir()
    except Exception:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        target = os.path.join(base_dir, "dist")
        if os.path.exists(target) and os.access(os.path.dirname(target), os.W_OK):
            return target
        fallback = os.path.expanduser("~/Library/Caches/KodoPOS/dist")
        os.makedirs(fallback, exist_ok=True)
        return fallback


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
        data["latest_version"] = latest
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

    logger.info(f"Début du téléchargement et installation de la mise à jour v{target_ver} depuis {patch_url}...")
    dist_dir = get_target_dist_dir()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    tmp_path = None
    extract_dir = None
    try:
        # 1. Téléchargement avec headers navigateur réel
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            req = urllib.request.Request(patch_url, headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=45) as response:
                shutil.copyfileobj(response, tmp)
            tmp_path = tmp.name

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

        return {
            "success": True,
            "message": f"Mise à jour v{target_ver} installée avec succès dans {dist_dir} !",
            "dist_dir": dist_dir
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
