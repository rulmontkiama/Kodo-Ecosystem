#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lanceur Principal Kōdo POS Next-Gen (macOS Native App)
Démarre le serveur API/Web Python et ouvre la fenêtre native WKWebView.
"""

import os
import sys
import time
import subprocess
import threading
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from server_pos import run_server

import urllib.request

PORT = 8765
URL = f"http://localhost:{PORT}"

def start_backend():
    """Démarre le serveur REST API Python et de ressources statiques."""
    run_server(port=PORT)

def wait_for_server(timeout=15):
    """Attend la réponse 200 OK du serveur local pour éviter tout écran blanc."""
    start_time = time.time()
    health_url = f"http://localhost:{PORT}/api/status"
    while time.time() - start_time < timeout:
        try:
            req = urllib.request.Request(health_url)
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    print("✅ [KODO POS] Serveur local prêt et opérationnel.")
                    return True
        except Exception:
            time.sleep(0.15)
    print("⚠️ [KODO POS] Timeout d'initialisation du serveur local. Tentative d'ouverture directe...")
    return False

def open_native_window():
    """Ouvre la fenêtre native macOS ou le moteur d'affichage."""
    wait_for_server(timeout=15)
    
    # 1. Tenter d'ouvrir avec pywebview si installé et fonctionnel
    try:
        import webview
        print("🖥️ [KODO POS] Ouverture dans une fenêtre macOS native (WKWebView)...")
        webview.create_window(
            title="Kōdo POS - Caisse Enregistreuse",
            url=URL,
            width=1280,
            height=800,
            min_size=(1024, 700),
            resizable=True,
            text_select=True,
            confirm_close=True
        )
        webview.start()
        return
    except Exception as e:
        print(f"⚠️ [KODO POS] pywebview non disponible ou en erreur ({e}). Bascule vers le navigateur natif macOS...")

    # 2. Sinon, ouvrir dans Safari ou le navigateur par défaut
    print("🌐 [KODO POS] Ouverture de l'interface Caisse dans Safari...")
    try:
        subprocess.run(["open", "-a", "Safari", URL])
    except Exception:
        webbrowser.open(URL)


if __name__ == '__main__':
    print("==================================================")
    print("    🚀 DÉMARRAGE DU LOGICIEL KŌDO POS NEXT-GEN")
    print("==================================================")
    
    # Lancer le serveur backend Python dans un thread séparé
    server_thread = threading.Thread(target=start_backend, daemon=True)
    server_thread.start()
    
    # Lancer l'interface visuelle
    open_native_window()
