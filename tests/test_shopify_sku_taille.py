# -*- coding: utf-8 -*-
"""
Kōdo POS — Chaque taille vendue en caisse décrémente SA variante Shopify, et réciproquement.

Le défaut corrigé : l'export CSV du catalogue écrit un SKU par taille (`BI038-30`, `BI038-32`…)
et ne pose le code-barres que sur la première taille, mais la synchro cherchait la variante
Shopify avec le code-barres du PRODUIT. Vendre un 32 en caisse retirait donc la pièce du 30 en
ligne. Ces tests montent le catalogue Shopify à partir du VRAI fichier d'export, puis vendent
chaque taille en caisse.
"""
import csv
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from export_manager import export_shopify_catalog_csv
from kodo_core.sync.shopify_sku import prefixes_possibles, skus_produit
from tests.test_shopify_sync import REPONSE_LOCATIONS, BaseTemporaire, MoteurBouchonne


def terme_recherche(data):
    """Le SKU/code-barres cherché dans une requête GraphQL `sku:"X" OR barcode:"X"`."""
    requete = (data or {}).get("variables", {}).get("query", "")
    m = re.match(r'sku:("(?:[^"\\]|\\.)*") OR barcode:', requete)
    return json.loads(m.group(1)) if m else None


class FausseBoutique:
    """Catalogue Shopify en mémoire : variantes `{sku, barcode, item}` et journal des ajustements."""

    def __init__(self, variantes, recherche_approchee=False):
        self.variantes = variantes
        self.ajustements = []
        # Le moteur de recherche Shopify n'est pas une égalité stricte : il peut renvoyer une
        # variante voisine (« BI038-3 » → BI038-30). On le simule pour prouver qu'on vérifie.
        self.recherche_approchee = recherche_approchee

    def graphql(self, endpoint, data):
        terme = terme_recherche(data)
        if self.recherche_approchee:
            trouvees = [v for v in self.variantes
                        if terme and (str(v["sku"]).startswith(terme) or str(v["barcode"]).startswith(terme))]
        else:
            trouvees = [v for v in self.variantes if terme and terme in (v["sku"], v["barcode"])]
        return {"data": {"productVariants": {"edges": [
            {"node": {"sku": v["sku"], "barcode": v["barcode"] or None,
                      "inventoryItem": {"id": f"gid://shopify/InventoryItem/{v['item']}"}}}
            for v in trouvees
        ]}}}

    def adjust(self, endpoint, data):
        self.ajustements.append((data["inventory_item_id"], data["available_adjustment"]))
        return {"inventory_level": {"available": 0}}

    def moteur(self):
        return MoteurBouchonne({
            "locations.json": REPONSE_LOCATIONS,
            "graphql.json": self.graphql,
            "inventory_levels/adjust.json": self.adjust,
        }, store_url="boutique.myshopify.com", access_token="jeton")


class TestReglePartagee(unittest.TestCase):

    def test_sku_par_taille(self):
        self.assertEqual(skus_produit("BI038", 7, ["30", "31", "32"]), ["BI038-30", "BI038-31", "BI038-32"])
        self.assertEqual(skus_produit("BI038", 7, ["Taille Unique"]), ["BI038"])
        self.assertEqual(skus_produit(None, 7, ["S", "M"]), ["KODO-7-S", "KODO-7-M"])
        self.assertEqual(skus_produit("", 7, ["Unique"]), ["KODO-7"])
        self.assertEqual(skus_produit("BI038", 7, ["½", "M L"]), ["BI038-V1", "BI038-ML"])

    def test_prefixes(self):
        self.assertEqual(prefixes_possibles("BI038-XS-S"), ["BI038-XS", "BI038"])
        self.assertEqual(prefixes_possibles("BI038"), [])


