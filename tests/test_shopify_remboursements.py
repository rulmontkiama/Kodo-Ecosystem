# -*- coding: utf-8 -*-
"""
Kōdo POS — Un remboursement fait EN LIGNE revient jusque dans la caisse.

Jusqu'ici la caisse ne regardait une commande Shopify qu'une seule fois, au moment de
l'importer. Tout ce qui lui arrivait ENSUITE — remboursement total, remboursement partiel,
annulation — lui restait invisible, pour une raison très concrète : la liste des commandes
est filtrée sur `financial_status=paid` et `fulfillment_status=unfulfilled`, et une commande
remboursée quitte justement ces deux états. La vente continuait donc de peser de tout son
poids dans le rapport Z et dans la TVA déclarée, et l'article remboursé ne revenait jamais
en rayon.

Ce fichier démontre le nouveau chemin, sans aucune boutique Shopify réelle : tous les
échanges HTTP sont remplacés par des réponses écrites dans le test.

La règle de prudence qui structure l'ensemble : **seul un remboursement déclaré par Shopify
crée une écriture financière.** Rien n'est jamais inventé — ni un remboursement pour une
commande annulée sans remboursement, ni une unité rendue que la caisse n'a pas vendue.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.sync import shopify as shopify_sync
from test_shopify_sync import BaseTemporaire, MoteurBouchonne


class BaseRemboursements(BaseTemporaire):
    """Une commande importée, puis la même commande revue plus tard avec ses remboursements."""

    SKU = "CB-ROBE"
    VARIANT_ID = 555

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")
        self.pid = self.creer_produit(self.SKU, "Robe", [("S", 4), ("M", 6), ("L", 2)])
        self.stocks = dict((t, i) for i, t in self.rows(
            "SELECT id, taille FROM Stocks WHERE id_produit = ?", (self.pid,)))

    # --- fabriques ---------------------------------------------------------------------------

    def commande(self, quantite=2, **surcharges):
        base = {
            "id": 9001, "order_number": 1042,
            "total_price": "60.00", "total_tax": "10.41", "taxes_included": True,
            "updated_at": "2026-02-01T10:00:00+01:00",
            "line_items": [{
                "id": 4001, "sku": self.SKU, "title": "Robe", "quantity": quantite,
                "variant_id": self.VARIANT_ID, "variant_title": "M", "price": "30.00",
            }],
        }
        base.update(surcharges)
        return base

    def remboursement(self, quantite=1, restock_type="return", refund_id=7001, **surcharges):
        ligne = {
            "id": 8001, "quantity": quantite, "restock_type": restock_type,
            "line_item": {
                "id": 4001, "sku": self.SKU, "title": "Robe",
                "variant_id": self.VARIANT_ID, "variant_title": "M",
            },
        }
        ligne.update(surcharges.pop("ligne", {}))
        remb = {"id": refund_id, "created_at": "2026-02-02T09:00:00+01:00",
                "refund_line_items": [ligne]}
        remb.update(surcharges)
        return remb

    def moteur(self, a_importer=None, apres=None):
        """
        Un seul point d'entrée `orders.json`, deux réponses : Shopify distingue les deux
        passes par ses paramètres, et le bouchon fait exactement pareil.
        """
        def routeur(endpoint, data):
            if "updated_at_min" in endpoint:
                return {"orders": list(apres or [])}
            return {"orders": list(a_importer or [])}

        return MoteurBouchonne({"orders.json": routeur},
                               store_url="boutique.myshopify.com", access_token="jeton")

    # --- lectures ----------------------------------------------------------------------------

    def tailles(self):
        return dict(self.rows(
            "SELECT taille, quantite_actuelle FROM Stocks WHERE id_produit = ?", (self.pid,)))

    def tickets_de_remboursement(self):
        return self.rows("SELECT numero_ticket, total_tvac FROM Tickets "
                         "WHERE total_tvac < 0 ORDER BY id")

    def importer_puis_rembourser(self, commande, remboursements, passes=1):
        moteur = self.moteur(a_importer=[commande])
        self.assertEqual(moteur.sync_orders_from_shopify(), 1, "la commande doit d'abord s'importer")
        apres = dict(commande)
        apres["refunds"] = remboursements
        apres["updated_at"] = "2026-02-02T09:00:00+01:00"
        moteur = self.moteur(apres=[apres])
        return [moteur.sync_refunds_from_shopify() for _ in range(passes)], moteur


# =============================================================================================
# 1. Le remboursement arrive jusqu'à la caisse
# =============================================================================================

class TestRemboursementRapatrie(BaseRemboursements):

    def test_un_remboursement_en_ligne_cree_un_ticket_negatif_et_remet_l_article_en_rayon(self):
        """Le cas nominal : la cliente renvoie une robe, la caisse doit le savoir."""
        (traites,), _ = self.importer_puis_rembourser(self.commande(), [self.remboursement()])

        self.assertEqual(traites, 1)
        self.assertEqual(self.tailles()["M"], 5, "l'article rendu doit revenir en rayon (6 - 2 + 1)")
        tickets = self.tickets_de_remboursement()
        self.assertEqual(len(tickets), 1, "un et un seul ticket de remboursement")
        self.assertEqual(tickets[0][1], Decimal("-30.00"),
                         "le montant est relu sur la ligne de vente d'origine, pas fourni par Shopify")

    def test_le_remboursement_est_rattache_a_la_vente_d_origine(self):
        """Sans `refund_of_vd_id`, le garde-fou anti-double-remboursement perd sa trace."""
        self.importer_puis_rembourser(self.commande(), [self.remboursement()])
        liens = self.rows("SELECT quantite, refund_of_vd_id FROM Ventes_Details "
                          "WHERE refund_of_vd_id IS NOT NULL")
        self.assertEqual(len(liens), 1)
        self.assertEqual(liens[0][0], -1, "la ligne de remboursement porte une quantité négative")

    def test_le_ticket_de_remboursement_est_signe_dans_la_chaine_fiscale(self):
        self.importer_puis_rembourser(self.commande(), [self.remboursement()])
        signature, precedent = self.rows(
            "SELECT signature, hash_precedent FROM Tickets WHERE total_tvac < 0")[0]
        self.assertTrue(signature, "un ticket non signé casse la chaîne NF525")
        self.assertTrue(precedent)
        # Le maillon précédent est bien le ticket importé juste avant.
        attendu = self.rows("SELECT signature FROM Tickets WHERE total_tvac > 0 ORDER BY id DESC LIMIT 1")[0][0]
        self.assertEqual(precedent, attendu)

    def test_un_remboursement_partiel_ne_rend_que_ce_qui_a_ete_rendu(self):
        (traites,), _ = self.importer_puis_rembourser(
            self.commande(quantite=3), [self.remboursement(quantite=1)])
        self.assertEqual(traites, 1)
        self.assertEqual(self.tailles()["M"], 4, "6 vendus -3, rendu +1")
        self.assertEqual(self.tickets_de_remboursement()[0][1], Decimal("-30.00"))

    def test_le_ledger_de_caisse_enregistre_le_mouvement(self):
        self.importer_puis_rembourser(self.commande(), [self.remboursement()])
        mouvements = self.rows("SELECT type_mouvement, methode_paiement FROM Ledger_Caisse "
                               "WHERE type_mouvement = 'REMBOURSEMENT'")
        self.assertEqual(len(mouvements), 1)
        self.assertEqual(mouvements[0][1], "Shopify",
                         "le remboursement doit se ranger comme la vente qu'il annule")


# =============================================================================================
# 2. Rien ne se répète, rien ne s'invente
# =============================================================================================

class TestPrudence(BaseRemboursements):

    CLE_REPERE = shopify_sync.CLE_REMB_DEPUIS

    def test_le_meme_remboursement_n_est_jamais_enregistre_deux_fois(self):
        """Le cœur du sujet : une passe toutes les minutes ne doit pas rembourser en boucle."""
        traites, _ = self.importer_puis_rembourser(
            self.commande(), [self.remboursement()], passes=3)
        self.assertEqual(traites, [1, 0, 0])
        self.assertEqual(len(self.tickets_de_remboursement()), 1)
        self.assertEqual(self.tailles()["M"], 5, "le stock ne doit pas remonter à chaque passe")

    def test_les_passes_suivantes_ecartent_le_remboursement_sans_la_moindre_erreur(self):
        """
        Ne pas rembourser deux fois ne suffit PAS. Sans garde explicite, la seconde passe
        retenterait l'écriture, se heurterait à la clé primaire du journal, et l'échec
        remonterait : le repère de fenêtre cesserait d'avancer et la boutique enchaînerait
        les erreurs en boucle — alors que tout est en ordre. Le remboursement déjà traité
        doit être ÉCARTÉ, pas retenté.
        """
        commande = self.commande()
        moteur = self.moteur(a_importer=[commande])
        moteur.sync_orders_from_shopify()
        apres = dict(commande, refunds=[self.remboursement()],
                     updated_at="2026-02-02T09:00:00+01:00")

        self.moteur(apres=[apres]).sync_refunds_from_shopify()
        with self.assertNoLogs("kodo_core.sync.shopify", level="ERROR"):
            self.assertEqual(self.moteur(apres=[apres]).sync_refunds_from_shopify(), 0)

        self.assertEqual(self.rows("SELECT COUNT(*) FROM Shopify_Remboursements"), [(1,)])
        self.assertEqual(
            self.rows("SELECT COUNT(*) FROM Parametres WHERE cle = ?", (self.CLE_REPERE,)), [(1,)],
            "une passe propre doit faire avancer le repère, pas le bloquer")

    def test_no_restock_rembourse_l_argent_sans_remettre_l_article_en_rayon(self):
        """
        `restock_type: no_restock` : la cliente est remboursée mais l'article ne revient pas au
        stock vendable. Le remettre en rayon créerait une unité fantôme, ensuite poussée vers
        Shopify et vendue une seconde fois.
        """
        (traites,), _ = self.importer_puis_rembourser(
            self.commande(), [self.remboursement(restock_type="no_restock")])
        self.assertEqual(traites, 1)
        self.assertEqual(len(self.tickets_de_remboursement()), 1, "l'argent est bien rendu")
        self.assertEqual(self.tailles()["M"], 4, "mais l'article ne revient PAS en rayon")

    def test_shopify_ne_peut_pas_rembourser_plus_que_ce_que_la_caisse_a_vendu(self):
        """On ne fabrique pas du stock : l'excédent est signalé, pas absorbé."""
        (traites,), _ = self.importer_puis_rembourser(
            self.commande(quantite=1), [self.remboursement(quantite=4)])
        self.assertEqual(traites, 1)
        self.assertEqual(len(self.tickets_de_remboursement()), 1)
        self.assertEqual(self.tailles()["M"], 6, "5 vendu 1 → 5, rendu 1 → 6, et pas 9")
        self.assertEqual(self.rows("SELECT requires_stock_audit FROM Produits WHERE id = ?",
                                   (self.pid,)), [(1,)],
                         "l'écart doit être porté à la connaissance de la commerçante")

    def test_une_ligne_remboursee_introuvable_localement_ne_touche_a_aucun_stock(self):
        """Retirer au hasard serait pire que ne rien faire : on signale et on s'abstient."""
        remb = self.remboursement(ligne={"line_item": {
            "id": 4002, "sku": "INCONNU-AILLEURS", "title": "Article jamais importé",
            "variant_id": 999999, "variant_title": "M"}})
        avant = self.tailles()
        (traites,), _ = self.importer_puis_rembourser(self.commande(), [remb])
        self.assertEqual(traites, 1, "le remboursement est classé traité : il ne reviendra pas en boucle")
        self.assertEqual(self.tickets_de_remboursement(), [], "aucune écriture inventée")
        self.assertEqual(self.tailles()["M"], avant["M"] - 2, "le stock reste celui de la vente")

    def test_une_commande_annulee_sans_remboursement_n_invente_aucune_ecriture(self):
        """
        Shopify permet d'annuler SANS rembourser (paiement conservé, arrangement hors caisse).
        Transformer cela en remboursement signerait un mouvement d'argent qui n'a pas eu lieu.
        """
        commande = self.commande()
        moteur = self.moteur(a_importer=[commande])
        self.assertEqual(moteur.sync_orders_from_shopify(), 1)

        annulee = dict(commande)
        annulee["cancelled_at"] = "2026-02-03T11:00:00+01:00"
        annulee["updated_at"] = "2026-02-03T11:00:00+01:00"
        moteur = self.moteur(apres=[annulee])
        self.assertEqual(moteur.sync_refunds_from_shopify(), 0)

        self.assertEqual(self.tickets_de_remboursement(), [], "aucun remboursement fabriqué")
        self.assertEqual(self.rows("SELECT requires_stock_audit FROM Produits WHERE id = ?",
                                   (self.pid,)), [(1,)],
                         "mais l'annulation doit être VISIBLE, pas silencieuse")
        self.assertEqual(self.rows("SELECT cle FROM Shopify_Remboursements"), [("annulation:9001",)],
                         "et tracée, pour ne pas ré-alerter à chaque passe")

    def test_une_commande_jamais_importee_ne_declenche_rien(self):
        commande = self.commande(id=424242)
        commande["refunds"] = [self.remboursement()]
        moteur = self.moteur(apres=[commande])
        self.assertEqual(moteur.sync_refunds_from_shopify(), 0)
        self.assertEqual(self.tickets_de_remboursement(), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Shopify_Remboursements"), [(0,)])


# =============================================================================================
# 3. La fenêtre interrogée : ni tout l'historique, ni un trou
# =============================================================================================

class TestRepereDeFenetre(BaseRemboursements):

    CLE = shopify_sync.CLE_REMB_DEPUIS

    def test_la_premiere_passe_interroge_une_fenetre_bornee_et_pas_tout_l_historique(self):
        moteur = self.moteur(apres=[])
        moteur.sync_refunds_from_shopify()
        endpoints = [e for e, _ in moteur.appels if "updated_at_min" in e]
        self.assertEqual(len(endpoints), 1)
        self.assertIn("status=any", endpoints[0],
                      "une commande remboursée n'est plus `paid` : le filtre d'import ne la verrait pas")

    def test_le_repere_avance_et_borne_la_requete_suivante(self):
        self.importer_puis_rembourser(self.commande(), [self.remboursement()])
        repere = self.rows("SELECT valeur FROM Parametres WHERE cle = ?", (self.CLE,))
        self.assertEqual(len(repere), 1, "le point atteint doit survivre à un redémarrage")

        moteur = self.moteur(apres=[])
        moteur.sync_refunds_from_shopify()
        endpoint = [e for e, _ in moteur.appels if "updated_at_min" in e][0]
        self.assertIn("2026-02-02", endpoint,
                      "la passe suivante repart du dernier point vérifié, pas de 90 jours en arrière")

    def test_le_repere_recule_d_une_minute_pour_ne_jamais_rater_un_remboursement(self):
        self.importer_puis_rembourser(self.commande(), [self.remboursement()])
        valeur = self.rows("SELECT valeur FROM Parametres WHERE cle = ?", (self.CLE,))[0][0]
        atteint = shopify_sync.ShopifySync._horodatage_shopify("2026-02-02T09:00:00+01:00")
        garde = shopify_sync.ShopifySync._horodatage_shopify(valeur)
        self.assertEqual((atteint - garde).total_seconds(),
                         shopify_sync.RECOUVREMENT_REMBOURSEMENTS_S)

    def test_le_repere_n_avance_pas_quand_une_commande_n_a_pas_pu_etre_traitee(self):
        """
        Le cas qui compte : une commande PLUS RÉCENTE passe, une plus ancienne échoue.
        Si le repère suivait quand même la plus récente, le remboursement manqué serait
        sauté DÉFINITIVEMENT — la passe suivante ne le redemanderait jamais.
        """
        commande = self.commande()
        moteur = self.moteur(a_importer=[commande])
        moteur.sync_orders_from_shopify()

        en_echec = dict(commande, id=9002, refunds=[self.remboursement(refund_id=7002)],
                        updated_at="2026-02-02T09:00:00+01:00")
        qui_passe = dict(commande, refunds=[self.remboursement()],
                         updated_at="2026-02-05T09:00:00+01:00")
        moteur = self.moteur(apres=[en_echec, qui_passe])

        reel = moteur._appliquer_remboursements_commande

        def parfois_en_echec(conn, order):
            if str(order.get("id")) == "9002":
                raise RuntimeError("base verrouillée")
            return reel(conn, order)

        moteur._appliquer_remboursements_commande = parfois_en_echec
        moteur.sync_refunds_from_shopify()

        self.assertEqual(self.rows("SELECT COUNT(*) FROM Tickets WHERE total_tvac < 0"), [(1,)],
                         "la commande saine doit tout de même être traitée")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Parametres WHERE cle = ?", (self.CLE,)),
                         [(0,)], "aucun repère ne doit être posé sur une fenêtre mal lue")


