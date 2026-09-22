# -*- coding: utf-8 -*-
"""
Kōdo POS — Le stock d'une vente est retiré du BON dépôt de la boutique Shopify.

Une boutique Shopify peut compter plusieurs emplacements : le magasin, une réserve, un
entrepôt de préparation, une boutique éphémère. L'API n'ajuste jamais « le stock » en
général : elle ajuste le stock d'UN dépôt nommé (`location_id`).

Le code d'origine prenait le premier dépôt actif renvoyé par `locations.json`, dans un ordre
que Shopify ne garantit nulle part, et sans qu'aucun réglage ne permette de le choisir. Sur une
boutique à plusieurs dépôts, une vente au comptoir pouvait donc être retirée de la réserve
pendant que la fiche en ligne du magasin restait pleine — et l'ordre pouvait changer d'une
passe à l'autre, éparpillant le stock entre les dépôts. Rien n'échouait, rien n'était
journalisé : l'écart n'apparaissait qu'à l'inventaire, des semaines plus tard.

Ces tests décrivent ce que la caisse doit faire dans les quatre situations réelles (dépôt
choisi, dépôt unique, plusieurs dépôts sans choix, dépôt disparu), et vérifient que changer
d'avis prend effet sans redémarrer la caisse.

Aucun appel réseau : `make_request` est bouchonné, comme dans `test_shopify_sync.py`.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database_manager
from kodo_core.api.app import kodo_app
from kodo_core.sync.shopify import ShopifySync, lire_etat_sync, lire_reglages_shopify


def depot(identifiant, nom, actif=True):
    return {"id": identifiant, "name": nom, "active": actif}


# Renvoyés dans un ordre quelconque, exprès : c'est tout le sujet.
TROIS_DEPOTS = {"locations": [depot(222, "Réserve"),
                              depot(111, "Magasin"),
                              depot(333, "Entrepôt")]}


class MoteurBouchonne(ShopifySync):
    """Moteur dont les échanges HTTP sont remplacés par des réponses écrites dans le test."""

    def __init__(self, reponses, **kwargs):
        super().__init__(**kwargs)
        self.reponses = reponses
        self.appels = []

    def make_request(self, endpoint, method="GET", data=None, max_retries=3, rejouable=True):
        self.appels.append((endpoint, data))
        self.dernier_echec = None
        for cle, valeur in self.reponses.items():
            if endpoint.startswith(cle):
                reponse = valeur(endpoint, data) if callable(valeur) else valeur
                if reponse is None:
                    self.dernier_echec = "http"
                return reponse
        self.dernier_echec = "http"
        return None


class BaseDepot(unittest.TestCase):
    """Base SQLite jetable : aucun test ne touche aux données réelles de la commerçante."""

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()
        self.regler("shopify_store_url", "boutique-de-la-cliente.myshopify.com")
        self.regler("shopify_access_token", "shpat_JETON_DE_LA_CLIENTE")

    def tearDown(self):
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    # --- utilitaires ------------------------------------------------------------------

    def regler(self, cle, valeur):
        conn = database_manager.get_connection()
        try:
            conn.cursor().execute(
                "INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)", (cle, valeur))
            conn.commit()
        finally:
            conn.close()

    def rows(self, sql, params=()):
        conn = database_manager.get_connection()
        try:
            return [tuple(r) for r in conn.cursor().execute(sql, params).fetchall()]
        finally:
            conn.close()

    def api(self, method, path, data=None):
        status, body, _ = kodo_app.handle_request(method, path, {}, {}, data or {})
        return status, body

    def moteur(self, reponses=None, ajustements=None):
        """Moteur complet : dépôts, résolution de variante, ajustement d'inventaire."""
        reponses = dict(reponses or {"locations.json": TROIS_DEPOTS})
        reponses.setdefault("graphql.json", {"data": {"productVariants": {"edges": [
            {"node": {"inventoryItem": {"id": "gid://shopify/InventoryItem/777"}}}]}}})
        if ajustements is not None:
            reponses.setdefault(
                "inventory_levels/adjust.json",
                lambda e, d: ajustements.append(d) or {"ok": 1})
        else:
            reponses.setdefault("inventory_levels/adjust.json", {"ok": 1})
        return MoteurBouchonne(reponses)

    def creer_vente(self, numero="T-DEPOT-1", quantite=1):
        """Un produit, une ligne de stock, un ticket non synchronisé : de quoi pousser."""
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac, taux_tva) "
                      "VALUES ('CB-DEPOT', 'Robe', 'Test', '10.00', '0.21')")
            pid = c.lastrowid
            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) "
                      "VALUES (?, 'Unique', 5)", (pid,))
            sid = c.lastrowid
            c.execute("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, "
                      "methode_paiement, synced_shopify) "
                      "VALUES (?, '2026-01-01 10:00:00', '10.00', 'CB', 0)", (numero,))
            tid = c.lastrowid
            c.execute("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) "
                      "VALUES (?, ?, ?, '10.00')", (tid, sid, quantite))
            conn.commit()
        finally:
            conn.close()


