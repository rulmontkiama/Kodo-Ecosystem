import os
import sys
import json
import urllib.request
import zipfile
import shutil
import tempfile
import ssl

CURRENT_VERSION = "1.0.18"
VERSION_URL = "https://kodo-solutions.vercel.app/api/version"

def get_installed_version():
    return CURRENT_VERSION

def check_for_updates_sync():
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(VERSION_URL)
        with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data
    except Exception as e:
        return {"error": str(e), "has_update": False}

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
        
        # Download zip
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            req = urllib.request.Request(patch_url)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
                shutil.copyfileobj(response, tmp)
            tmp_path = tmp.name
            
        # Extract to temporary dir
        extract_dir = tempfile.mkdtemp()
        with zipfile.ZipFile(tmp_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
            
        # The zip from GitHub usually has a root folder like Kodo-Ecosystem-main
        # We need to find the 'dist' directory inside it
        dist_src = None
        for root, dirs, files in os.walk(extract_dir):
            if 'dist' in dirs:
                dist_src = os.path.join(root, 'dist')
                break
                
        if not dist_src:
            return {"success": False, "error": "dist folder not found in update package"}
            
        # Replace local dist
        if os.path.exists(dist_dir):
            shutil.rmtree(dist_dir)
        shutil.copytree(dist_src, dist_dir)
        
        # Cleanup
        os.unlink(tmp_path)
        shutil.rmtree(extract_dir)
        
        return {"success": True, "message": f"Updated to {target_ver}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
