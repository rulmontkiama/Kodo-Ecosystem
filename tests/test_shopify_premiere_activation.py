# -*- coding: utf-8 -*-
"""
Kōdo POS — Activer la synchro Shopify ne repousse pas l'historique des ventes.

Le défaut corrigé : tout ticket naît avec `synced_shopify = 0`. Le jour où la commerçante
branche sa boutique, la première passe poussait donc TOUTES les ventes passées vers Shopify —
alors que le stock en ligne (importé par le CSV de Kōdo, ou copié depuis Shopify) en tenait
déjà compte. Chaque pièce vendue un jour en caisse était décomptée une seconde fois en ligne.

Règle : seules les ventes postérieures au dernier alignement des deux stocks sont poussées.
L'alignement est l'export CSV (ou l'import du catalogue Shopify) ; à défaut, l'activation.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from export_manager import export_shopify_catalog_csv
from kodo_core.sync import shopify as shopify_sync
from tests.test_shopify_sku_taille import FausseBoutique
from tests.test_shopify_sync import CATALOGUE_FAUSSE_BOUTIQUE, BaseTemporaire, MoteurBouchonne


class TestPremiereActivation(BaseTemporaire):

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        pid = self.creer_produit("ROBE", "Robe", [("Unique", 3)])
        self.sid = self.rows("SELECT id FROM Stocks WHERE id_produit = ?", (pid,))[0][0]
        self.boutique = FausseBoutique([{"sku": "ROBE", "barcode": "ROBE", "item": 42}])

    def exporter(self):
        fd, chemin = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            export_shopify_catalog_csv(output_path=chemin)
        finally:
            os.remove(chemin)

    def activer(self):
        """Ce que fait `start_auto_sync` au branchement, sans lancer de thread réseau."""
        shopify_sync.fixer_seuil_tickets()

    def test_les_ventes_d_avant_l_activation_ne_sont_pas_repoussees(self):
        self.creer_ticket("T-HIER", [(self.sid, 1)])
        self.activer()
        self.creer_ticket("T-APRES", [(self.sid, 1)])

        self.boutique.moteur().sync_tickets_to_shopify()

        self.assertEqual(self.boutique.ajustements, [(42, -1)],
                         "la vente d'hier, déjà comprise dans le stock en ligne, a été décomptée à nouveau")

    def test_les_ventes_entre_l_export_et_l_activation_sont_poussees(self):
        """Le CSV fige le stock au moment de l'export : ce qui est vendu ensuite doit suivre."""
        self.creer_ticket("T-AVANT-EXPORT", [(self.sid, 1)])
        self.exporter()
        self.creer_ticket("T-APRES-EXPORT", [(self.sid, 1)])
        self.activer()

        self.boutique.moteur().sync_tickets_to_shopify()

        self.assertEqual(self.boutique.ajustements, [(42, -1)])

    def test_une_synchro_deja_en_service_ne_perd_aucun_ticket_en_attente(self):
        """Installation qui poussait déjà : un ticket resté en attente (coupure) part toujours."""
        self.creer_ticket("T-DEJA", [(self.sid, 1)])
        self.boutique.moteur().sync_tickets_to_shopify()
        self.creer_ticket("T-EN-ATTENTE", [(self.sid, 1)])
        self.ecrire("DELETE FROM Parametres WHERE cle = ?", (shopify_sync.PARAM_SEUIL_TICKETS,))

        self.activer()
        self.boutique.moteur().sync_tickets_to_shopify()

        self.assertEqual(self.boutique.ajustements, [(42, -1), (42, -1)])

    def test_le_seuil_n_avance_pas_a_chaque_demarrage(self):
        """Relancer l'app ne doit pas faire sauter les ventes pas encore poussées."""
        self.activer()
        self.creer_ticket("T-HORS-LIGNE", [(self.sid, 1)])
        self.activer()                       # redémarrage de la caisse
        self.boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(self.boutique.ajustements, [(42, -1)])

    def test_l_import_du_catalogue_shopify_est_un_alignement(self):
        """Kōdo copie le stock en ligne : les ventes d'avant l'import y sont déjà comptées."""
        self.creer_ticket("T-AVANT-IMPORT", [(self.sid, 1)])
        pages = iter([{"products": [CATALOGUE_FAUSSE_BOUTIQUE]}, {"products": []}])
        moteur = MoteurBouchonne({"products.json": lambda e, d: next(pages)},
                                 store_url="boutique.myshopify.com", access_token="jeton")
        self.assertEqual(moteur.import_catalog(), 2)
        # L'import a décliné l'article local en S/M : en ligne, la ligne vendue est la taille S.
        self.boutique = FausseBoutique([{"sku": "ROBE-S", "barcode": "ROBE-S", "item": 42}])
        self.creer_ticket("T-APRES-IMPORT", [(self.sid, 1)])
        self.activer()

        self.boutique.moteur().sync_tickets_to_shopify()

        self.assertEqual(self.boutique.ajustements, [(42, -1)])

    def test_le_demarrage_automatique_fixe_le_seuil(self):
        self.creer_ticket("T-HIER", [(self.sid, 1)])
        lances = []

        class FilMuet:
            def __init__(self, *a, **k):
                pass

            def start(self):
                lances.append(True)

            def is_alive(self):
                return bool(lances)

        original = shopify_sync.ShopifySyncThread
        shopify_sync.ShopifySyncThread = FilMuet
        try:
            shopify_sync.start_auto_sync(force=True)
        finally:
            shopify_sync.ShopifySyncThread = original
            shopify_sync._thread_auto = None

        self.assertEqual(lances, [True])
        self.boutique.moteur().sync_tickets_to_shopify()
        self.assertEqual(self.boutique.ajustements, [])


if __name__ == "__main__":
    unittest.main()
