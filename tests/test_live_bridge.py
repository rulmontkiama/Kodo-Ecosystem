# -*- coding: utf-8 -*-
"""Tests du pont Live Shopping (export stock / import ventes par fichiers JSON)."""

import json
import os
import sqlite3
import sys
import tempfile
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.domain.live.live_bridge import LiveBridge, LiveBridgeError, normalize_phone


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    database_manager.DB_NAME = path
    os.environ["KODO_DB_PATH"] = path  # kodo_core.db.connection résout son propre chemin
    database_manager.initialiser_db()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac, taux_tva) VALUES ('TSHIRT-ROUGE', 'T-shirt rouge', 89.90, 0.21)")
    conn.execute("INSERT INTO Produits (code_barre, nom, prix_vente_tvac, taux_tva) VALUES (NULL, 'Casquette', 20.00, 0.21)")
    conn.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (1, 'M', 5)")   # stock 1
    conn.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (1, 'L', 1)")   # stock 2
    conn.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (2, 'Taille Unique', 3)")  # stock 3
    conn.commit()
    conn.close()
    yield path
    os.environ.pop("KODO_DB_PATH", None)
    os.close(fd)
    os.remove(path)


def q(path, sql, *args):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def vente(sku="TSHIRT-ROUGE-M", quantite=1, prix=89.90, tel="+33612345678", mode="mobile_money",
          recup="livraison", adresse="14 Avenue Montaigne, 75008, Paris", date="2026-09-20T14:12:00Z", **extra):
    v = {
        "sku": sku, "quantite": quantite, "prix_unitaire": prix,
        "client": {"prenom": "Camille", "nom": "Dubois", "telephone": tel, "email": None,
                   "mode_recuperation": recup, "adresse": adresse},
        "mode_paiement": mode, "reference_paiement": None, "statut": "payé", "date_commande": date,
    }
    v.update(extra)
    return v


def fichier(ventes, ref="live-2026-09-20-1400", exported="2026-09-20T17:30:00Z"):
    return json.dumps({"session_reference": ref, "exported_at": exported, "ventes": ventes}, ensure_ascii=False)


# --------------------------------------------------------------------------- export

def test_export_matches_contract_and_leaves_stock_untouched(db_path):
    res = LiveBridge.export_stock([{"stock_id": 1, "quantite": 3}, {"stock_id": 3, "quantite": 2}],
                                  session_reference="live-2026-09-20-1400", boutique="Ma Boutique")
    p = res["payload"]
    assert set(p) == {"session_reference", "exported_at", "boutique", "articles"}
    assert p["exported_at"].endswith("Z")
    assert p["articles"][0] == {"sku": "TSHIRT-ROUGE-M", "nom": "T-shirt rouge", "variante": "Taille M",
                                "prix": 89.9, "quantite_disponible": 3, "image_url": None}
    assert p["articles"][1]["sku"] == "P2" and p["articles"][1]["variante"] == "Taille Unique"
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks ORDER BY id") == [(5,), (1,), (3,)]


def test_export_rejects_quantity_above_real_stock(db_path):
    with pytest.raises(LiveBridgeError) as e:
        LiveBridge.export_stock([{"stock_id": 2, "quantite": 4}], session_reference="live-x-1")
    assert "1 en stock" in e.value.details[0]


def test_export_rejects_bad_selection_and_reference(db_path):
    with pytest.raises(LiveBridgeError):
        LiveBridge.export_stock([])
    with pytest.raises(LiveBridgeError):
        LiveBridge.export_stock([{"stock_id": 1, "quantite": 1}], session_reference="avec espace")
    with pytest.raises(LiveBridgeError):
        LiveBridge.export_stock([{"stock_id": 1, "quantite": 1}, {"stock_id": 1, "quantite": 1}], session_reference="live-x-1")


# --------------------------------------------------------------------------- import