# =============================================================================================
# 1. Le dépôt choisi par la commerçante est celui qui reçoit la vente
# =============================================================================================

class TestLeDepotChoisiEstRespecte(BaseDepot):

    def test_la_vente_est_retiree_du_depot_choisi_et_d_aucun_autre(self):
        """
        Le cœur du défaut. « Entrepôt » est le TROISIÈME de la liste et le dernier par
        identifiant : aucun ordre accidentel ne peut y mener. S'il est choisi, c'est lui.
        """
        self.regler("shopify_location_id", "333")
        self.creer_vente()
        ajustements = []
        moteur = self.moteur(ajustements=ajustements)

        moteur.sync_tickets_to_shopify()

        self.assertEqual(
            ajustements,
            [{"inventory_item_id": 777, "location_id": 333, "available_adjustment": -1}],
            "la vente n'a pas été retirée du dépôt choisi dans les réglages")

    def test_le_depot_choisi_au_milieu_de_la_liste_est_respecte_aussi(self):
        """« Magasin » est deuxième dans la réponse : ni le premier reçu, ni le plus petit rang."""
        self.regler("shopify_location_id", "111")
        self.creer_vente()
        ajustements = []
        self.moteur(ajustements=ajustements).sync_tickets_to_shopify()
        self.assertEqual([a["location_id"] for a in ajustements], [111])

    def test_choisir_un_depot_n_ecrit_aucun_avertissement(self):
        """Un choix explicite ne doit rien reprocher à personne."""
        self.regler("shopify_location_id", "222")
        moteur = self.moteur()
        self.assertEqual(moteur.get_location_id(), 222)
        self.assertEqual(lire_etat_sync()["avertissement_depot"], "")


# =============================================================================================
# 2. Sans choix : décider quand c'est évident, le dire quand ça ne l'est pas
# =============================================================================================

class TestSansChoixExplicite(BaseDepot):

    def test_une_boutique_a_un_seul_depot_ne_demande_rien(self):
        """L'immense majorité des boutiques : aucune ambiguïté, donc aucune friction."""
        moteur = self.moteur({"locations.json": {"locations": [depot(999, "Boutique")]}})
        self.assertEqual(moteur.get_location_id(), 999)
        self.assertEqual(lire_etat_sync()["avertissement_depot"], "",
                         "une boutique à dépôt unique n'a rien à décider")

    def test_plusieurs_depots_le_choix_est_stable_quel_que_soit_l_ordre_recu(self):
        """
        Le défaut d'origine n'est pas « le mauvais dépôt » : c'est « un dépôt au hasard ».
        Deux passes recevant la même liste dans deux ordres différents doivent viser le même.
        """
        ordre_a = {"locations": [depot(222, "Réserve"), depot(111, "Magasin"), depot(333, "Entrepôt")]}
        ordre_b = {"locations": [depot(333, "Entrepôt"), depot(222, "Réserve"), depot(111, "Magasin")]}
        premier = self.moteur({"locations.json": ordre_a}).get_location_id()
        second = self.moteur({"locations.json": ordre_b}).get_location_id()
        self.assertEqual(premier, second,
                         "le dépôt visé change avec l'ordre de réponse de Shopify")
        self.assertEqual(premier, 111, "le dépôt le plus ancien (le magasin) doit être retenu")

    def test_plusieurs_depots_sans_choix_le_dit_a_l_ecran(self):
        """Un `logger.warning` reste dans un fichier que personne n'ouvre. Il faut que ça se voie."""
        self.moteur().get_location_id()
        avertissement = lire_etat_sync()["avertissement_depot"]
        self.assertIn("Magasin", avertissement, "l'écran ne dit pas quel dépôt est utilisé")
        self.assertIn("Réserve", avertissement, "l'écran ne dit pas quels sont les autres dépôts")
        self.assertIn("Réglages", avertissement, "l'écran ne dit pas où faire le choix")

    def test_l_avertissement_s_efface_des_que_le_choix_est_fait(self):
        """Un reproche qui reste affiché après correction devient du bruit, puis de l'aveuglement."""
        self.moteur().get_location_id()
        self.assertNotEqual(lire_etat_sync()["avertissement_depot"], "")

        self.regler("shopify_location_id", "222")
        moteur = self.moteur()
        moteur.load_config()
        self.assertEqual(moteur.get_location_id(), 222)
        self.assertEqual(lire_etat_sync()["avertissement_depot"], "")


