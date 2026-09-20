#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Python REST API & Static Web Server
Passerelle REST API et serveur statique déléguant la logique métier à kodo_core.
"""

import os
import sys
import json
import signal
import sqlite3
import subprocess
import time
import datetime
from decimal import Decimal
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Ajout du dossier courant au path Python
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import database_manager
from database_manager import get_connection, initialiser_db, hash_pin
import export_manager
import pdf_generator
import ticket_printer

from kodo_core.api.app import kodo_app

# Initialisation de la base de données au démarrage
initialiser_db()


import re
import shutil

def is_dist_valid(dist_path: str) -> bool:
    """Vérifie qu'un dossier dist contient bien un index.html et son bundle JS existant."""
    if not dist_path or not os.path.exists(dist_path):
        return False
    index_file = os.path.join(dist_path, 'index.html')
    if not os.path.exists(index_file):
        return False
    try:
        with open(index_file, 'r', encoding='utf-8') as f:
            html = f.read()
        match = re.search(r'src=["\']([^"\']+\.js)["\']', html)
        if match:
            js_rel = match.group(1).lstrip('/')
            full_js_path = os.path.join(dist_path, js_rel)
            if not os.path.exists(full_js_path):
                return False
        return True
    except Exception:
        return True


def _version_tuple(v) -> tuple:
    return tuple(int(d) for d in re.findall(r"\d+", str(v or "")))


def is_cache_dist_current(version_file: str) -> bool:
    """Vrai si l'interface en cache a été installée par une mise à jour au moins aussi récente que
    l'application. Sans cette vérification, une interface téléchargée avant une réinstallation du
    DMG masquait pour toujours celle du DMG (l'app se croit à jour et ne la retélécharge jamais)."""
    try:
        import kodo_base
        with open(version_file, "r", encoding="utf-8") as f:
            cached = json.load(f).get("version")
        return _version_tuple(cached) >= _version_tuple(kodo_base.BASE_VERSION)
    except Exception:
        return False


def get_dist_dir():
    # 1. Priorité aux mises à jour dynamiques installées dans le cache utilisateur,
    #    à condition qu'elles ne soient pas plus anciennes que l'application installée
    cache_dist = os.path.expanduser("~/Library/Caches/KodoPOS/dist")
    if is_dist_valid(cache_dist) and is_cache_dist_current(
            os.path.expanduser("~/Library/Caches/KodoPOS/version.json")):
        return cache_dist

    win_cache = os.path.expanduser("~/.kodo_pos/dist")
    if is_dist_valid(win_cache) and is_cache_dist_current(
            os.path.expanduser("~/.kodo_pos/version.json")):
        return win_cache

    # 2. En mode exécutable / production (PyInstaller gelé) -> bundle propre embarqué
    if getattr(sys, 'frozen', False):
        meipass_dist = os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), "dist")
        if is_dist_valid(meipass_dist):
            return meipass_dist
        resources_dist = os.path.normpath(os.path.join(os.path.dirname(sys.executable), "..", "Resources", "dist"))
        if is_dist_valid(resources_dist):
            return resources_dist
        return meipass_dist

    # 3. En mode développement / source, prioriser le dist local ou Desktop
    desktop_dist = os.path.expanduser("~/Desktop/kōdo-pos-3/dist")
    if is_dist_valid(desktop_dist):
        return desktop_dist

    local_dist = os.path.join(BASE_DIR, "dist")
    if is_dist_valid(local_dist):
        return local_dist

    return os.path.join(BASE_DIR, "dist")


def json_serial(obj):
    """JSON serializer pour datetime, date, Decimal et objets complexes."""
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


