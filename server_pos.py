#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS - Python REST API & Static Web Server
Passerelle REST API et serveur statique déléguant la logique métier à kodo_core.
"""

import os
import sys
import json
import sqlite3
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


def get_dist_dir():
    # 1. En mode développement / source, prioriser le dist local
    if not getattr(sys, 'frozen', False):
        local_dist = os.path.join(BASE_DIR, "dist")
        if os.path.exists(local_dist) and os.path.exists(os.path.join(local_dist, 'index.html')):
            return local_dist
        desktop_dist = os.path.expanduser("~/Desktop/kōdo-pos-3/dist")
        if os.path.exists(desktop_dist) and os.path.exists(os.path.join(desktop_dist, 'index.html')):
            return desktop_dist

    # 2. En mode exécutable / production
    candidates = [
        os.path.expanduser("~/Library/Caches/KodoPOS/dist"),
        os.path.expanduser("~/.kodo_pos/dist"),
        os.path.expanduser("~/Documents/Kodo_POS/dist"),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.exists(os.path.join(c, 'index.html')):
            return c

    if getattr(sys, 'frozen', False):
        return os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), "dist")
    else:
        return os.path.join(BASE_DIR, "dist")


class POSRequestHandler(BaseHTTPRequestHandler):
    """Gestionnaire de requêtes HTTP déléguant à la couche kodo_core API REST."""

    def _set_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
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
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(content)


def run_server(port=8765):
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, POSRequestHandler)
    print(f"🚀 [KODO POS SERVER] REST API kodo_core & Web App en ligne sur http://localhost:{port}")
    httpd.serve_forever()


if __name__ == '__main__':
    port = 8765
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port)
