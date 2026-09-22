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
import ipaddress
import socket
import logging
import threading
from decimal import Decimal
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("kodo.server")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[SERVER] %(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

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
from kodo_core.config import ShopConfig

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


# ---------------------------------------------------------------------------------------------
# Sécurité réseau de l'API locale (audit technique du 20/09/2026, point C1)
# L'API n'a pas encore d'authentification : elle ne doit répondre qu'aux pages servies par ce poste.
#  1. Écoute sur 127.0.0.1 (ShopConfig.get_host, surchargeable par la variable KODO_HOST).
#  2. En-tête Host : tout nom de domaine autre que « localhost » est refusé. Sans cela, un site piégé
#     qui fait pointer son propre nom vers 127.0.0.1 (« DNS rebinding ») devient « même origine » que
#     l'API et peut lire ses réponses, malgré l'écoute locale et le CORS restreint.
#  3. CORS limité aux pages locales, et refus des requêtes venues d'un autre site (Sec-Fetch-Site,
#     Origin) : un POST « simple » part sans preflight, CORS seul n'empêche donc pas les écritures.
# ---------------------------------------------------------------------------------------------
HOTES_LOCAUX = frozenset({"localhost", "127.0.0.1", "::1"})
METHODES_ECRITURE = frozenset({"POST", "PUT", "DELETE"})


def _nom_hote(valeur) -> str:
    """« localhost:8765 » -> « localhost » ; « [::1]:8765 » -> « ::1 »."""
    valeur = str(valeur or "").strip().lower()
    if valeur.startswith("["):
        return valeur[1:valeur.find("]")] if "]" in valeur else valeur[1:]
    if valeur.count(":") == 1:
        valeur = valeur.split(":", 1)[0]
    return valeur.rstrip(".")


def hote_autorise(entete_host) -> bool:
    """Vrai si l'en-tête Host désigne ce poste : « localhost » ou une adresse IP littérale.
    Une IP ne peut pas servir au DNS rebinding (qui repose sur un nom de domaine) ; l'accepter garde
    un écran distant fonctionnel si KODO_HOST ouvre un jour l'écoute au réseau local.
    Sans en-tête Host (client non navigateur, ex. urllib sans Host), la requête est acceptée."""
    if entete_host is None or not str(entete_host).strip():
        return True
    nom = _nom_hote(entete_host)
    if nom in HOTES_LOCAUX:
        return True
    try:
        ipaddress.ip_address(nom)
        return True
    except ValueError:
        return False


def origine_autorisee(origin, entete_host=None) -> bool:
    """Vrai si la page à l'origine de la requête est servie par ce poste : localhost / 127.0.0.1
    quel que soit le port (l'app sur 8765, le serveur Vite de développement sur 3000), ou ce serveur
    lui-même sous l'adresse de l'en-tête Host (même origine)."""
    if not origin or str(origin).strip().lower() == "null":
        return False
    try:
        parsed = urlparse(str(origin).strip())
        nom = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not nom:
        return False
    if nom in HOTES_LOCAUX:
        return True
    return bool(entete_host) and parsed.netloc.lower() == str(entete_host).strip().lower()


class POSRequestHandler(BaseHTTPRequestHandler):
    """Gestionnaire de requêtes HTTP déléguant à la couche kodo_core API REST."""

    def _set_cors_headers(self):
        # Plus de « * » : seules les pages servies par ce poste peuvent lire les réponses (audit C1).
        # En production l'interface est servie par ce serveur (même origine) : l'en-tête ne sert qu'au
        # serveur Vite de développement (http://localhost:3000).
        origin = self.headers.get('Origin')
        if origin and origine_autorisee(origin, self.headers.get('Host')):
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Vary', 'Origin')

    def _requete_refusee(self, method: str) -> bool:
        """Répond 403 et retourne True si la requête ne vient pas d'une page servie par ce poste."""
        host = self.headers.get('Host')
        if not hote_autorise(host):
            self._send_error("Hôte non autorisé : l'API Kōdo POS n'est accessible que depuis ce poste.", 403)
            return True
        chemin = urlparse(self.path).path or ''
        if chemin.startswith('/api/') and (self.headers.get('Sec-Fetch-Site') or '').lower() == 'cross-site':
            self._send_error("Requête provenant d'un autre site refusée.", 403)
            return True
        origin = self.headers.get('Origin')
        if method in METHODES_ECRITURE and origin is not None and not origine_autorisee(origin, host):
            self._send_error("Origine non autorisée.", 403)
            return True
        return False

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

    def _send_error(self, message, code=400, error_code="ERROR"):
        self._send_json({"success": False, "error": message, "code": error_code}, code)

    def do_OPTIONS(self):
        try:
            if self._requete_refusee("OPTIONS"):
                return
            self.send_response(200)
            self._set_cors_headers()
            self.end_headers()
        except Exception as ex:
            logger.exception(f"Erreur OPTIONS {self.path}: {ex}")

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
        try:
            if self._requete_refusee("GET"):
                return
            if self.path.startswith("/api/"):
                if self._dispatch_to_kodo_core("GET"):
                    return

            parsed = urlparse(self.path)
            self._serve_static(parsed.path)
        except Exception as ex:
            logger.exception(f"Erreur non gérée GET {self.path}: {ex}")
            self._send_error(f"Erreur serveur interne : {ex}", 500, "INTERNAL_SERVER_ERROR")

    def do_POST(self):
        try:
            if self._requete_refusee("POST"):
                return
            if not self._dispatch_to_kodo_core("POST"):
                self._send_error("Route API introuvable", 404, "NOT_FOUND")
        except Exception as ex:
            logger.exception(f"Erreur non gérée POST {self.path}: {ex}")
            self._send_error(f"Erreur serveur interne : {ex}", 500, "INTERNAL_SERVER_ERROR")

    def do_PUT(self):
        try:
            if self._requete_refusee("PUT"):
                return
            if not self._dispatch_to_kodo_core("PUT"):
                self._send_error("Route API introuvable", 404, "NOT_FOUND")
        except Exception as ex:
            logger.exception(f"Erreur non gérée PUT {self.path}: {ex}")
            self._send_error(f"Erreur serveur interne : {ex}", 500, "INTERNAL_SERVER_ERROR")

    def do_DELETE(self):
        try:
            if self._requete_refusee("DELETE"):
                return
            if not self._dispatch_to_kodo_core("DELETE"):
                self._send_error("Route API introuvable", 404, "NOT_FOUND")
        except Exception as ex:
            logger.exception(f"Erreur non gérée DELETE {self.path}: {ex}")
            self._send_error(f"Erreur serveur interne : {ex}", 500, "INTERNAL_SERVER_ERROR")

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


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """Serveur HTTP multi-threadé ultra-réactif avec réutilisation d'adresse et threads démons."""
    allow_reuse_address = True
    daemon_threads = True

# Alias de rétrocompatibilité ascendante
ReusableHTTPServer = ReusableThreadingHTTPServer


# Instance de serveur active, exposée pour permettre au thread principal d'installer les
# gestionnaires de signaux : signal.signal() lève ValueError hors du thread principal, et
# run_server s'exécute dans un thread démon (launch_app.py). Sans cela, le checkpoint WAL
# de fermeture ne s'exécutait jamais dans l'application réellement livrée.
_SERVEUR_ACTIF = None


def _construire_shutdown_handler(server):
    def _shutdown_handler(signum, frame):
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        print(f"\n🛑 [KODO POS SERVER] Signal {sig_name} reçu. Démarrage de l'arrêt gracieux...")
        try:
            conn = get_connection()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.close()
            print("💾 [KODO POS SERVER] Journal WAL vérifié et vidé (TRUNCATE).")
        except Exception as ex:
            print(f"⚠️ [KODO POS SERVER] Erreur checkpoint WAL : {ex}")

        threading.Thread(target=server.shutdown, daemon=True).start()

    return _shutdown_handler


def installer_arret_gracieux() -> bool:
    """
    Installe SIGINT/SIGTERM sur le serveur actif. À appeler depuis le THREAD PRINCIPAL.
    Retourne True si les gestionnaires sont effectivement en place.
    """
    if _SERVEUR_ACTIF is None:
        print("⚠️ [KODO POS SERVER] Arrêt gracieux non installé : aucun serveur actif.")
        return False
    try:
        signal.signal(signal.SIGINT, _construire_shutdown_handler(_SERVEUR_ACTIF))
        signal.signal(signal.SIGTERM, _construire_shutdown_handler(_SERVEUR_ACTIF))
        return True
    except (ValueError, AttributeError) as e:
        print(f"⚠️ [KODO POS SERVER] Arrêt gracieux indisponible ({e}) : "
              "le journal WAL ne sera pas replié à la fermeture.")
        return False


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


def _port_deja_ecoute(port: int, timeout: float = 0.5) -> bool:
    """Vrai si un processus écoute déjà ce port en local (sur 127.0.0.1 ou 0.0.0.0).
    Depuis l'écoute sur 127.0.0.1, l'échec du bind ne suffit plus à détecter une autre instance :
    sous macOS (sémantique BSD de SO_REUSEADDR), lier 127.0.0.1:port réussit même si une version
    précédente écoute encore sur 0.0.0.0:port ; sous Windows, SO_REUSEADDR laisse même lier deux fois
    la même adresse. Deux serveurs sur la même base pourraient alors forker le chaînage des tickets."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (TimeoutError, socket.timeout):
        # Si la connexion time out, le port est OCCUPÉ par un processus qui ne répond pas
        return True
    except OSError:
        pass

    # Vérification de secours via lsof (macOS / Linux)
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=2).stdout
        for pid in out.split():
            if pid.isdigit() and int(pid) != os.getpid():
                return True
    except Exception:
        pass

    return False


def run_server(port=8765, busy_wait=8.0, host=None):
    """
    Démarre le serveur multi-threadé Kōdo POS.
    Gère la libération des ports orphelins et le graceful shutdown.
    """
    host = host or ShopConfig.get_host()
    httpd = None
    deadline = time.monotonic() + busy_wait
    warned = False
    while True:
        try:
            if _port_deja_ecoute(port):
                raise OSError(f"port {port} déjà en écoute par un autre processus")
            httpd = ReusableThreadingHTTPServer((host, port), POSRequestHandler)
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
            httpd = ReusableThreadingHTTPServer((host, port), POSRequestHandler)
        except OSError as ex:
            print(f"❌ [KODO POS SERVER] Échec: {ex}")
            return

    global _SERVEUR_ACTIF
    _SERVEUR_ACTIF = httpd
    # L'installation des signaux est délibérément faite par le THREAD PRINCIPAL
    # (launch_app.open_native_window, après wait_for_server). L'appeler ici, depuis le
    # thread démon du serveur, échouerait systématiquement et afficherait au commerçant
    # un avertissement alarmant et faux à chaque démarrage.
    try:
        from kodo_core.db.sanctuary_shield import SanctuaryShield
        import database_manager
        _s_conn = database_manager.get_connection()
        try:
            _fp = SanctuaryShield.compute_sanctuary_fingerprint(_s_conn)
            print(f"🛡️ [SANCTUARY SHIELD] Stock & Magasin sanctuarisés : {_fp['products_count']} produits, {_fp['total_stock_units']} pièces en stock.")
        finally:
            _s_conn.close()
    except Exception as _se:
        pass
    # Synchronisation Shopify. Le moteur existait depuis toujours mais n'était démarré
    # QUE par main_app.py, l'ancienne interface Tkinter que le produit ne lance plus :
    # aucune vente ne décrémentait le stock de la boutique en ligne, aucune commande en
    # ligne ne décrémentait le stock de la caisse, et les deux interrupteurs de l'écran
    # Réglages ne commandaient rien. C'est ici, dans le serveur réellement lancé par
    # launch_app.py, que le branchement manquait.
    # start_auto_sync() ne démarre que si la boutique est configurée dans les Réglages
    # (domaine ET jeton) et qu'au moins un des deux sens est activé : une caisse sans
    # Shopify n'ouvre aucune connexion.
    try:
        from kodo_core.sync.shopify import start_auto_sync
        start_auto_sync()
    except Exception as _shop_err:
        print(f"⚠️ [SHOPIFY] Synchronisation non démarrée : {_shop_err}")

    print(f"🚀 [KODO POS SERVER Multi-Thread v2.0.1] REST API kodo_core & Web App en ligne sur http://localhost:{port} (écoute {host})")
    httpd.serve_forever()


if __name__ == '__main__':
    port = 8765
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port)