class POSRequestHandler(BaseHTTPRequestHandler):
    """Gestionnaire de requêtes HTTP déléguant à la couche kodo_core API REST."""

    def _set_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _send_json(self, data, code=200):
        try:
            body = json.dumps(data, ensure_ascii=False, default=json_serial).encode('utf-8')
        except Exception:
            body = json.dumps({"error": "Erreur sérialisation"}, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._set_cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, content_bytes, code=200, headers=None):
        self.send_response(code)
        self._set_cors_headers()
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        if 'Content-Length' not in (headers or {}):
            self.send_header('Content-Length', str(len(content_bytes)))
        self.end_headers()
        self.wfile.write(content_bytes)

    def _send_error(self, message, code=400):
        self._send_json({"error": message}, code)

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def _dispatch_to_kodo_core(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Extraction du body JSON si présent
        length = int(self.headers.get('Content-Length', 0))
        body_bytes = self.rfile.read(length) if length > 0 else b'{}'

        data = {}
        if body_bytes and method in ["POST", "PUT", "DELETE"]:
            try:
                data = json.loads(body_bytes.decode('utf-8'))
            except Exception:
                data = {}

        headers_in = {k: v for k, v in self.headers.items()}

        status_code, response_content, response_headers = kodo_app.handle_request(
            method=method,
            path=path,
            query=query,
            headers=headers_in,
            data=data
        )

        if status_code != 404 or path.startswith("/api/"):
            if isinstance(response_content, bytes):
                self._send_bytes(response_content, status_code, response_headers)
            else:
                self._send_json(response_content, status_code)
            return True

        return False

    def do_GET(self):
        if self.path.startswith("/api/"):
            if self._dispatch_to_kodo_core("GET"):
                return

        parsed = urlparse(self.path)
        self._serve_static(parsed.path)

    def do_POST(self):
        if not self._dispatch_to_kodo_core("POST"):
            self._send_error("Route API introuvable", 404)

    def do_PUT(self):
        if not self._dispatch_to_kodo_core("PUT"):
            self._send_error("Route API introuvable", 404)

    def do_DELETE(self):
        if not self._dispatch_to_kodo_core("DELETE"):
            self._send_error("Route API introuvable", 404)

    def _serve_static(self, path):
        dist_dir = get_dist_dir()
        if path == '/' or not path:
            file_path = os.path.join(dist_dir, 'index.html')
        else:
            rel_path = path.lstrip('/')
            file_path = os.path.abspath(os.path.join(dist_dir, rel_path))

        # Sécurité Anti-Path Traversal
        real_dist = os.path.abspath(dist_dir)
        if not file_path.startswith(real_dist) or not os.path.exists(file_path) or os.path.isdir(file_path):
            file_path = os.path.join(dist_dir, 'index.html')

        if not os.path.exists(file_path):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self._set_cors_headers()
            self.end_headers()
            self.wfile.write(b"<h1>Kodo POS API Active</h1><p>Veuillez compiler le frontend React dans dist/.</p>")
            return

        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'text/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.json': 'application/json',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.svg': 'image/svg+xml',
            '.ico': 'image/x-icon',
            '.woff2': 'font/woff2'
        }
        ctype = content_types.get(ext, 'application/octet-stream')

        with open(file_path, 'rb') as f:
            content = f.read()

        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        if ext in ['.html', '.json']:
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(content)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

def _kodo_server_answers(port: int, timeout: float = 2.0) -> bool:
    """Vrai si un serveur Kōdo POS répond déjà sur ce port (autre instance en cours d'utilisation)."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as resp:
            info = json.loads(resp.read(4096).decode("utf-8", "ignore"))
        return resp.status == 200 and str(info.get("app", "")).startswith("Kōdo POS")
    except Exception:
        return False


def _free_port_from_zombie(port: int) -> None:
    """Arrête le processus (autre que nous) qui occupe le port SANS répondre : serveur figé d'un lancement précédent."""
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return
    for pid in out.split():
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                os.kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
            except Exception:
                pass


def run_server(port=8765, busy_wait=8.0):
    """
    Démarre le serveur. Si le port est déjà pris, on ne tue JAMAIS d'emblée l'occupant (avant, un second
    lancement arrêtait de force l'instance en cours d'utilisation, puis plantait : plus aucun serveur) :
    1. on patiente `busy_wait` s (l'ancienne instance qui se ferme, ex. redémarrage après une mise à jour) ;
    2. si un serveur Kōdo POS répond toujours, c'est une autre instance vivante : on la réutilise ;
    3. s'il ne répond pas (processus figé), on le libère et on prend sa place.
    """
    httpd = None
    deadline = time.monotonic() + busy_wait
    warned = False
    while True:
        try:
            httpd = ReusableHTTPServer(('0.0.0.0', port), POSRequestHandler)
            break
        except OSError as e:
            if not warned:
                print(f"⚠️ [KODO POS SERVER] Port {port} occupé ({e}). Attente de sa libération...")
                warned = True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)

    if httpd is None:
        if _kodo_server_answers(port):
            print(f"ℹ️ [KODO POS SERVER] Une autre instance de Kōdo POS répond déjà sur le port {port} : elle est réutilisée.")
            return
        print(f"⚠️ [KODO POS SERVER] Le port {port} est occupé par un processus qui ne répond pas : libération...")
        _free_port_from_zombie(port)
        time.sleep(0.5)
        try:
            httpd = ReusableHTTPServer(('0.0.0.0', port), POSRequestHandler)
        except OSError as ex:
            print(f"❌ [KODO POS SERVER] Échec: {ex}")
            return

    print(f"🚀 [KODO POS SERVER] REST API kodo_core & Web App en ligne sur http://localhost:{port}")
    httpd.serve_forever()


if __name__ == '__main__':
    port = 8765
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port)
