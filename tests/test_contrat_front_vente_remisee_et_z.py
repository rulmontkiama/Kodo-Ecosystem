# -*- coding: utf-8 -*-
"""
Contrat écran ↔ serveur pour deux défauts d'ARGENT trouvés par la revue du 2026-10-14.

1. Vente remisée. Le serveur recalcule le dû depuis les prix du catalogue (jamais le total annoncé par
   l'écran) : sans `discountPercent`, il exige le prix PLEIN et refuse « Paiement insuffisant » une vente
   dont l'écran a déjà appliqué la remise. L'écran n'envoyait pas ce champ. Ces tests figent le contrat que
   l'écran doit respecter (src/App.tsx `handleCompleteSale`).

2. Z sans comptage physique. L'écran envoyait le fond du MATIN comme « montant compté » quand la
   commerçante validait la clôture sans compter : l'écart scellé valait −(espèces du jour). Il doit envoyer
   `fondCaisseReel: null` ; le serveur n'invente alors aucun écart.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager  # noqa: E402
from database_manager import initialiser_db, get_connection  # noqa: E402
from kodo_core.api.routes.pos_routes import handle_pos_request  # noqa: E402


class TestVenteRemiseeEtZ(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "contrat.db")
        self._origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        initialiser_db()
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Produits (nom, prix_vente_tvac, taux_tva) VALUES ('Robe', 100.00, 0.21)")
            self.pid = c.lastrowid
            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 20)", (self.pid,))
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        database_manager.DB_NAME = self._origine
        os.environ.pop("KODO_DB_PATH", None)
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass

    def vendre(self, total, mode="CB", **extra):
        corps = {
            "items": [{"product": {"id": self.pid, "name": "Robe", "price": 100.0, "vat_rate": 0.21},
                       "quantity": 1, "selectedSize": "M", "size": "M"}],
            "totalTTC": total, "paymentMethod": mode, "cashierName": "Admin", "printReceipt": False,
        }
        corps.update(extra)
        return handle_pos_request("POST", "/api/sales", {}, corps)

    # -- 1. vente remisée --------------------------------------------------------------------
    def test_vente_remisee_avec_le_pourcentage_est_acceptee(self):
        code, rep = self.vendre(90.0, discountPercent=10)
        self.assertEqual(code, 200, rep)
        self.assertTrue(rep["success"])
        self.assertEqual(float(rep["ticket"]["total_tvac"]), 90.0)

    def test_vente_remisee_sans_le_pourcentage_est_refusee_comme_le_faisait_l_ancien_ecran(self):
        # Documente la cause : un total annoncé de 90 € sans pourcentage ne couvre pas le dû serveur (100 €).
        code, rep = self.vendre(90.0)
        self.assertEqual(code, 400)
        self.assertIn("Paiement insuffisant", rep["error"])

    def test_remise_en_especes_avec_arrondi_belge(self):
        # 100 × (1 − 12,5 %) = 87,50 : multiple de 5 c, accepté en espèces avec le pourcentage.
        code, rep = self.vendre(87.5, mode="Espèces", discountPercent=12.5, changeGiven=0)
        self.assertEqual(code, 200, rep)

    # -- 2. Z sans comptage ------------------------------------------------------------------
    def _vente_especes_du_jour(self):
        code, rep = self.vendre(100.0, mode="Espèces")
        self.assertEqual(code, 200, rep)

    def test_z_sans_comptage_ne_scelle_aucun_ecart(self):
        self._vente_especes_du_jour()
        code, rep = handle_pos_request("POST", "/api/cloture-z", {}, {
            "fondCaisseReel": None, "fondCaisseMatin": 50.0, "vendeur": "Admin"})
        self.assertEqual(code, 200, rep)
        self.assertEqual(float(rep["cloture"]["ecart"]), 0.0)

    def test_z_avec_le_fond_du_matin_comme_compte_scellerait_un_ecart_faux(self):
        # Ce que l'ancien écran envoyait quand rien n'avait été compté : le défaut que la correction évite.
        self._vente_especes_du_jour()
        code, rep = handle_pos_request("POST", "/api/cloture-z", {}, {
            "fondCaisseReel": 50.0, "fondCaisseMatin": 50.0, "vendeur": "Admin"})
        self.assertEqual(code, 200, rep)
        self.assertEqual(float(rep["cloture"]["ecart"]), -100.0)

    def test_z_avec_un_vrai_comptage_calcule_l_ecart(self):
        self._vente_especes_du_jour()
        code, rep = handle_pos_request("POST", "/api/cloture-z", {}, {
            "fondCaisseReel": 148.0, "fondCaisseMatin": 50.0, "vendeur": "Admin"})
        self.assertEqual(code, 200, rep)
        self.assertEqual(float(rep["cloture"]["ecart"]), -2.0)


if __name__ == "__main__":
    unittest.main()
