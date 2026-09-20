# -*- coding: utf-8 -*-
"""Sécurité réseau de l'API locale (audit technique du 20/09/2026, point C1).

L'API n'a pas d'authentification : elle doit écouter sur 127.0.0.1, refuser un en-tête Host d'un autre
domaine (DNS rebinding) et les requêtes venues d'un autre site (CSRF), et ne plus répondre
« Access-Control-Allow-Origin: * ». Le vrai serveur tourne dans un processus jetable (HOME/base redirigés).
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

ORIGINE_PIEGE = "https://site-piege.example"


# --- Règles pures (sans serveur) -----------------------------------------------------------------

def _server_pos():
    import server_pos  # base initialisée dans le HOME jetable de conftest.py, comme les autres tests
    return server_pos


def test_hote_par_defaut_local(monkeypatch):
    from kodo_core.config import ShopConfig
    monkeypatch.delenv("KODO_HOST", raising=False)
    assert ShopConfig.get_host() == "127.0.0.1"
    monkeypatch.setenv("KODO_HOST", "0.0.0.0")
    assert ShopConfig.get_host() == "0.0.0.0"


@pytest.mark.parametrize("host", [None, "", "localhost:8765", "LOCALHOST:8765", "localhost",
                                  "127.0.0.1:8765", "[::1]:8765", "192.168.1.20:8765"])
def test_hote_local_ou_ip_accepte(host):
    assert _server_pos().hote_autorise(host)


@pytest.mark.parametrize("host", ["site-piege.example:8765", "localhost.site-piege.example:8765",
                                  "127.0.0.1.nip.io:8765", "caisse.local:8765"])
def test_nom_de_domaine_refuse(host):
    assert not _server_pos().hote_autorise(host)


@pytest.mark.parametrize("origin,host", [
    ("http://localhost:8765", "localhost:8765"),
    ("http://localhost:3000", "localhost:8765"),          # serveur Vite de développement
    ("http://127.0.0.1:8765", "127.0.0.1:8765"),
    ("http://[::1]:8765", "[::1]:8765"),
    ("http://192.168.1.20:8765", "192.168.1.20:8765"),    # même origine (écoute réseau via KODO_HOST)
])
def test_origine_locale_acceptee(origin, host):
    assert _server_pos().origine_autorisee(origin, host)


@pytest.mark.parametrize("origin,host", [
    (ORIGINE_PIEGE, "localhost:8765"),
    ("null", "localhost:8765"),
    ("", "localhost:8765"),
    ("http://localhost.site-piege.example", "localhost:8765"),
    ("http://192.168.1.20:8765", "localhost:8765"),
    ("file://", "localhost:8765"),
])
def test_origine_etrangere_refusee(origin, host):
    assert not _server_pos().origine_autorisee(origin, host)


# --- Serveur réel ----------------------------------------------------------------------------------

def _port_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _requete(port, methode, chemin, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(methode, chemin, skip_host=True, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        data = body.encode("utf-8") if isinstance(body, str) else body
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        rep = conn.getresponse()
        return rep.status, {k.lower(): v for k, v in rep.getheaders()}, rep.read()
    finally:
        conn.close()


def _ip_non_locale():
    """Adresse IPv4 de la machine sur le réseau (aucun paquet n'est envoyé), ou None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


@pytest.fixture(scope="module")
def port():
    port = _port_libre()
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, HOME=home, KODO_DB_PATH=os.path.join(home, "t.db"), PYTHONPATH=ROOT)
        env.pop("KODO_HOST", None)
        proc = subprocess.Popen([sys.executable, "-c", f"import server_pos; server_pos.run_server({port}, busy_wait=1)"],
                                env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            fin = time.monotonic() + 40
            while time.monotonic() < fin:
                try:
                    if _requete(port, "GET", "/api/status", {"Host": f"localhost:{port}"})[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                proc.kill()
                pytest.fail("le serveur n'a pas démarré : " + proc.communicate(timeout=10)[0][-1500:])
            yield port
        finally:
            proc.kill()
            proc.communicate(timeout=10)


def test_ecoute_uniquement_sur_la_boucle_locale(port):
    ip = _ip_non_locale()
    if ip is None:
        pytest.skip("aucune adresse réseau non locale sur cette machine")
    with pytest.raises(OSError):
        socket.create_connection((ip, port), timeout=2).close()


def test_appli_locale_acceptee(port):
    for host in (f"localhost:{port}", f"127.0.0.1:{port}"):
        statut, _, corps = _requete(port, "GET", "/api/status", {"Host": host})
        assert statut == 200, corps
    statut, _, _ = _requete(port, "POST", "/api/settings",
                            {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}",
                             "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"},
                            json.dumps({"defaultAlertThreshold": 4}))
    assert statut == 200


def test_dns_rebinding_refuse(port):
    for chemin in ("/api/clients", "/"):
        statut, _, _ = _requete(port, "GET", chemin, {"Host": f"site-piege.example:{port}"})
        assert statut == 403, chemin


def test_lecture_depuis_un_autre_site_refusee(port):
    statut, _, _ = _requete(port, "GET", "/api/clients",
                            {"Host": f"localhost:{port}", "Origin": ORIGINE_PIEGE, "Sec-Fetch-Site": "cross-site"})
    assert statut == 403


def test_ecriture_depuis_un_autre_site_refusee_et_sans_effet(port):
    # POST « simple » (text/plain) : le navigateur l'envoie sans preflight, CORS seul ne l'arrête pas.
    statut, _, _ = _requete(port, "POST", "/api/settings",
                            {"Host": f"localhost:{port}", "Origin": ORIGINE_PIEGE, "Content-Type": "text/plain"},
                            json.dumps({"storeName": "PIRATE"}))
    assert statut == 403
    statut, _, corps = _requete(port, "GET", "/api/settings", {"Host": f"localhost:{port}"})
    assert statut == 200 and json.loads(corps)["storeName"] != "PIRATE"


def test_plus_de_cors_joker(port):
    _, entetes, _ = _requete(port, "GET", "/api/status", {"Host": f"localhost:{port}", "Origin": ORIGINE_PIEGE})
    assert "access-control-allow-origin" not in entetes
    _, entetes, _ = _requete(port, "GET", "/api/status",
                             {"Host": f"localhost:{port}", "Origin": "http://localhost:3000"})
    assert entetes.get("access-control-allow-origin") == "http://localhost:3000"


def test_preflight_du_serveur_vite_accepte(port):
    statut, entetes, _ = _requete(port, "OPTIONS", "/api/settings",
                                  {"Host": f"localhost:{port}", "Origin": "http://localhost:3000",
                                   "Access-Control-Request-Method": "POST",
                                   "Access-Control-Request-Headers": "content-type"})
    assert statut == 200
    assert entetes.get("access-control-allow-origin") == "http://localhost:3000"
    assert "POST" in entetes.get("access-control-allow-methods", "")