def test_import_creates_ticket_client_stock_and_bordereau(db_path):
    LiveBridge.export_stock([{"stock_id": 1, "quantite": 5}], session_reference="live-2026-09-20-1400")
    content = fichier([vente(quantite=2)])

    prev = LiveBridge.preview_import(content)
    assert prev["summary"]["nb_importables"] == 1 and prev["summary"]["nb_livraisons"] == 1
    assert q(db_path, "SELECT COUNT(*) FROM Tickets") == [(0,)]  # l'aperçu n'écrit rien
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks WHERE id=1") == [(5,)]

    res = LiveBridge.apply_import(content)
    assert res["success"] and res["nb_importees"] == 1
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks WHERE id=1") == [(3,)]
    tickets = q(db_path, "SELECT total_tvac, methode_paiement, id_client FROM Tickets")
    assert Decimal(str(tickets[0][0])) == Decimal("179.80") and tickets[0][1] == "Mobile Money"
    assert q(db_path, "SELECT nom, telephone, adresse FROM Clients") == [
        ("Camille Dubois", "+33612345678", "14 Avenue Montaigne, 75008, Paris")]
    assert q(db_path, "SELECT COUNT(*) FROM Ledger_Caisse WHERE type_mouvement='VENTE'") == [(1,)]

    orders = LiveBridge.get_delivery_orders(import_id=res["import_id"])
    assert len(orders) == 1 and orders[0]["lignes"][0]["sku"] == "TSHIRT-ROUGE-M"
    status, body, headers = kodo_app.handle_request(
        "GET", "/api/live-bridge/bordereau", {"import_id": [str(res["import_id"])]}, {}, {})
    assert status == 200 and body[:5] == b"%PDF-" and headers["Content-Type"] == "application/pdf"


def test_same_file_cannot_be_imported_twice(db_path):
    content = fichier([vente()])
    assert LiveBridge.apply_import(content)["nb_importees"] == 1
    prev = LiveBridge.preview_import(content)
    assert prev["already_imported"] and prev["summary"]["nb_importables"] == 0
    again = LiveBridge.apply_import(content)
    assert again["nb_importees"] == 0 and again["nb_deja_importees"] == 1
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks WHERE id=1") == [(4,)]
    assert q(db_path, "SELECT COUNT(*) FROM Tickets") == [(1,)]


def test_later_full_export_only_imports_new_lines(db_path):
    first = vente(date="2026-09-20T14:12:00Z")
    LiveBridge.apply_import(fichier([first], exported="2026-09-20T15:00:00Z"))
    second = vente(tel="0698765432", date="2026-09-20T16:00:00Z")
    res = LiveBridge.apply_import(fichier([first, second], exported="2026-09-20T17:30:00Z"))
    assert res["nb_importees"] == 1 and res["nb_deja_importees"] == 1
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks WHERE id=1") == [(3,)]


def test_identical_lines_in_one_file_are_both_imported(db_path):
    res = LiveBridge.apply_import(fichier([vente(), vente()]))
    assert res["nb_importees"] == 2
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks WHERE id=1") == [(3,)]


def test_unknown_sku_and_oversell_are_blocked_never_negative(db_path):
    content = fichier([vente(sku="INCONNU"), vente(sku="TSHIRT-ROUGE-L", quantite=2), vente(sku="TSHIRT-ROUGE-M")])
    res = LiveBridge.apply_import(content)
    assert res["nb_importees"] == 1 and res["nb_bloquees"] == 2
    reasons = {l["sku"]: l["reasons"][0] for l in res["lines"] if l["status"] == "blocked"}
    assert "introuvable" in reasons["INCONNU"] and "Stock insuffisant" in reasons["TSHIRT-ROUGE-L"]
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks ORDER BY id") == [(4,), (1,), (3,)]


def test_cumulative_quantities_across_lines_cannot_oversell(db_path):
    res = LiveBridge.apply_import(fichier([vente(sku="TSHIRT-ROUGE-L"), vente(sku="TSHIRT-ROUGE-L", tel="0611111111")]))
    assert res["nb_importees"] == 1 and res["nb_bloquees"] == 1
    assert q(db_path, "SELECT quantite_actuelle FROM Stocks WHERE id=2") == [(0,)]


