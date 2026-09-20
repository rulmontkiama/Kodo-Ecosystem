# -*- coding: utf-8 -*-
"""Démarrage du serveur quand le port est déjà pris.

Avant : un second lancement exécutait `lsof | xargs kill -9` sur l'instance EN COURS D'UTILISATION, puis plantait
(`name 'time' is not defined`) : plus aucun serveur. Ports éphémères, processus jetables, HOME/base redirigés.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KODO_FACTICE = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"status": "online", "app": "Kōdo POS Engine"}, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
'''

PROCESSUS_FIGE = r'''
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", int(sys.argv[1]))); s.listen(5)
time.sleep(300)
'''


def port_libre():
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def attendre(condition, delai=20.0):
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.2)
    return False


def repond(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def lancer_run_server(port, home):
    env = dict(os.environ, HOME=home, KODO_DB_PATH=os.path.join(home, "t.db"), PYTHONPATH=ROOT)
    code = f"import server_pos; server_pos.run_server({port}, busy_wait=1)"
    return subprocess.Popen([sys.executable, "-c", code], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def test_une_instance_kodo_vivante_nest_pas_tuee_et_est_reutilisee():
    port = port_libre()
    with tempfile.TemporaryDirectory() as home:
        premiere = subprocess.Popen([sys.executable, "-c", KODO_FACTICE, str(port)])
        try:
            assert attendre(lambda: repond(port)), "l'instance factice doit répondre"
            seconde = lancer_run_server(port, home)
            sortie, _ = seconde.communicate(timeout=60)

            assert seconde.returncode == 0, sortie[-1500:]
            assert "réutilisée" in sortie, sortie[-1500:]
            assert "is not defined" not in sortie
            assert premiere.poll() is None, "l'instance en cours d'utilisation ne doit pas être tuée"
            assert repond(port)
        finally:
            premiere.kill()


def test_un_processus_fige_est_libere_puis_le_serveur_demarre():
    port = port_libre()
    with tempfile.TemporaryDirectory() as home:
        fige = subprocess.Popen([sys.executable, "-c", PROCESSUS_FIGE, str(port)])
        serveur = None
        try:
            time.sleep(1.0)
            assert not repond(port), "le processus figé ne répond pas"
            serveur = lancer_run_server(port, home)
            assert attendre(lambda: repond(port), delai=40), "le serveur doit prendre la place du processus figé"
            assert fige.poll() is not None, "le processus figé doit avoir été arrêté"
        finally:
            fige.kill()
            if serveur is not None:
                serveur.kill()
                serveur.communicate(timeout=10)


def test_port_libre_demarre_normalement():
    port = port_libre()
    with tempfile.TemporaryDirectory() as home:
        serveur = lancer_run_server(port, home)
        try:
            assert attendre(lambda: repond(port), delai=40)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
                assert json.loads(r.read())["app"].startswith("Kōdo POS")
        finally:
            serveur.kill()
            serveur.communicate(timeout=10)