# =============================================================================================
# 3. Un dépôt introuvable arrête la synchro : il ne se rabat JAMAIS sur un autre
# =============================================================================================

class TestUnDepotIntrouvableNeGlissePasVersUnAutre(BaseDepot):

    def test_un_depot_supprime_arrete_la_poussee_au_lieu_de_viser_le_voisin(self):
        """
        Le pire scénario possible : la commerçante a choisi « Entrepôt », Shopify ne le connaît
        plus. Se rabattre sur un autre dépôt contredirait en silence un choix EXPLICITE.
        """
        self.regler("shopify_location_id", "444")   # n'existe pas dans TROIS_DEPOTS
        self.creer_vente()
        ajustements = []
        moteur = self.moteur(ajustements=ajustements)

        moteur.sync_tickets_to_shopify()

        self.assertIsNone(moteur.get_location_id())
        self.assertEqual(ajustements, [],
                         "le stock est parti vers un dépôt que la commerçante n'a pas choisi")
        self.assertEqual(self.rows("SELECT synced_shopify FROM Tickets"), [(0,)],
                         "le ticket a été marqué synchronisé alors que rien n'est parti")
        self.assertIn("444", lire_etat_sync()["avertissement_depot"])

    def test_un_depot_desactive_est_traite_comme_absent(self):
        """Un emplacement désactivé côté Shopify ne peut plus recevoir d'ajustement de stock."""
        self.regler("shopify_location_id", "333")
        moteur = self.moteur({"locations.json": {"locations": [
            depot(111, "Magasin"), depot(333, "Entrepôt", actif=False)]}})
        self.assertIsNone(moteur.get_location_id(),
                          "un dépôt désactivé a été retenu comme cible d'ajustement")

    def test_une_boutique_sans_aucun_depot_actif_le_dit(self):
        """Rien à ajuster nulle part : la commerçante doit savoir pourquoi plus rien ne circule."""
        moteur = self.moteur({"locations.json": {"locations": [depot(111, "Magasin", actif=False)]}})
        self.assertIsNone(moteur.get_location_id())
        self.assertIn("Aucun dépôt actif", lire_etat_sync()["avertissement_depot"])

    def test_une_boutique_injoignable_n_efface_pas_le_depot_en_cache(self):
        """Une coupure réseau ne doit pas être lue comme « le dépôt a disparu »."""
        self.regler("shopify_location_id", "222")
        moteur = self.moteur()
        self.assertEqual(moteur.get_location_id(), 222)
        moteur.reponses["locations.json"] = None          # Shopify ne répond plus
        self.assertEqual(moteur.get_location_id(), 222,
                         "le dépôt déjà résolu a été perdu à la première coupure")


# =============================================================================================
# 4. Changer d'avis prend effet sans redémarrer la caisse
# =============================================================================================

class TestChangerDeCibleSansRedemarrer(BaseDepot):

    def test_changer_de_depot_prend_effet_a_la_passe_suivante(self):
        """
        Le thread de synchro vit des heures et relit les réglages à chaque passe. Sans oubli du
        cache, changer de dépôt n'aurait d'effet qu'au prochain lancement de l'application.
        """
        self.regler("shopify_location_id", "111")
        moteur = self.moteur()
        self.assertEqual(moteur.get_location_id(), 111)

        self.regler("shopify_location_id", "333")
        moteur.load_config()
        self.assertEqual(moteur.get_location_id(), 333,
                         "la caisse continue de retirer le stock de l'ancien dépôt")

    def test_changer_de_boutique_oublie_le_depot_de_l_ancienne(self):
        """
        Un identifiant de dépôt n'a de sens que dans SA boutique. Le garder après un changement
        de boutique reviendrait à viser un numéro qui, là-bas, désigne autre chose ou rien.
        """
        moteur = self.moteur()
        self.assertEqual(moteur.get_location_id(), 111)

        self.regler("shopify_store_url", "autre-boutique.myshopify.com")
        moteur.reponses["locations.json"] = {"locations": [depot(555, "Unique")]}
        moteur.load_config()
        self.assertEqual(moteur.get_location_id(), 555,
                         "le dépôt de l'ancienne boutique est resté en cache")