# =============================================================================================
# 4. La clé de rattachement : l'id de variante, pas le SKU
# =============================================================================================

class TestRattachementParVariante(BaseTemporaire):
    """
    Le SKU et le code-barres d'une variante sont modifiables à tout moment depuis l'admin
    Shopify ; l'id de variante, non. Chercher uniquement `code_barre = sku`, comme avant,
    laissait une commande parfaitement légitime « introuvable » : aucune quantité retirée,
    et le stock en ligne qui s'éloigne du stock réel sans que personne ne le voie.
    """

    def setUp(self):
        super().setUp()
        self.regler_shopify("boutique.myshopify.com", "jeton")

    def moteur_catalogue(self, produits):
        return MoteurBouchonne({"products.json": lambda e, d: {"products": produits}},
                               store_url="boutique.myshopify.com", access_token="jeton")

    def moteur_commandes(self, commandes):
        return MoteurBouchonne({"orders.json": lambda e, d: {"orders": [] if "updated_at_min" in e else commandes}},
                               store_url="boutique.myshopify.com", access_token="jeton")

    def test_un_sku_renomme_apres_l_import_ne_fait_plus_perdre_la_commande(self):
        self.moteur_catalogue([{"id": 7, "title": "Jupe", "variants": [
            {"id": 77, "sku": "JUPE-V1", "price": "40.00", "option1": "M"}]}]).import_catalog()
        pid = self.rows("SELECT id FROM Produits")[0][0]
        # L'import crée déjà la ligne de stock de la variante : on lui met une quantité,
        # on n'en ajoute pas une seconde (ce serait une déclinaison en double).
        self.ecrire("UPDATE Stocks SET quantite_actuelle = 5 WHERE id_produit = ? AND taille = 'M'", (pid,))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM Stocks WHERE id_produit = ?", (pid,)), [(1,)])

        # La commerçante renomme le SKU ET l'article côté Shopify. Le titre est délibérément
        # différent pour que le repli par nom NE PUISSE PAS sauver le test : seul l'id de
        # variante peut encore rattacher cette ligne au bon produit.
        commande = {"id": 9100, "order_number": 2001, "total_price": "40.00", "total_tax": "6.94",
                    "line_items": [{"sku": "JUPE-V2", "title": "Jupe plissée (collection 2)",
                                    "quantity": 1, "variant_id": 77, "variant_title": "M",
                                    "price": "40.00"}]}
        self.assertEqual(self.moteur_commandes([commande]).sync_orders_from_shopify(), 1)
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks WHERE id_produit = ?", (pid,)),
                         [(4,)], "la vente en ligne doit bien décompter le stock local")

    def test_une_ligne_sans_sku_est_retrouvee_par_son_code_barres(self):
        """Certaines variantes ne portent qu'un code-barres : l'import écrit celui-là."""
        self.creer_produit("EAN-13-REEL", "Ceinture", [("Unique", 3)])
        # Titre volontairement différent du nom local : le repli par nom ne peut pas
        # masquer une régression du rattachement par code-barres.
        commande = {"id": 9200, "order_number": 2002, "total_price": "15.00", "total_tax": "2.60",
                    "line_items": [{"sku": "", "barcode": "EAN-13-REEL",
                                    "title": "Ceinture cuir camel", "quantity": 1,
                                    "price": "15.00"}]}
        self.assertEqual(self.moteur_commandes([commande]).sync_orders_from_shopify(), 1)
        self.assertEqual(self.rows("SELECT quantite_actuelle FROM Stocks"), [(2,)])


if __name__ == "__main__":
    unittest.main()
