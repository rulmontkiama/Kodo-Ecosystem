# -*- coding: utf-8 -*-
"""
Contrôle d'intégrité au lancement (kodo_core.services.startup_check).

Ce que ces tests figent :
- il ne MODIFIE RIEN (base, snapshots, clé de scellement) ;
- chaque défaut dormant qu'il prétend voir est vu, et une base d'usine saine ne lève aucune alerte ;
- il ne plante jamais et ne bloque jamais, quoi qu'il rencontre ;
- le « nouveau depuis la dernière fois » ne crie pas pour un défaut hérité.
"""
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kodo_core.services import startup_check as sc  # noqa: E402


def _sha(chemin):
    with open(chemin, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class BaseControle(unittest.TestCase):
    """Base d'usine jetable + HOME jetable (la clé de scellement vit dans ~/.kodo_signing)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kodo_startup_check_")
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["HOME"])
        self.db = os.path.join(self.tmp, "kodo_pos.db")
        self.rapports = os.path.join(self.tmp, "diagnostics")

        import database_manager
        self.dm = database_manager
        self._db_origine = database_manager.DB_NAME
        database_manager.DB_NAME = self.db
        os.environ["KODO_DB_PATH"] = self.db
        database_manager.initialiser_db()
        sc._ETAT["rapport"] = None
        sc._ETAT["en_cours"] = False
        sc._ETAT.pop("fournisseur_dist", None)

    def tearDown(self):
        self.dm.DB_NAME = self._db_origine
        os.environ.pop("KODO_DB_PATH", None)
        os.environ.pop("KODO_STARTUP_CHECK", None)
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        sc._ETAT["rapport"] = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- aides -------------------------------------------------------------------------------
    def sql(self, requete, params=()):
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute(requete, params)
            conn.commit()
            return cur.fetchall()
        finally:
            conn.close()

    def controle(self, niveau=sc.NIVEAU_COMPLET, **kw):
        kw.setdefault("dossier_rapport", self.rapports)
        return sc.executer_controle(db_path=self.db, niveau=niveau, **kw)

    @staticmethod
    def par_id(rapport):
        return {c["id"]: c for c in rapport["controles"]}

    def poser_cle_de_scellement(self):
        """La clé HMAC de la machine (créée normalement à la première vente)."""
        dossier = os.path.join(os.environ["HOME"], ".kodo_signing")
        os.makedirs(dossier, exist_ok=True)
        with open(os.path.join(dossier, "audit_hmac.key"), "wb") as f:
            f.write(b"0" * 64)

    def ticket(self, numero, jour=None, z_id=None):
        jour = jour or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.sql("INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, caisse_id, z_id) "
                 "VALUES (?, ?, 10, 8.26, 1.74, 'POS-01', ?)", (numero, jour, z_id))


class TestBaseSaine(BaseControle):

    def test_une_base_d_usine_ne_leve_aucune_alerte(self):
        r = self.controle()
        graves = [(c["id"], c["statut"], c["detail"]) for c in r["controles"]
                  if c["statut"] in (sc.ATTENTION, sc.CRITIQUE, sc.ERREUR)]
        self.assertEqual(graves, [], f"Alertes sur une base d'usine : {graves}")
        self.assertEqual(r["statut"], "OK")
        self.assertEqual(r["non_executes"], [])

    def test_tous_les_controles_sont_executes_au_niveau_complet(self):
        ids = {c["id"] for c in self.controle()["controles"]}
        self.assertEqual(ids, {c[0] for c in sc.CONTROLES})

    def test_le_niveau_rapide_n_execute_pas_les_controles_complets(self):
        ids = {c["id"] for c in self.controle(niveau=sc.NIVEAU_RAPIDE)["controles"]}
        self.assertIn("base.integrite", ids)
        self.assertNotIn("ventes.chaine_nf525", ids)
        self.assertNotIn("shopify.file", ids)


class TestLectureSeule(BaseControle):

    def test_la_base_les_sauvegardes_et_la_cle_ne_changent_pas(self):
        # Un ticket sans clé de scellement : le contrôle ne doit PAS en fabriquer une.
        self.ticket("TCK-2026-0001")
        from kodo_core.config import ShopConfig
        snaps = ShopConfig.get_snapshots_dir()
        avant_snaps = sorted(os.listdir(snaps))
        avant_hash, avant_mtime = _sha(self.db), os.stat(self.db).st_mtime_ns
        cle = os.path.join(os.environ["HOME"], ".kodo_signing", "audit_hmac.key")
        self.assertFalse(os.path.exists(cle))

        r = self.controle()

        self.assertEqual(_sha(self.db), avant_hash, "la base a été modifiée")
        self.assertEqual(os.stat(self.db).st_mtime_ns, avant_mtime)
        self.assertEqual(sorted(os.listdir(snaps)), avant_snaps, "un instantané a été créé")
        self.assertFalse(os.path.exists(cle), "le contrôle a fabriqué une clé de scellement")
        self.assertEqual(self.par_id(r)["ventes.chaine_nf525"]["statut"], sc.ATTENTION)

    def test_la_version_est_lue_sans_ecrire_app_version(self):
        # `updater.get_installed_version` écrit Parametres.app_version quand il manque : pas ici.
        self.assertEqual(self.sql("SELECT COUNT(*) FROM Parametres WHERE cle = 'app_version'")[0][0], 0)
        r = self.controle()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM Parametres WHERE cle = 'app_version'")[0][0], 0)
        self.assertTrue(r["version"])

    def test_la_connexion_refuse_toute_ecriture(self):
        conn = sc._ouvrir_lecture_seule(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO Marques (nom) VALUES ('X')")
        finally:
            conn.close()

    def test_une_base_absente_n_est_pas_creee(self):
        absente = os.path.join(self.tmp, "absente.db")
        r = sc.executer_controle(db_path=absente, dossier_rapport=self.rapports)
        self.assertFalse(os.path.exists(absente))
        self.assertEqual(r["statut"], "CRITIQUE")
        self.assertEqual(self.par_id(r)["base.fichier"]["statut"], sc.CRITIQUE)


class TestDefautsDormants(BaseControle):

    def test_migration_jamais_appliquee(self):
        v = sc._versions_migrations()[-1]
        self.sql("DELETE FROM schema_version WHERE version = ?", (v,))
        c = self.par_id(self.controle())["base.migrations"]
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn(v, c["donnees"]["manquantes"])

    def test_base_plus_recente_que_l_application(self):
        self.sql("INSERT INTO schema_version (version) VALUES ('99.0.0')")
        c = self.par_id(self.controle())["base.migrations"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertIn("99.0.0", c["donnees"]["inconnues_de_cette_version"])

    def test_table_supprimee_derive_de_schema(self):
        self.sql("DROP TABLE sync_queue")
        r = self.par_id(self.controle())
        self.assertEqual(r["base.derive_schema"]["statut"], sc.CRITIQUE)
        self.assertIn("sync_queue", r["base.derive_schema"]["donnees"]["tables_manquantes"])

    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 35, 0), "DROP COLUMN exige SQLite 3.35")
    def test_colonne_avalee_par_un_alter_silencieux(self):
        # Le cas réel : `ALTER TABLE ... ADD COLUMN` entouré d'un `except: pass` qui échoue en silence.
        self.sql("ALTER TABLE Cartes_Cadeaux DROP COLUMN ticket_origine")
        c = self.par_id(self.controle())["base.derive_schema"]
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn("ticket_origine", c["donnees"]["colonnes_manquantes"]["Cartes_Cadeaux"])

    def test_declencheur_de_protection_manquant(self):
        nom = self.sql("SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1")
        self.assertTrue(nom, "la base d'usine devrait porter des déclencheurs")
        self.sql(f'DROP TRIGGER "{nom[0][0]}"')
        c = self.par_id(self.controle())["base.derive_schema"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertIn(nom[0][0], c["donnees"]["declencheurs_manquants"])

    def test_ligne_de_vente_orpheline(self):
        self.ticket("TCK-2026-0001")
        self.sql("INSERT INTO Ventes_Details (id_ticket, id_stock, quantite, prix_unitaire_tvac) VALUES (1, 9999, 1, 10)")
        c = self.par_id(self.controle())["base.cles_etrangeres"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertEqual(c["donnees"]["groupes"], {"Ventes_Details → Stocks": 1})

    def test_corruption_de_page(self):
        conn = sqlite3.connect(self.db)
        for i in range(600):
            conn.execute("INSERT INTO Produits (nom, prix_vente_tvac) VALUES (?, 10)", (f"Produit {i} " + "x" * 80,))
        conn.commit()
        conn.close()
        taille_page = self.sql("PRAGMA page_size")[0][0]
        with open(self.db, "r+b") as f:
            f.seek(taille_page * 6)
            f.write(b"\xff" * 400)
        r = self.controle()
        self.assertEqual(r["statut"], "CRITIQUE")
        par_id = self.par_id(r)
        # Selon la page touchée, SQLite refuse déjà l'ouverture ou ne le dit qu'au quick_check.
        critiques = {i for i in ("base.integrite", "base.ouverture") if par_id.get(i, {}).get("statut") == sc.CRITIQUE}
        self.assertTrue(critiques, f"corruption non vue : {[(c['id'], c['statut']) for c in r['controles']]}")
        # Un seul constat net : les contrôles qui lisent la base ne sont pas répétés en cascade d'erreurs.
        self.assertEqual(r["resume"]["erreur"], 0)

    def test_fichier_qui_n_est_pas_une_base(self):
        with open(self.db, "wb") as f:
            f.write(b"ceci n'est pas une base SQLite" * 50)
        r = self.controle()
        self.assertEqual(r["statut"], "CRITIQUE")
        self.assertEqual(self.par_id(r)["base.ouverture"]["statut"], sc.CRITIQUE)
        self.assertEqual(r["resume"]["erreur"], 0, "pas de cascade de constats « erreur »")

    def test_file_shopify_bloquee_depuis_plus_de_24_h(self):
        ancien = (datetime.datetime.now() - datetime.timedelta(hours=60)).strftime("%Y-%m-%d %H:%M:%S")
        self.sql("INSERT INTO sync_queue (id_vente_detail, id_ticket, code_barre, delta, statut, tentatives, "
                 "derniere_erreur, date_creation, date_maj) VALUES (1, 1, 'X', -1, 'PENDING', 12, "
                 "'HTTP 401 Unauthorized', ?, ?)", (ancien, ancien))
        c = self.par_id(self.controle())["shopify.file"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertIn("401", c["detail"])
        self.assertGreaterEqual(c["donnees"]["age_heures"], 59)

    def test_file_shopify_recente_n_alarme_pas(self):
        maintenant = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.sql("INSERT INTO sync_queue (id_vente_detail, id_ticket, code_barre, delta, statut, date_creation, date_maj) "
                 "VALUES (1, 1, 'X', -1, 'PENDING', ?, ?)", (maintenant, maintenant))
        self.assertEqual(self.par_id(self.controle())["shopify.file"]["statut"], sc.OK)

    def test_jour_passe_non_cloture(self):
        hier = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
        self.ticket("TCK-2026-0001", jour=hier)
        c = self.par_id(self.controle())["ventes.jours_non_clotures"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertEqual(c["donnees"]["total_jours"], 1)

    def test_aujourd_hui_non_cloture_est_normal(self):
        self.ticket("TCK-2026-0001")
        self.assertEqual(self.par_id(self.controle())["ventes.jours_non_clotures"]["statut"], sc.OK)

    def test_ticket_rattache_a_un_z_inexistant(self):
        self.ticket("TCK-2026-0001", z_id=4242)
        c = self.par_id(self.controle())["ventes.z_orphelins"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertEqual(c["donnees"]["Tickets"], 1)

    def test_stock_negatif_sans_audit_et_taille_en_double(self):
        self.sql("INSERT INTO Produits (id, nom, prix_vente_tvac) VALUES (1, 'Robe', 50)")
        self.sql("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (1, 'M', -2)")
        self.sql("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (1, 'L', 3)")
        self.sql("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (1, 'L', 1)")
        c = self.par_id(self.controle())["stock.coherence"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertEqual(c["donnees"]["negatifs_sans_drapeau_audit"], 1)
        self.assertEqual(c["donnees"]["doublons_taille"], 1)

    def test_stock_negatif_deja_marque_a_auditer_n_alarme_pas(self):
        self.sql("INSERT INTO Produits (id, nom, prix_vente_tvac) VALUES (1, 'Robe', 50)")
        self.sql("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, requires_stock_audit) VALUES (1, 'M', -2, 1)")
        self.assertEqual(self.par_id(self.controle())["stock.coherence"]["statut"], sc.INFO)

    def test_article_sans_prix(self):
        self.sql("INSERT INTO Produits (nom, prix_vente_tvac) VALUES ('Sans prix', NULL)")
        c = self.par_id(self.controle())["produits.catalogue"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertEqual(c["donnees"]["sans_prix_ou_negatif"], 1)

    def test_interface_sans_bundle(self):
        dist = os.path.join(self.tmp, "dist")
        os.makedirs(os.path.join(dist, "assets"))
        with open(os.path.join(dist, "index.html"), "w") as f:
            f.write('<html><script type="module" src="/assets/index-abc.js"></script></html>')
        c = self.par_id(self.controle(dist_dir=dist))["interface.dist"]
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn("/assets/index-abc.js", c["donnees"]["manquants"])
        with open(os.path.join(dist, "assets", "index-abc.js"), "w") as f:
            f.write("console.log(1)")
        self.assertEqual(self.par_id(self.controle(dist_dir=dist))["interface.dist"]["statut"], sc.OK)


class TestNouveauDepuisLaDerniereFois(BaseControle):

    def test_un_trou_herite_n_alarme_pas_mais_un_trou_nouveau_oui(self):
        for n in (1, 2, 4):
            self.ticket(f"TCK-2026-{n:04d}")
        premier = self.par_id(self.controle())["ventes.numerotation"]
        self.assertEqual(premier["statut"], sc.INFO, "un trou hérité ne doit pas alarmer au premier contrôle")

        second = self.par_id(self.controle())["ventes.numerotation"]
        self.assertEqual(second["statut"], sc.INFO, "le même trou, revu, reste informatif")

        self.ticket("TCK-2026-0007")
        troisieme = self.controle()
        c = self.par_id(troisieme)["ventes.numerotation"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertIn("NOUVEAU", c["detail"])
        self.assertIn("ventes.numerotation", troisieme["nouveautes"])

    def test_un_defaut_corrige_est_signale_comme_resolu(self):
        self.poser_cle_de_scellement()
        self.ticket("TCK-2026-0001", z_id=4242)
        self.assertEqual(self.controle()["statut"], "ATTENTION")
        self.sql("INSERT INTO Clotures_Caisse (id, date_cloture, caisse_id) VALUES (4242, '2026-10-01', 'POS-01')")
        r = self.controle()
        self.assertIn("ventes.z_orphelins", r["resolus"])
        self.assertEqual(r["statut"], "OK")

    def test_l_empreinte_ne_change_pas_tant_que_les_alertes_sont_les_memes(self):
        self.ticket("TCK-2026-0001", z_id=4242)
        a, b = self.controle()["empreinte"], self.controle()["empreinte"]
        self.assertTrue(a)
        self.assertEqual(a, b)
        self.ticket("TCK-2026-0002", z_id=4243)
        self.assertNotEqual(self.controle()["empreinte"], a)


class TestEmpreinteStable(unittest.TestCase):
    """Une bannière fermée ne doit pas revenir à chaque lancement parce qu'un compteur a bougé."""

    def constat(self, **donnees):
        return sc._constat("shopify.file", "Synchronisation Shopify", sc.ATTENTION, "bloquée", donnees)

    def test_les_mesures_qui_bougent_n_entrent_pas_dans_l_empreinte(self):
        a = self.constat(plus_ancien="2026-10-01 10:00:00", age_heures=60, en_attente=12, derniere_erreur="HTTP 401")
        b = self.constat(plus_ancien="2026-10-01 10:00:00", age_heures=84, en_attente=15, derniere_erreur="HTTP 401 (bis)")
        self.assertEqual(sc._empreinte_constat(a), sc._empreinte_constat(b))

    def test_un_probleme_different_change_l_empreinte(self):
        a = self.constat(plus_ancien="2026-10-01 10:00:00", indetermines=0)
        b = self.constat(plus_ancien="2026-10-01 10:00:00", indetermines=3)
        self.assertNotEqual(sc._empreinte_constat(a), sc._empreinte_constat(b))

    def test_les_mesures_imbriquees_sont_aussi_ecartees(self):
        a = sc._constat("ventes.jours_non_clotures", "J", sc.ATTENTION, "", {"jours": [{"jour": "2026-10-12", "tickets": 4}]})
        b = sc._constat("ventes.jours_non_clotures", "J", sc.ATTENTION, "", {"jours": [{"jour": "2026-10-12", "tickets": 9}]})
        c = sc._constat("ventes.jours_non_clotures", "J", sc.ATTENTION, "", {"jours": [{"jour": "2026-10-13", "tickets": 4}]})
        self.assertEqual(sc._empreinte_constat(a), sc._empreinte_constat(b))
        self.assertNotEqual(sc._empreinte_constat(a), sc._empreinte_constat(c))


class TestJamaisBloquant(BaseControle):

    def test_un_controle_qui_plante_devient_un_constat_erreur(self):
        def boom(ctx):
            raise RuntimeError("défaut imprévu")

        def sain(ctx):
            return sc._constat("x.sain", "Sain", sc.OK, "bien")

        with mock.patch.object(sc, "CONTROLES", [
            ("x.boom", "Boom", sc.NIVEAU_RAPIDE, False, boom),
            ("x.sain", "Sain", sc.NIVEAU_RAPIDE, False, sain),
        ]):
            r = self.controle()
        c = self.par_id(r)
        self.assertEqual(c["x.boom"]["statut"], sc.ERREUR)
        self.assertIn("défaut imprévu", c["x.boom"]["detail"])
        self.assertEqual(c["x.sain"]["statut"], sc.OK)
        self.assertEqual(r["resume"]["erreur"], 1)
        # Un contrôle qui n'a pas pu conclure n'est JAMAIS un « tout va bien » (revue du 2026-10-14).
        self.assertEqual(r["statut"], "ATTENTION")

    def test_le_budget_de_temps_est_respecte_et_signale(self):
        with mock.patch.object(sc, "BUDGET_SECONDES", -1.0):
            r = self.controle()
        self.assertEqual([c["id"] for c in r["controles"]], ["controle.incomplet"])
        self.assertEqual(len(r["non_executes"]), len(sc.CONTROLES))
        # Des contrôles sautés ne sont jamais présentés comme « OK » : constat visible + statut global.
        self.assertEqual(r["statut"], "ATTENTION")
        self.assertEqual(r["controles"][0]["statut"], sc.ATTENTION)

    def test_dossier_de_rapport_illisible_ne_leve_pas(self):
        # Un fichier à la place du dossier : l'écriture échoue, le rapport est tout de même retourné.
        faux = os.path.join(self.tmp, "fichier")
        with open(faux, "w") as f:
            f.write("x")
        r = sc.executer_controle(db_path=self.db, dossier_rapport=os.path.join(faux, "sous"))
        self.assertIn("statut", r)

    def test_le_thread_de_fond_produit_un_rapport_et_n_est_pas_reentrant(self):
        porte = threading.Event()
        vrai = sc.executer_controle

        def lent(*a, **kw):
            porte.wait(5)
            return vrai(*a, **kw)

        with mock.patch.object(sc, "executer_controle", lent):
            self.assertTrue(sc.demarrer_en_arriere_plan(delai=0, db_path=self.db, dossier_rapport=self.rapports))
            self.assertTrue(sc.en_cours())
            self.assertFalse(sc.demarrer_en_arriere_plan(delai=0, db_path=self.db, dossier_rapport=self.rapports),
                             "un second contrôle ne doit pas se lancer pendant le premier")
            porte.set()
            fin = time.monotonic() + 10
            while sc.en_cours() and time.monotonic() < fin:
                time.sleep(0.05)
        self.assertFalse(sc.en_cours())
        rapport = sc.dernier_rapport(self.rapports)
        self.assertIsNotNone(rapport)
        self.assertEqual(rapport["statut"], "OK")
        # Le verrou est rendu : un contrôle ultérieur peut repartir.
        self.assertTrue(sc.demarrer_en_arriere_plan(delai=0, db_path=self.db, dossier_rapport=self.rapports))
        fin = time.monotonic() + 10
        while sc.en_cours() and time.monotonic() < fin:
            time.sleep(0.05)

    def test_desactivation_par_l_environnement(self):
        os.environ["KODO_STARTUP_CHECK"] = "0"
        self.assertFalse(sc.demarrer_en_arriere_plan(delai=0, db_path=self.db, dossier_rapport=self.rapports))
        self.assertFalse(sc.en_cours())

    def test_le_dernier_rapport_se_relit_depuis_le_fichier(self):
        self.controle()
        sc._ETAT["rapport"] = None
        relu = sc.dernier_rapport(self.rapports)
        self.assertIsNotNone(relu)
        self.assertEqual(relu["schema"], sc.SCHEMA_RAPPORT)


class TestRapportEtRoutes(BaseControle):

    def test_le_rapport_texte_ne_contient_ni_le_nom_de_session_ni_de_secret(self):
        absente = os.path.join(os.path.expanduser("~"), "Documents", "Kodo_POS", "db", "absente.db")
        r = sc.executer_controle(db_path=absente, ecrire=False)
        texte = sc.rendre_texte(r)
        self.assertNotIn(os.path.expanduser("~") + os.sep, texte)
        self.assertIn("~", texte)
        self.assertIn("CRITIQUE", texte)
        json.dumps(r)  # sérialisable tel quel pour l'API

    def test_routes_health_et_diagnostic(self):
        from kodo_core.api.routes import system_routes
        rapport = self.controle()
        sc._ETAT["rapport"] = rapport

        code, health = system_routes.handle_system_request("GET", "/api/system/health", {}, {})
        self.assertEqual(code, 200)
        self.assertEqual(health["controle_demarrage"]["statut"], "OK")
        self.assertNotIn("controles", health["controle_demarrage"], "le résumé de /health reste léger")

        code, diag = system_routes.handle_system_request("GET", "/api/system/diagnostic", {}, {})
        self.assertEqual(code, 200)
        self.assertTrue(diag["disponible"])
        self.assertEqual(len(diag["rapport"]["controles"]), len(sc.CONTROLES))
        self.assertIn("RAPPORT DE CONTRÔLE", diag["rapport_texte"])

    def test_route_de_relance(self):
        from kodo_core.api.routes import system_routes
        os.environ["KODO_STARTUP_CHECK"] = "0"
        code, res = system_routes.handle_system_request("POST", "/api/system/diagnostic/run", {}, {})
        self.assertEqual(code, 200)
        self.assertTrue(res["success"])
        self.assertFalse(res["demarre"])


class TestRevueDuFiltre(BaseControle):
    """Défauts du filet lui-même relevés par la revue du 2026-10-14 : chacun a un test."""

    def _ledger(self, n=3):
        """Journal de caisse correctement scellé (même primitive que l'application)."""
        from kodo_core.db import audit_trail
        conn = sqlite3.connect(self.db)
        try:
            c = conn.cursor()
            for i in range(n):
                dh = f"2026-10-0{i + 1} 10:00:00"
                sig, prec = audit_trail.signer_ledger(c, "VENTE", f"{10 + i}.50", "ESPECES", f"TCK-2026-{i + 1:04d}", dh)
                c.execute("INSERT INTO Ledger_Caisse (date_heure, type_mouvement, montant, methode_paiement, "
                          "reference, signature, hash_precedent) VALUES (?,?,?,?,?,?,?)",
                          (dh, "VENTE", f"{10 + i}.50", "ESPECES", f"TCK-2026-{i + 1:04d}", sig, prec))
            conn.commit()
        finally:
            conn.close()

    def _z(self, n=2):
        from kodo_core.db import audit_trail
        conn = sqlite3.connect(self.db)
        try:
            c = conn.cursor()
            for i in range(n):
                donnees = json.dumps({"total": 10 * (i + 1)})
                sig, prec = audit_trail.signer_rapport_z(c, f"2026-10-0{i + 1}", donnees)
                c.execute("INSERT INTO Rapports_Z (date, donnees_json, signature, hash_precedent) VALUES (?,?,?,?)",
                          (f"2026-10-0{i + 1}", donnees, sig, prec))
            conn.commit()
        finally:
            conn.close()

    def _clotures(self, n=3):
        """Vrais Z de caisse, scellés comme `enregistrer_cloture_caisse`."""
        from kodo_core.db import audit_trail
        conn = sqlite3.connect(self.db)
        try:
            prec = "GENESIS_Z_00000000000000000000000000000000"
            for i in range(n):
                tvac, esp, carte = 100.10 + i, 60.05 + i, 40.05
                date = f"2026-10-0{i + 1} 20:00:00"
                h = audit_trail.calculer_hash_cloture(prec, date, "POS-01", tvac, esp, carte)
                conn.execute("INSERT INTO Clotures_Caisse (date_cloture, caisse_id, total_ventes_tvac, total_especes, "
                             "total_carte, hash_precedent, current_hash, signature) VALUES (?,?,?,?,?,?,?,?)",
                             (date, "POS-01", tvac, esp, carte, prec, h, h))
                prec = h
            conn.commit()
        finally:
            conn.close()

    def nf525(self):
        return self.par_id(self.controle())["ventes.chaine_nf525"]

    def alterer(self, requete, params=()):
        """Altération « à la main » : un tiers qui retire d'abord les déclencheurs de protection."""
        for (nom,) in self.sql("SELECT name FROM sqlite_master WHERE type='trigger'"):
            self.sql(f'DROP TRIGGER "{nom}"')
        self.sql(requete, params)

    # -- le contrôle ne plante plus sur une altération : il la NOMME ---------------------------
    def test_chaines_saines_sont_ok(self):
        self.poser_cle_de_scellement()
        self._ledger()
        self._z()
        self._clotures()
        c = self.nf525()
        self.assertEqual(c["statut"], sc.OK, c["detail"])
        self.assertTrue(c["donnees"]["clotures_ok"])

    def test_journal_de_caisse_altere_est_critique_et_non_une_erreur(self):
        self.poser_cle_de_scellement()
        self._ledger()
        self.alterer("UPDATE Ledger_Caisse SET montant = '999.99' WHERE id = 2")
        r = self.controle()
        c = self.par_id(r)["ventes.chaine_nf525"]
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn("journal de caisse", c["detail"])
        self.assertEqual(r["statut"], "CRITIQUE", "une altération ne doit plus se perdre en statut global « OK »")
        self.assertEqual(r["resume"]["erreur"], 0)

    def test_journal_de_caisse_avec_montant_illisible_est_critique(self):
        self.poser_cle_de_scellement()
        self._ledger()
        self.alterer("UPDATE Ledger_Caisse SET montant = 'abc' WHERE id = 1")
        c = self.nf525()
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertFalse(c["donnees"]["ledger_ok"])

    def test_journal_des_z_altere_est_critique(self):
        self.poser_cle_de_scellement()
        self._z()
        self.sql("UPDATE Rapports_Z SET donnees_json = '{\"total\": 1}' WHERE id = 1")
        c = self.nf525()
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn("journal des Z", c["detail"])

    def test_la_cle_absente_n_empeche_pas_de_voir_un_journal_altere(self):
        self._ledger()
        self.ticket("TCK-2026-0001")  # sans clé : les tickets ne sont pas vérifiables, le journal l'est
        self.alterer("UPDATE Ledger_Caisse SET montant = '1.00' WHERE id = 3")
        c = self.nf525()
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn("journal de caisse", c["detail"])
        self.assertIn("tickets", c["detail"])  # signalés « non vérifiés »

    # -- les VRAIS Z (Clotures_Caisse) sont enfin vérifiés --------------------------------------
    def test_total_d_une_cloture_z_retouche_est_critique(self):
        self.poser_cle_de_scellement()
        self._clotures()
        self.sql("UPDATE Clotures_Caisse SET total_especes = 0.01 WHERE id = 2")
        c = self.nf525()
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertIn("clôtures Z", c["detail"])

    def test_cloture_z_supprimee_au_milieu_rompt_la_chaine(self):
        self.poser_cle_de_scellement()
        self._clotures()
        self.sql("DELETE FROM Clotures_Caisse WHERE id = 2")
        c = self.nf525()
        self.assertEqual(c["statut"], sc.CRITIQUE)
        self.assertFalse(c["donnees"]["clotures_ok"])

    def test_vraie_cloture_scellee_par_l_application_est_reconnue(self):
        # Pas de reconstitution à la main : le Z est scellé par la fonction de production.
        self.poser_cle_de_scellement()
        self.ticket("TCK-2026-0001")
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            from database_manager import enregistrer_cloture_caisse
            enregistrer_cloture_caisse(caisse_id="POS-01", fond_caisse_reel=None, conn=conn)
        finally:
            conn.close()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM Clotures_Caisse")[0][0], 1)
        c = self.nf525()
        self.assertTrue(c["donnees"]["clotures_ok"], c["detail"])

    # -- aucune clé n'est fabriquée, même quand le fichier existe mais est trop court ----------
    def test_cle_trop_courte_n_est_pas_regeneree(self):
        self.ticket("TCK-2026-0001")
        dossier = os.path.join(os.environ["HOME"], ".kodo_signing")
        os.makedirs(dossier)
        cle = os.path.join(dossier, "audit_hmac.key")
        with open(cle, "wb") as f:
            f.write(b"court")
        c = self.nf525()
        self.assertEqual(c["statut"], sc.ATTENTION)
        with open(cle, "rb") as f:
            self.assertEqual(f.read(), b"court", "le contrôle a réécrit la clé de scellement")

    # -- lecture seule au niveau du fichier ----------------------------------------------------
    def test_une_base_en_mode_delete_garde_son_en_tete(self):
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
        conn.close()
        with open(self.db, "rb") as f:
            entete = f.read(100)
        self.assertEqual(entete[18:20], b"\x01\x01", "précondition : base en mode journal DELETE")
        avant = _sha(self.db)
        self.controle()
        with open(self.db, "rb") as f:
            self.assertEqual(f.read(100)[18:20], b"\x01\x01", "le contrôle a basculé la base en mode WAL")
        self.assertEqual(_sha(self.db), avant)
        self.assertFalse(os.path.exists(self.db + "-wal"))

    def test_chemin_avec_espaces_diacritiques_et_diese(self):
        dossier = os.path.join(self.tmp, "Kōdo POS #1 ?x")
        os.makedirs(dossier)
        copie = os.path.join(dossier, "kodo pos.db")
        shutil.copy(self.db, copie)
        r = sc.executer_controle(db_path=copie, dossier_rapport=self.rapports, ecrire=False)
        self.assertNotEqual(r["statut"], "CRITIQUE", [(c["id"], c["detail"]) for c in r["controles"]])
        self.assertEqual(self.par_id(r)["base.integrite"]["statut"], sc.OK)

    def test_le_rapport_n_est_pas_ecrit_via_un_temporaire_fixe(self):
        self.controle()
        self.controle()
        self.assertEqual(sorted(os.listdir(self.rapports)), [sc.NOM_RAPPORT],
                         "un temporaire est resté dans le dossier des rapports")

    # -- numérotation : plages, empreinte complète, mémoire bornée -----------------------------
    def test_numero_aberrant_ne_consomme_pas_la_memoire(self):
        self.ticket("TCK-2026-0001")
        self.ticket("TCK-2026-99999999")
        debut = time.monotonic()
        c = self.par_id(self.controle())["ventes.numerotation"]
        self.assertLess(time.monotonic() - debut, 5)
        self.assertEqual(c["donnees"]["nombre"]["2026"], 99999997)
        self.assertEqual(len(c["donnees"]["manquants"]["2026"]), 20)

    def test_un_nouveau_trou_au_dela_des_20_premiers_est_vu(self):
        for n in range(1, 90, 2):  # 1,3,5…89 : 44 trous (> 20)
            self.ticket(f"TCK-2026-{n:04d}")
        self.controle()
        premier = self.par_id(self.controle())["ventes.numerotation"]
        self.assertEqual(premier["statut"], sc.INFO)
        for n in (91, 94):  # un trou de plus à la FIN : invisible dans les 20 premiers affichés
            self.ticket(f"TCK-2026-{n:04d}")
        r = self.controle()
        c = self.par_id(r)["ventes.numerotation"]
        self.assertEqual(c["statut"], sc.ATTENTION, "un trou nouveau au-delà des 20 premiers doit être signalé")
        self.assertIn("NOUVEAU", c["detail"])

    # -- Shopify -----------------------------------------------------------------------------
    def test_date_iso_avec_t_est_comprise(self):
        ancien = (datetime.datetime.now() - datetime.timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%S")
        self.sql("INSERT INTO sync_queue (id_vente_detail, id_ticket, code_barre, delta, statut, tentatives, "
                 "derniere_erreur, date_creation, date_maj) VALUES (1, 1, 'X', -1, 'PENDING', 12, "
                 "'HTTP 401', ?, ?)", (ancien, ancien))
        c = self.par_id(self.controle())["shopify.file"]
        self.assertEqual(c["statut"], sc.ATTENTION)
        self.assertGreaterEqual(c["donnees"]["age_heures"], 59)

    def test_mouvements_ecartes_pour_sku_introuvable_sont_dits(self):
        maintenant = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.sql("INSERT INTO sync_queue (id_vente_detail, id_ticket, code_barre, delta, statut, "
                 "derniere_erreur, date_creation, date_maj) VALUES (1, 1, 'X', -1, 'IGNORE', "
                 "'SKU introuvable sur Shopify', ?, ?)", (maintenant, maintenant))
        c = self.par_id(self.controle())["shopify.file"]
        self.assertEqual(c["statut"], sc.INFO)
        self.assertEqual(c["donnees"]["ignores_sku_introuvable"], 1)
        self.assertIn("introuvable", c["detail"])

    # -- ordre et budget -----------------------------------------------------------------------
    def test_la_chaine_nf525_passe_en_dernier(self):
        self.assertEqual(sc.CONTROLES[-1][0], "ventes.chaine_nf525")

    def test_le_resume_court_porte_les_controles_non_executes(self):
        with mock.patch.object(sc, "BUDGET_SECONDES", -1.0):
            r = self.controle()
        court = sc.resume_court(r)
        self.assertEqual(court["statut"], "ATTENTION")
        self.assertTrue(court["non_executes"])


if __name__ == "__main__":
    unittest.main()
