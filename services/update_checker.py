import json
import urllib.request
import urllib.error
import ssl
import threading
import webbrowser
import logging
import os
import zipfile
import shutil
import tempfile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UpdateChecker")

VERSION_FILE = os.path.expanduser("~/Library/Caches/KodoPOS/version.txt")
DIST_USER_DIR = os.path.expanduser("~/Library/Caches/KodoPOS/dist")

def get_installed_version():
    """Returns local installed version from version file or default 1.0.3."""
    paths = [
        os.path.expanduser("~/Library/Caches/KodoPOS/version.txt"),
        os.path.expanduser("~/.kodo_pos/version.txt"),
        os.path.expanduser("~/Documents/Kodo_POS/version.txt")
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    v = f.read().strip()
                    if v:
                        return v
            except Exception as e:
                logger.warning(f"Erreur lecture fichier version {p}: {e}")
    return "1.0.3"

CURRENT_VERSION = get_installed_version()
PRIMARY_UPDATE_URL = "https://kodo-solutions-web.vercel.app/api/version"
FALLBACK_UPDATE_URL = "https://xn--kdo-solutions-sbb.com/api/version"

def parse_version(ver_str):
    """Convert a version string like '1.0.1' into a tuple of integers (1, 0, 1)."""
    try:
        clean_str = str(ver_str).strip().lstrip('v')
        return tuple(int(x) for x in clean_str.split('.') if x.isdigit())
    except Exception:
        return (0, 0, 0)

def is_newer_version(latest_str, current_str=None):
    """Compare versions return True if latest > current."""
    if not current_str:
        current_str = get_installed_version()
    return parse_version(latest_str) > parse_version(current_str)

def check_for_updates_sync(current_version=None):
    """
    Synchronously fetch update info from GitHub Releases.
    Returns dict with update status.
    """
    if not current_version:
        current_version = get_installed_version()

    # URL to fetch the latest release from GitHub API
    github_api_url = "https://api.github.com/repos/rulmontkiama/Kodo-Ecosystem/releases/latest"
    
    try:
        ssl_ctx = ssl.create_default_context()
    except Exception:
        ssl_ctx = ssl._create_unverified_context()

    try:
        req = urllib.request.Request(
            github_api_url, 
            headers={'User-Agent': 'KodoPOS-Desktop/1.0.0'}
        )
        try:
            resp_obj = urllib.request.urlopen(req, timeout=5, context=ssl_ctx)
        except Exception:
            unverified_ctx = ssl._create_unverified_context()
            resp_obj = urllib.request.urlopen(req, timeout=5, context=unverified_ctx)

        with resp_obj as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                
                # Ex: "v1.0.4" -> "1.0.4"
                latest_ver = data.get("tag_name", "").lstrip('v')
                if not latest_ver:
                    latest_ver = current_version
                    
                download_url = data.get("html_url", "https://github.com/rulmontkiama/Kodo-Ecosystem/releases")
                changelog = data.get("body", "Mise à jour de sécurité et de performances.")
                
                # Check if the GitHub version is greater than the current version
                has_update = is_newer_version(latest_ver, current_version)
                
                return {
                    "has_update": has_update,
                    "current_version": current_version,
                    "latest_version": latest_ver,
                    "download_url": download_url,
                    "dist_patch_url": None,
                    "changelog": changelog,
                    "mandatory": False,
                    "error": None
                }
    except Exception as e:
        logger.warning(f"Erreur vérification maj sur GitHub: {e}")
            
    return {
        "has_update": False,
        "current_version": current_version,
        "latest_version": current_version,
        "download_url": "https://github.com/rulmontkiama/Kodo-Ecosystem/releases",
        "dist_patch_url": None,
        "changelog": "",
        "mandatory": False,
        "error": "Impossible de contacter le serveur de mise à jour GitHub."
    }

def check_for_updates_async(callback, current_version=None):
    """
    Run update check in background thread to keep UI responsive.
    Calls callback(result_dict) on completion.
    """
    def _worker():
        res = check_for_updates_sync(current_version)
        callback(res)
        
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

def apply_remote_update_sync(dist_patch_url, target_version=None):
    """
    Downloads dist_patch_url (a zip containing dist assets),
    extracts it to ~/.kodo_pos/dist,
    and updates ~/.kodo_pos/version.txt.
    """
    if not dist_patch_url:
        return {"success": False, "error": "Aucune URL de correctif fournie."}

    try:
        os.makedirs(DIST_USER_DIR, exist_ok=True)
        
        temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip").name
        req = urllib.request.Request(dist_patch_url, headers={'User-Agent': 'KodoPOS-Desktop/1.0.0'})
        
        try:
            ctx = ssl.create_default_context()
        except Exception:
            ctx = ssl._create_unverified_context()

        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as response, open(temp_zip, 'wb') as out_f:
                shutil.copyfileobj(response, out_f)
        except Exception:
            unverified_ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=30, context=unverified_ctx) as response, open(temp_zip, 'wb') as out_f:
                shutil.copyfileobj(response, out_f)

        extract_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        source_dist = extract_dir
        if os.path.exists(os.path.join(extract_dir, "dist")):
            source_dist = os.path.join(extract_dir, "dist")

        os.makedirs(DIST_USER_DIR, exist_ok=True)
        for item in os.listdir(source_dist):
            s = os.path.join(source_dist, item)
            d = os.path.join(DIST_USER_DIR, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

        # Write target version to version files
        ver_val = target_version or "1.0.6"
        for vfile in [VERSION_FILE, os.path.expanduser("~/Documents/Kodo_POS/version.txt")]:
            try:
                os.makedirs(os.path.dirname(vfile), exist_ok=True)
                with open(vfile, 'w', encoding='utf-8') as f:
                    f.write(ver_val.strip())
            except Exception:
                pass

        try: os.remove(temp_zip)
        except Exception: pass
        try: shutil.rmtree(extract_dir)
        except Exception: pass

        return {
            "success": True, 
            "installed_version": ver_val,
            "message": "Mise à jour appliquée avec succès."
        }

    except Exception as e:
        logger.error(f"Erreur application maj à distance: {e}")
        return {"success": False, "error": str(e)}

def open_download_page(download_url):
    """Opens default browser to download the installer."""
    if download_url:
        webbrowser.open(download_url)