class TestExportPuisVenteEnCaisse(BaseTemporaire):

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.pid = self.creer_produit("BI038", "Jean BI038", [("30", 2), ("31", 1), ("32", 1)])
        self.stocks = dict((t, i) for i, t in self.rows(
            "SELECT id, taille FROM Stocks WHERE id_produit = ?", (self.pid,)))

    def boutique_depuis_export(self, **kw):
        """Importe dans la fausse boutique le CSV réellement produit par l'export Kōdo."""
        fd, chemin = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            export_shopify_catalog_csv(output_path=chemin)
            with open(chemin, encoding="utf-8-sig", newline="") as f:
                lignes = list(csv.DictReader(f))
        finally:
            os.remove(chemin)
        variantes = [{"sku": l["Variant SKU"], "barcode": l["Variant Barcode"], "item": 5000 + i}
                     for i, l in enumerate(lignes)]
        return FausseBoutique(variantes, **kw), {v["sku"]: v["item"] for v in variantes}

    def test_l_export_ne_pose_le_code_barres_que_sur_la_premiere_taille(self):
        """Le constat de départ (inchangé : Shopify refuse deux variantes au même code-barres)."""
        boutique, _ = self.boutique_depuis_export()
        self.assertEqual([(v["sku"], v["barcode"]) for v in boutique.variantes],
                         [("BI038-30", "BI038"), ("BI038-31", ""), ("BI038-32", "")])

    def test_chaque_taille_vendue_decremente_sa_propre_variante(self):
        boutique, items = self.boutique_depuis_export()
        self.creer_ticket("T-32", [(self.stocks["32"], 1)])
        self.creer_ticket("T-31", [(self.stocks["31"], 1)])
        self.creer_ticket("T-30", [(self.stocks["30"], 1)])

        self.assertEqual(boutique.moteur().sync_tickets_to_shopify(), 3)
        self.assertEqual(sorted(boutique.ajustements), sorted([
            (items["BI038-32"], -1), (items["BI038-31"], -1), (items["BI038-30"], -1)]),
            "une vente de 32 ou de 31 a été retirée d'une autre taille en ligne")

    def test_une_recherche_approchee_ne_designe_jamais_une_autre_taille(self):
        """Si Shopify renvoie une variante voisine, on ne la décrémente pas à la place."""
        boutique, items = self.boutique_depuis_export(recherche_approchee=True)
        self.creer_ticket("T-32", [(self.stocks["32"], 1)])
        boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(boutique.ajustements, [(items["BI038-32"], -1)])

    def test_taille_absente_en_ligne_rien_n_est_retire_d_une_autre(self):
        """Un produit décliné dont la taille n'existe pas en ligne : tracé, jamais reporté sur le 30."""
        boutique = FausseBoutique([{"sku": "BI038-30", "barcode": "BI038", "item": 5000}])
        self.creer_ticket("T-32", [(self.stocks["32"], 1)])
        boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(boutique.ajustements, [])
        self.assertEqual(self.rows("SELECT statut FROM Shopify_Sync_Lignes"), [("ABSENT_SHOPIFY",)])

    def test_un_produit_sans_code_barres_est_suivi_par_son_sku_kodo(self):
        pid = self.creer_produit(None, "Top sans code", [("S", 1), ("M", 1)])
        sid_m = self.rows("SELECT id FROM Stocks WHERE id_produit = ? AND taille = 'M'", (pid,))[0][0]
        boutique, items = self.boutique_depuis_export()
        self.creer_ticket("T-M", [(sid_m, 1)])
        boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(boutique.ajustements, [(items[f"KODO-{pid}-M"], -1)])

    def test_un_produit_importe_depuis_shopify_reste_suivi(self):
        """Bases alimentées par l'import Shopify : un produit par variante, code = SKU de la variante."""
        pid = self.creer_produit("ROBE-M", "Robe", [("M", 3)])
        sid = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]
        boutique = FausseBoutique([{"sku": "ROBE-S", "barcode": "", "item": 1},
                                   {"sku": "ROBE-M", "barcode": "", "item": 2}])
        self.creer_ticket("T-R", [(sid, 1)])
        boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(boutique.ajustements, [(2, -1)])


class TestCommandeEnLigneDepuisUnCatalogueExporte(BaseTemporaire):

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.pid = self.creer_produit("BI038", "Jean BI038", [("30", 2), ("XS-S", 1), ("32", 1)])

    def stock(self):
        return dict(self.rows("SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ?", (self.pid,)))

    def importer(self, ligne):
        commande = {"id": 9101, "order_number": 2001, "total_price": "50.00", "total_tax": "8.68",
                    "taxes_included": True, "line_items": [dict(ligne, quantity=1, price="50.00")]}
        moteur = MoteurBouchonne({"orders.json": {"orders": [commande]}},
                                 store_url="boutique.myshopify.com", access_token="jeton")
        self.assertEqual(moteur.sync_orders_from_shopify(), 1)

    def test_le_sku_de_taille_retrouve_la_bonne_ligne_meme_si_le_titre_a_change(self):
        """Titre renommé en ligne, pas de code-barres sur la variante : seul le SKU relie les deux."""
        self.importer({"sku": "BI038-32", "barcode": None, "title": "Jean droit brut", "variant_title": "32"})
        self.assertEqual(self.stock(), {"30": 2, "XS-S": 1, "32": 0})

    def test_une_taille_contenant_un_tiret(self):
        self.importer({"sku": "BI038-XS-S", "barcode": None, "title": "Autre titre", "variant_title": "XS-S"})
        self.assertEqual(self.stock(), {"30": 2, "XS-S": 0, "32": 1})


if __name__ == "__main__":
    unittest.main()