# =============================================================================================
# 5. Le réglage existe vraiment dans le produit (écran Réglages ↔ base ↔ moteur)
# =============================================================================================

class TestLeReglageEstBrancheDansLeProduit(BaseDepot):

    def test_le_depot_choisi_a_l_ecran_est_enregistre_et_relu(self):
        """Aller-retour complet : POST /api/settings → Parametres → GET /api/settings."""
        status, body = self.api("POST", "/api/settings", {"shopifyLocationId": "333"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.rows("SELECT valeur FROM Parametres WHERE cle='shopify_location_id'"),
                         [("333",)])

        status, reglages = self.api("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(reglages.get("shopifyLocationId"), "333")

    def test_le_moteur_lit_le_depot_par_le_meme_chemin_que_l_ecran(self):
        """Le réglage écrit par l'écran doit être CELUI que le moteur relit."""
        self.api("POST", "/api/settings", {"shopifyLocationId": "222"})
        self.assertEqual(lire_reglages_shopify()["location_id"], "222")
        self.assertEqual(self.moteur().get_location_id(), 222)

    def test_un_identifiant_de_depot_fantaisiste_est_refuse_tout_de_suite(self):
        """
        Un dépôt qui ne correspond à rien arrête toute la synchro (cf. plus haut). Le refuser à
        la saisie vaut mieux que de laisser la boutique s'arrêter en silence le lendemain.
        """
        status, body = self.api("POST", "/api/settings", {"shopifyLocationId": "Entrepôt"})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.rows("SELECT valeur FROM Parametres WHERE cle='shopify_location_id'"), [])

    def test_ne_pas_choisir_reste_un_ordre_legitime(self):
        """Revenir en arrière doit être possible : la chaîne vide efface le choix."""
        self.regler("shopify_location_id", "333")
        status, body = self.api("POST", "/api/settings", {"shopifyLocationId": ""})
        self.assertEqual(status, 200, body)
        self.assertEqual(lire_reglages_shopify()["location_id"], "")

    def test_enregistrer_autre_chose_ne_touche_pas_au_depot(self):
        """Mise à jour partielle : le seuil d'alerte ne doit pas effacer le dépôt choisi."""
        self.regler("shopify_location_id", "222")
        self.api("POST", "/api/settings", {"defaultAlertThreshold": 7})
        self.assertEqual(lire_reglages_shopify()["location_id"], "222")

    def test_l_ecran_recoit_de_quoi_proposer_le_choix(self):
        """
        `tester_connexion` ne renvoyait que des NOMS : de quoi écrire une phrase, pas de quoi
        proposer une liste. Sans identifiant, aucun choix n'est possible à l'écran.
        """
        resultat = self.moteur().tester_connexion()
        self.assertTrue(resultat["success"], resultat)
        self.assertEqual(
            resultat["depots"],
            [{"id": "222", "nom": "Réserve", "actif": True},
             {"id": "111", "nom": "Magasin", "actif": True},
             {"id": "333", "nom": "Entrepôt", "actif": True}])
        # L'ancien champ reste intact : la phrase affichée aujourd'hui ne change pas.
        self.assertEqual(resultat["locations"], ["Réserve", "Magasin", "Entrepôt"])

    def test_l_etat_de_synchro_porte_le_depot_et_son_avertissement(self):
        """`GET /api/shopify/status` est le seul endroit où la commerçante voit ce qui se passe."""
        self.moteur().get_location_id()          # plusieurs dépôts, aucun choix
        status, body = self.api("GET", "/api/shopify/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["locationId"], "")
        self.assertIn("Magasin", body["depotAvertissement"])

        self.regler("shopify_location_id", "333")
        status, body = self.api("GET", "/api/shopify/status")
        self.assertEqual(body["locationId"], "333")


if __name__ == "__main__":
    unittest.main()