def test_blocked_line_can_be_reimported_after_restock(db_path):
    content = fichier([vente(sku="TSHIRT-ROUGE-L", quantite=2)])
    assert LiveBridge.apply_import(content)["nb_importees"] == 0
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE Stocks SET quantite_actuelle=5 WHERE id=2")
    conn.commit()
    conn.close()
    assert LiveBridge.apply_import(content)["nb_importees"] == 1


def test_export_mapping_survives_barcode_change(db_path):
    LiveBridge.export_stock([{"stock_id": 1, "quantite": 5}], session_reference="live-2026-09-20-1400")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE Produits SET code_barre='NOUVEAU' WHERE id=1")
    conn.commit()
    conn.close()
    assert LiveBridge.apply_import(fichier([vente()]))["nb_importees"] == 1


def test_phone_formats_match_same_client(db_path):
    assert normalize_phone("+33 6 12 34 56 78") == normalize_phone("06.12.34.56.78")
    LiveBridge.apply_import(fichier([vente(tel="+33612345678", date="2026-09-20T14:00:00Z")]))
    LiveBridge.apply_import(fichier([vente(tel="06 12 34 56 78", date="2026-09-20T15:00:00Z")], exported="2026-09-20T18:00:00Z"))
    assert q(db_path, "SELECT COUNT(*) FROM Clients") == [(1,)]
    total = q(db_path, "SELECT total_depense FROM Clients")[0][0]
    assert Decimal(str(total)) == Decimal("179.80")


def test_live_price_below_pos_price_is_kept_as_discount(db_path):
    res = LiveBridge.apply_import(fichier([vente(prix=79.90)]))
    assert res["nb_importees"] == 1
    prev = q(db_path, "SELECT total_tvac, remise FROM Tickets")[0]
    assert Decimal(str(prev[0])) == Decimal("79.90") and Decimal(str(prev[1])) == Decimal("10.00")


def test_unpaid_sales_are_ignored_and_missing_payment_uses_default(db_path):
    content = fichier([vente(statut="en_attente_paiement"), vente(mode=None, recup="retrait", adresse=None)])
    prev = LiveBridge.preview_import(content, default_payment="carte")
    assert prev["summary"]["nb_ignorees"] == 1 and prev["summary"]["nb_sans_mode_paiement"] == 1
    LiveBridge.apply_import(content, default_payment="carte")
    assert q(db_path, "SELECT methode_paiement FROM Tickets") == [("CB",)]


def test_quick_client_without_phone_is_accepted(db_path):
    v = vente(tel=None)
    v["client"]["nom"] = None
    v["client"]["prenom"] = "Léa"
    assert LiveBridge.apply_import(fichier([v]))["nb_importees"] == 1
    assert q(db_path, "SELECT nom FROM Clients") == [("Léa",)]


# --------------------------------------------------------------------------- fichiers invalides

@pytest.mark.parametrize("content,expected", [
    ("", "vide"),
    ("{pas du json", "JSON invalide"),
    ("[1, 2]", "objet JSON"),
    (json.dumps({"session_reference": "s", "exported_at": "2026-09-18T14:00:00Z", "articles": []}), "export de STOCK"),
    (json.dumps({"exported_at": "hier", "ventes": "x"}), "invalide"),
])
def test_invalid_files_give_clear_errors(db_path, content, expected):
    with pytest.raises(LiveBridgeError) as e:
        LiveBridge.preview_import(content)
    assert expected in " ".join([str(e.value)] + e.value.details)


def test_bad_lines_are_blocked_individually_without_crashing(db_path):
    bad = [None, {"sku": "X"}, vente(quantite=0), vente(quantite="2"), vente(prix="abc"), vente(mode="bitcoin"),
           {**vente(), "client": "Camille"}]
    prev = LiveBridge.preview_import(fichier(bad + [vente()]))
    assert prev["summary"]["nb_bloquees"] == len(bad) and prev["summary"]["nb_importables"] == 1


def test_route_returns_400_with_message_on_bad_file(db_path):
    status, body, _ = kodo_app.handle_request(
        "POST", "/api/live-bridge/import/preview", {}, {}, {"content": "{oups"})
    assert status == 400 and "JSON invalide" in body["error"]
