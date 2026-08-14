import os
import sys
import re
import json
import urllib.request
import zipfile
import shutil
import tempfile
import ssl

CURRENT_VERSION = "1.0.18"

VERSION_URLS = [
    "https://kodo-solutions.vercel.app/api/version",
    "https://raw.githubusercontent.com/rulmontkiama/Kodo-Ecosystem/main/public/latest.json",
    "https://kodo-solutions.com/api/version"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*"
}

def parse_version(v_str):
    if not v_str:
        return (0, 0, 0)
    digits = re.findall(r'\d+', str(v_str))
    return tuple(int(d) for d in digits)

def get_installed_version():
    return CURRENT_VERSION

def check_for_updates_sync():
    data = None
    last_err = "Aucun serveur de mise à jour joignable."
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for url in VERSION_URLS:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
                raw = response.read().decode('utf-8')
                data = json.loads(raw)
                if data and isinstance(data, dict):
                    break
        except Exception as e:
            last_err = str(e)
            continue

    if not data:
        return {"error": last_err, "has_update": False}

    latest = data.get("latestVersion") or data.get("latest_version") or data.get("version")
    if latest:
        data["has_update"] = parse_version(latest) > parse_version(CURRENT_VERSION)
    else:
        data["has_update"] = bool(data.get("has_update", False))

    return data

def get_target_dist_dir():
    try:
        import server_pos
        return server_pos.get_dist_dir()
    except Exception:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(base_dir, "dist")
        if os.path.exists(target) and os.access(os.path.dirname(target), os.W_OK):
            return target
        fallback = os.path.expanduser("~/Library/Caches/KodoPOS/dist")
        os.makedirs(fallback, exist_ok=True)
        return fallback

def apply_remote_update_sync(patch_url, target_ver):
    try:
        if not patch_url:
            return {"success": False, "error": "No patch URL provided"}
            
        dist_dir = get_target_dist_dir()
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        # Download zip with browser headers
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            req = urllib.request.Request(patch_url, headers=HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
                shutil.copyfileobj(response, tmp)
            tmp_path = tmp.name
            
        # Extract to temporary dir
        extract_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(tmp_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
            
        dist_src = None
        for root, dirs, files in os.walk(extract_dir):
            if 'dist' in dirs:
                dist_src = os.path.join(root, 'dist')
                break
            elif 'index.html' in files:
                dist_src = root
                break
                
        if not dist_src:
            dist_src = extract_dir
            
        os.makedirs(dist_dir, exist_ok=True)
        # Overwrite in-place safely without rmtree locking
        shutil.copytree(dist_src, dist_dir, dirs_exist_ok=True)
        
        # Cleanup
        try: os.unlink(tmp_path)
        except Exception: pass
        try: shutil.rmtree(extract_dir, ignore_errors=True)
        except Exception: pass
        
        return {"success": True, "message": f"Mise à jour v{target_ver} installée avec succès !"}
    except Exception as e:
        return {"success": False, "error": str(e)}
