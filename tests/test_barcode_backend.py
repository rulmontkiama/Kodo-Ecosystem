# -*- coding: utf-8 -*-
"""
Tests dédiés au back-end code-barres de Kōdo POS (agent `barcode-qa`).

ISOLATION — rien de ce fichier ne doit toucher la machine réelle :
  * `HOME` est redirigé vers un dossier jetable (le conftest le fait déjà ; on le
    re-vérifie ici, car ce fichier peut être exécuté seul).
  * `KODO_DB_PATH` pointe sur un fichier temporaire AVANT tout import de `kodo_core`,
    puis sur une base neuve à chaque test.
  * Aucune impression : `subprocess` est neutralisé dans les tests matériel, et le
    spouleur est instancié à part (jamais le singleton `get_print_worker()`).
  * Aucun accès à `kodo_pos.db` à la racine, à `~/Documents/Kodo_POS/` ni à
    `/Applications/Kodo_POS.app`.
"""

import os
import sys
import shutil
import sqlite3
import tempfile
import unittest

# --- Isolation AVANT tout import de kodo_core / database_manager -------------------
_RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _RACINE not in sys.path:
    sys.path.insert(0, _RACINE)

if not os.environ.get("HOME", "").startswith(tempfile.gettempdir()):
    # Exécution hors pytest (ou conftest non chargé) : on redirige quand même.
    os.environ["HOME"] = tempfile.mkdtemp(prefix="kodo_barcode_home_")

_DB_AMORCE = os.path.join(tempfile.mkdtemp(prefix="kodo_barcode_boot_"), "amorce.db")
os.environ["KODO_DB_PATH"] = _DB_AMORCE

import database_manager  # noqa: E402
from kodo_core.db import migrations as kodo_migrations  # noqa: E402
from kodo_core.db.migrations import MigrationManager  # noqa: E402
from kodo_core.domain.catalog.inventory_manager import (  # noqa: E402
    BarcodeConflictError,
    InventoryManager,
)
from kodo_core.hardware import pdf as kodo_pdf  # noqa: E402
from kodo_core.hardware import printer as kodo_printer  # noqa: E402
from kodo_core.hardware import print_worker as kodo_print_worker  # noqa: E402
from kodo_core.api.app import kodo_app  # noqa: E402
from kodo_core.api.routes.products_routes import (  # noqa: E402
    _resoudre_lignes_etiquettes,
    MAX_ETIQUETTES_PAR_DEMANDE,
)


# Deux EAN-13 réels, clés de contrôle vérifiées à la main.
EAN_VALIDE_A = "2000000000008"
EAN_VALIDE_B = "4006381333931"
# Troisième code réel distinct : chaque cas de nettoyage doit viser un code libre,
# sinon c'est le conflit d'unicité qui est testé, pas le nettoyage.
EAN_VALIDE_C = "5449000000996"
# Même base que EAN_VALIDE_A, mais dernier chiffre faux : c'est le code qui, confié tel
# quel à ReportLab, se ferait réécrire en EAN_VALIDE_A à l'impression.
EAN_CLE_FAUSSE = "2000000000000"


def _chemin_interdit(chemin: str) -> bool:
    """Vrai si `chemin` sort de la zone jetable des tests."""
    reel = os.path.realpath(chemin)
    zones_sures = (
        os.path.realpath(tempfile.gettempdir()),
        os.path.realpath(os.environ.get("HOME", tempfile.gettempdir())),
    )
    return not any(reel.startswith(z) for z in zones_sures)


class BaseBarcodeTest(unittest.TestCase):
    """Base neuve et isolée pour chaque test."""

    def setUp(self):
        self.dossier = tempfile.mkdtemp(prefix="kodo_barcode_")
        self.db_path = os.path.join(self.dossier, "test_kodo.db")
        self.assertFalse(_chemin_interdit(self.db_path),
                         "La base de test doit rester dans un dossier temporaire.")
        self._ancien_db_name = database_manager.DB_NAME
        database_manager.DB_NAME = self.db_path
        os.environ["KODO_DB_PATH"] = self.db_path
        database_manager.initialiser_db()

    def tearDown(self):
        database_manager.DB_NAME = self._ancien_db_name
        os.environ["KODO_DB_PATH"] = _DB_AMORCE
        shutil.rmtree(self.dossier, ignore_errors=True)

    # -- utilitaires ---------------------------------------------------------------
    def creer_produit(self, nom, code=None, prix=19.90, tailles="S:4|M:6"):
        charge = {"name": nom, "category": "Tests", "price": prix, "sizes": tailles}
        if code is not None:
            charge["barcode"] = code
        res = InventoryManager.save_product(charge)
        return int(res["product_id"])

    def code_en_base(self, pid):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT code_barre FROM Produits WHERE id=?", (pid,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None


# =====================================================================================
# 1. GÉNÉRATION
# =====================================================================================
class TestGenerationCodeBarres(BaseBarcodeTest):

    def test_cle_ean13_conforme_a_la_reference_gs1(self):
        """La clé calculée doit correspondre à des EAN-13 réels connus."""
        self.assertEqual(InventoryManager.ean13_check_digit("400638133393"), "1")
        self.assertEqual(InventoryManager.ean13_check_digit("590123412345"), "7")
        self.assertEqual(InventoryManager.ean13_check_digit("200000000000"), "8")
        self.assertTrue(InventoryManager.is_valid_ean13(EAN_VALIDE_B))
        self.assertFalse(InventoryManager.is_valid_ean13(EAN_CLE_FAUSSE))
        self.assertFalse(InventoryManager.is_valid_ean13("20000000000"))   # 11 chiffres
        self.assertFalse(InventoryManager.is_valid_ean13("200000000000A"))

    def test_code_genere_prefixe_200_et_cle_valide(self):
        """Chaque tirage doit être un EAN-13 interne conforme, jamais approximatif."""
        for _ in range(200):
            code = InventoryManager.generate_internal_barcode()
            self.assertEqual(len(code), 13)
            self.assertTrue(code.startswith("200"), f"préfixe interne attendu : {code}")
            self.assertTrue(code.isdigit())
            self.assertTrue(InventoryManager.is_valid_ean13(code),
                            f"clé de contrôle fausse sur {code}")

    def test_unicite_garantie_malgre_une_collision_forcee(self):
        """Un code déjà pris en base ne doit jamais être re-tiré : on retire jusqu'à un libre."""
        pris = "2001111111119"
        self.assertTrue(InventoryManager.is_valid_ean13(pris))
        self.creer_produit("Article occupant le code", code=pris)

        libre = "2002222222220"
        self.assertTrue(InventoryManager.is_valid_ean13(libre))

        tirages = [pris, pris, libre]
        original = InventoryManager.generate_internal_barcode.__func__

        def faux_tirage(cls):
            return tirages.pop(0) if tirages else original(cls)

        InventoryManager.generate_internal_barcode = classmethod(faux_tirage)
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                obtenu = InventoryManager.generate_unique_internal_barcode(conn)
            finally:
                conn.close()
        finally:
            InventoryManager.generate_internal_barcode = classmethod(original)

        self.assertEqual(obtenu, libre,
                         "le générateur a rendu un code déjà attribué en base")
        self.assertEqual(tirages, [], "les deux collisions auraient dû être consommées")

    def test_generation_refuse_de_travailler_hors_transaction(self):
        """Sans connexion, le contrôle d'unicité serait fait ailleurs que l'écriture."""
        with self.assertRaises(ValueError):
            InventoryManager.generate_unique_internal_barcode(None)

    def test_generation_abandonne_plutot_que_de_rendre_un_doublon(self):
        """Si tous les tirages collisionnent, on échoue : on ne rend jamais un code pris."""
        pris = "2003333333331"
        self.assertTrue(InventoryManager.is_valid_ean13(pris))
        self.creer_produit("Occupant", code=pris)

        original = InventoryManager.generate_internal_barcode.__func__
        InventoryManager.generate_internal_barcode = classmethod(lambda cls: pris)
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                with self.assertRaises(RuntimeError):
                    InventoryManager.generate_unique_internal_barcode(conn)
            finally:
                conn.close()
        finally:
            InventoryManager.generate_internal_barcode = classmethod(original)


# =====================================================================================
# 2. ATTRIBUTION
# =====================================================================================
class TestAttributionCodeBarres(BaseBarcodeTest):

    def test_article_introuvable(self):
        with self.assertRaises(LookupError):
            InventoryManager.assign_barcode(999999)

    def test_attribution_interne_sur_article_sans_code(self):
        pid = self.creer_produit("Chemise en lin")
        res = InventoryManager.assign_barcode(pid)
        self.assertTrue(res["generated"])
        self.assertIsNone(res["previous_barcode"])
        self.assertTrue(InventoryManager.is_valid_ean13(res["barcode"]))
        self.assertEqual(self.code_en_base(pid), res["barcode"],
                         "le code annoncé doit être celui réellement écrit en base")

    def test_article_ayant_deja_un_code_sans_overwrite(self):
        """Un code déjà imprimé sur des étiquettes ne se remplace pas par accident."""
        pid = self.creer_produit("Jupe plissée", code=EAN_VALIDE_B)
        with self.assertRaises(BarcodeConflictError) as ctx:
            InventoryManager.assign_barcode(pid)
        self.assertEqual(ctx.exception.code, "BARCODE_ALREADY_SET")
        self.assertEqual(ctx.exception.details.get("product_id"), pid)
        self.assertEqual(ctx.exception.details.get("barcode"), EAN_VALIDE_B)
        self.assertEqual(self.code_en_base(pid), EAN_VALIDE_B, "le code d'origine a bougé")

    def test_code_impose_deja_pris_par_un_autre_article(self):
        pid_a = self.creer_produit("Article A", code=EAN_VALIDE_B)
        pid_b = self.creer_produit("Article B")
        with self.assertRaises(BarcodeConflictError) as ctx:
            InventoryManager.assign_barcode(pid_b, barcode=EAN_VALIDE_B)
        self.assertEqual(ctx.exception.code, "BARCODE_TAKEN")
        self.assertEqual(ctx.exception.details.get("product_id"), pid_a,
                         "le conflit doit désigner l'article qui détient déjà le code")
        self.assertIsNone(self.code_en_base(pid_b))
        self.assertEqual(self.code_en_base(pid_a), EAN_VALIDE_B)

    def test_overwrite_remplace_et_rend_l_ancien_code(self):
        pid = self.creer_produit("Manteau", code=EAN_VALIDE_B)
        res = InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_A, overwrite=True)
        self.assertEqual(res["barcode"], EAN_VALIDE_A)
        self.assertFalse(res["generated"])
        self.assertEqual(res["previous_barcode"], EAN_VALIDE_B)
        self.assertEqual(self.code_en_base(pid), EAN_VALIDE_A)

    def test_overwrite_regenere_un_code_interne(self):
        pid = self.creer_produit("Pull", code=EAN_VALIDE_B)
        res = InventoryManager.assign_barcode(pid, overwrite=True)
        self.assertTrue(res["generated"])
        self.assertNotEqual(res["barcode"], EAN_VALIDE_B)
        self.assertTrue(InventoryManager.is_valid_ean13(res["barcode"]))

    def test_code_impose_a_cle_fausse_refuse(self):
        """Une clé fausse ne doit jamais entrer en base : elle serait réécrite à l'impression."""
        pid = self.creer_produit("Ceinture")
        with self.assertRaises(ValueError) as ctx:
            InventoryManager.assign_barcode(pid, barcode=EAN_CLE_FAUSSE)
        self.assertNotIsInstance(ctx.exception, BarcodeConflictError)
        self.assertIn("8", str(ctx.exception), "le message doit annoncer la clé attendue")
        self.assertIsNone(self.code_en_base(pid))

    def test_code_impose_vide_refuse(self):
        pid = self.creer_produit("Écharpe")
        with self.assertRaises(ValueError):
            InventoryManager.assign_barcode(pid, barcode="   ")
        self.assertIsNone(self.code_en_base(pid))

    def test_route_api_attribution_et_conflits(self):
        pid = self.creer_produit("Sac cabas")

        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/barcode", {}, {}, {"product_id": pid})
        self.assertEqual(code, 200, corps)
        attribue = corps["barcode"]
        self.assertTrue(InventoryManager.is_valid_ean13(attribue))

        # Deuxième appel sans overwrite : 409 explicite, pas un 500 SQL.
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/barcode", {}, {}, {"product_id": pid})
        self.assertEqual(code, 409, corps)
        self.assertEqual(corps["code"], "BARCODE_ALREADY_SET")

        # Article inexistant.
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/barcode", {}, {}, {"product_id": 987654})
        self.assertEqual(code, 404, corps)

        # Code imposé invalide.
        pid2 = self.creer_produit("Bonnet")
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/barcode", {}, {},
            {"product_id": pid2, "barcode": EAN_CLE_FAUSSE})
        self.assertEqual(code, 422, corps)
        self.assertEqual(corps["code"], "BARCODE_INVALID")

        # Résolution par la route de scan.
        code, corps, _ = kodo_app.handle_request(
            "GET", "/api/products/barcode/resolve", {"code": [attribue]}, {}, {})
        self.assertEqual(code, 200, corps)
        self.assertEqual(int(corps["product"]["product_id"]), pid)

        code, corps, _ = kodo_app.handle_request(
            "GET", "/api/products/barcode/resolve", {"code": ["2009999999992"]}, {}, {})
        self.assertEqual(code, 404, corps)


# =====================================================================================
# 3. LE DÉFAUT QUI TUE LA FONCTIONNALITÉ : la modification d'article efface le code
# =====================================================================================
class TestPreservationDuCodeALaModification(BaseBarcodeTest):

    def test_modifier_un_article_sans_renvoyer_le_champ_preserve_le_code(self):
        """
        Régression historique : un code attribué puis imprimé sur une étiquette
        disparaissait de la base dès la première modification de prix.
        """
        pid = self.creer_produit("Robe portefeuille", prix=79.90)
        attribue = InventoryManager.assign_barcode(pid)["barcode"]
        self.assertEqual(self.code_en_base(pid), attribue)

        # Charge SANS aucune clé code-barres : l'écran n'a modifié que le prix.
        InventoryManager.save_product({
            "id": pid,
            "name": "Robe portefeuille",
            "category": "Tests",
            "price": 69.90,
            "sizes": "S:4|M:6",
        })

        self.assertEqual(self.code_en_base(pid), attribue,
                         "le code-barres a été effacé par une simple modification de prix")
        produit = InventoryManager.get_product_by_id(pid)
        self.assertEqual(produit["barcode"], attribue)
        self.assertAlmostEqual(produit["price"], 69.90, places=2,
                               msg="le reste de la modification doit bien avoir été appliqué")

        # L'article reste retrouvable à la douchette.
        retrouve = InventoryManager.get_product_by_barcode(attribue)
        self.assertIsNotNone(retrouve)
        self.assertEqual(int(retrouve["product_id"]), pid)

    def test_champ_vide_explicite_efface_volontairement_le_code(self):
        pid = self.creer_produit("Tunique")
        InventoryManager.assign_barcode(pid)
        self.assertIsNotNone(self.code_en_base(pid))

        InventoryManager.save_product({
            "id": pid,
            "name": "Tunique",
            "category": "Tests",
            "price": 39.90,
            "sizes": "S:4|M:6",
            "barcode": "",
        })
        self.assertIsNone(self.code_en_base(pid),
                          "un champ code-barres vide est un effacement demandé")

    def test_effacement_puis_recreation_de_deux_articles_sans_code(self):
        """Deux articles sans code-barres doivent coexister malgré la contrainte UNIQUE."""
        pid_a = self.creer_produit("Sans code A", code="")
        pid_b = self.creer_produit("Sans code B", code="")
        self.assertIsNone(self.code_en_base(pid_a))
        self.assertIsNone(self.code_en_base(pid_b))

    def test_modification_via_la_route_api_preserve_le_code(self):
        pid = self.creer_produit("Blouse", prix=49.0)
        attribue = InventoryManager.assign_barcode(pid)["barcode"]

        code, corps, _ = kodo_app.handle_request("POST", "/api/products", {}, {}, {
            "id": pid, "name": "Blouse", "category": "Tests",
            "price": 45.0, "sizes": "S:4|M:6",
        })
        self.assertEqual(code, 200, corps)
        self.assertEqual(self.code_en_base(pid), attribue,
                         "la route de sauvegarde a effacé le code-barres")


# =====================================================================================
# 4. PAS D'ÉCRASEMENT SILENCIEUX
# =====================================================================================
class TestPasDEcrasementSilencieux(BaseBarcodeTest):

    def test_creer_un_article_avec_un_code_deja_pris_leve_un_conflit(self):
        pid_a = self.creer_produit("Original", code=EAN_VALIDE_B, prix=99.0,
                                   tailles="S:3|M:7")

        with self.assertRaises(BarcodeConflictError) as ctx:
            InventoryManager.save_product({
                "name": "Intrus",
                "category": "Tests",
                "price": 5.0,
                "sizes": "XL:1",
                "barcode": EAN_VALIDE_B,
            })
        self.assertEqual(ctx.exception.code, "BARCODE_TAKEN")
        self.assertEqual(ctx.exception.details.get("product_id"), pid_a)

        # L'article d'origine doit être STRICTEMENT intact.
        origine = InventoryManager.get_product_by_id(pid_a)
        self.assertEqual(origine["name"], "Original")
        self.assertAlmostEqual(origine["price"], 99.0, places=2)
        self.assertEqual(origine["barcode"], EAN_VALIDE_B)
        self.assertEqual(origine["stock"], 10)
        self.assertEqual({s["size"]: s["quantity"] for s in origine["stocks"]},
                         {"S": 3, "M": 7})

        # Et l'intrus ne doit pas exister.
        conn = sqlite3.connect(self.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM Produits WHERE nom='Intrus'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, "l'article en conflit a tout de même été créé")

    def test_editer_un_article_avec_le_code_d_un_autre_leve_un_conflit(self):
        pid_a = self.creer_produit("Détenteur", code=EAN_VALIDE_B)
        pid_b = self.creer_produit("Candidat", code=EAN_VALIDE_A)

        with self.assertRaises(BarcodeConflictError) as ctx:
            InventoryManager.save_product({
                "id": pid_b, "name": "Candidat", "category": "Tests",
                "price": 10.0, "sizes": "S:1", "barcode": EAN_VALIDE_B,
            })
        self.assertEqual(ctx.exception.code, "BARCODE_TAKEN")
        self.assertEqual(self.code_en_base(pid_a), EAN_VALIDE_B)
        self.assertEqual(self.code_en_base(pid_b), EAN_VALIDE_A)


# =====================================================================================
# 5. NETTOYAGE DES CODES ENTRANTS
# =====================================================================================
class TestNettoyageDesCodes(BaseBarcodeTest):

    def test_clean_barcode_sur_valeurs_non_textuelles_et_bruitees(self):
        cb = InventoryManager.clean_barcode
        self.assertEqual(cb(2000000000008), EAN_VALIDE_A)           # nombre JSON
        self.assertEqual(cb(f"{EAN_VALIDE_A}\r\n"), EAN_VALIDE_A)   # retour de douchette
        self.assertEqual(cb(f"  {EAN_VALIDE_A}  "), EAN_VALIDE_A)   # espaces
        self.assertEqual(cb(f"{EAN_VALIDE_A}\x1d"), EAN_VALIDE_A)   # séparateur GS1
        self.assertIsNone(cb(""))
        self.assertIsNone(cb("   "))
        self.assertIsNone(cb("\r\n"))
        self.assertIsNone(cb(None))
        self.assertIsNone(cb(True), "un booléen n'est pas un code-barres")
        self.assertIsNone(cb({"code": "x"}))

    def test_aucune_chaine_vide_en_base_via_save_product(self):
        for valeur in ("", "   ", "\r\n", "\t"):
            pid = self.creer_produit(f"Article {valeur!r}", code=valeur)
            self.assertIsNone(self.code_en_base(pid),
                              f"une chaîne vide {valeur!r} a été stockée telle quelle")

    def test_code_numerique_et_bruite_via_la_route_api(self):
        """Aucune de ces formes ne doit produire d'erreur 500 ni de chaîne vide."""
        cas = [
            ("nombre JSON", 2000000000008, EAN_VALIDE_A),
            ("retour chariot", f"{EAN_VALIDE_B}\r", EAN_VALIDE_B),
            ("espaces", f"  {EAN_VALIDE_C}  ", EAN_VALIDE_C),
        ]
        for libelle, entree, attendu in cas:
            with self.subTest(libelle):
                pid = self.creer_produit(f"Article {libelle}")
                code, corps, _ = kodo_app.handle_request(
                    "POST", "/api/products/barcode", {}, {},
                    {"product_id": pid, "barcode": entree})
                self.assertEqual(code, 200, f"{libelle} : {corps}")
                self.assertEqual(corps["barcode"], attendu)
                self.assertEqual(self.code_en_base(pid), attendu)
                # Et la douchette retrouve bien l'article.
                retrouve = InventoryManager.get_product_by_barcode(f"{attendu}\r")
                self.assertIsNotNone(retrouve, f"{libelle} : article introuvable au scan")
                self.assertEqual(int(retrouve["product_id"]), pid)

    def test_code_vide_via_la_route_api_est_refuse_proprement(self):
        pid = self.creer_produit("Article code vide")
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/barcode", {}, {}, {"product_id": pid, "barcode": "   "})
        self.assertNotEqual(code, 500, corps)
        # Chaîne blanche : traitée comme « pas de code imposé » ou refusée, mais
        # JAMAIS stockée telle quelle.
        self.assertNotEqual(self.code_en_base(pid), "   ")
        self.assertNotEqual(self.code_en_base(pid), "")

    def test_route_de_resolution_sans_code(self):
        code, corps, _ = kodo_app.handle_request(
            "GET", "/api/products/barcode/resolve", {"code": ["   "]}, {}, {})
        self.assertEqual(code, 400, corps)


# =====================================================================================
# 6. RENDU : on n'imprime jamais autre chose que le code en base
# =====================================================================================
class TestRenduEtiquette(BaseBarcodeTest):

    def _pdf(self, nom="etiquettes.pdf"):
        return os.path.join(self.dossier, nom)

    def test_reportlab_reecrirait_bien_la_cle_de_controle(self):
        """
        Justifie le refus : présenté à un EAN-13 à clé fausse, ReportLab n'encode que
        les 12 premiers chiffres et recalcule la clé. Le symbole imprimé serait donc
        DIFFÉRENT du code enregistré. Ce test documente le piège : s'il venait à
        disparaître, le refus ci-dessous n'aurait plus de raison d'être.
        """
        from reportlab.graphics.barcode import createBarcodeDrawing
        d = createBarcodeDrawing("EAN13", value=EAN_CLE_FAUSSE, width=160, height=40,
                                 humanReadable=False)
        self.assertEqual(d.contents[0].value, EAN_CLE_FAUSSE[:12],
                         "ReportLab ne conserve pas les 13 chiffres fournis")

    def test_cle_fausse_jamais_imprimee_reecrite(self):
        with self.assertRaises(ValueError) as ctx:
            kodo_pdf.build_barcode_drawing("EAN13", EAN_CLE_FAUSSE, width=200, height=40)
        self.assertNotIsInstance(ctx.exception, kodo_pdf.BarcodeTropEtroitError)

        # Le repli tolérant ne doit pas non plus produire un symbole : Drawing vide.
        d = kodo_pdf.generate_barcode_drawing("EAN13", EAN_CLE_FAUSSE, width=200, height=40)
        self.assertEqual(len(d.contents), 0,
                         "un code à clé fausse a produit un symbole imprimable")

    def test_code_valide_encode_exactement_la_valeur_en_base(self):
        d, meta = kodo_pdf.build_barcode_drawing("EAN13", EAN_VALIDE_B, width=200, height=40)
        self.assertEqual(meta["symbologie"], "EAN13")
        self.assertEqual(meta["valeur"], EAN_VALIDE_B)
        widget = d.contents[0]
        # ReportLab ne garde que les 12 chiffres de données ; la clé qu'il recalcule
        # doit être celle du code enregistré, sinon le symbole diverge de la base.
        self.assertEqual(widget.value, EAN_VALIDE_B[:12])
        self.assertEqual(kodo_pdf.ean13_cle_controle(widget.value), EAN_VALIDE_B[12])

    def test_article_sans_code_ne_produit_jamais_000000000000(self):
        appels = []
        original = kodo_pdf.build_barcode_drawing

        def espion(*a, **kw):
            appels.append((a, kw))
            return original(*a, **kw)

        kodo_pdf.build_barcode_drawing = espion
        try:
            rendu = kodo_pdf.generer_etiquettes_lot_pdf(
                [{"name": "Article sans code", "barcode": "", "size": "M",
                  "price": 10, "price_sale": None, "quantity": 1}],
                self._pdf(),
                largeur_mm=60, hauteur_mm=35, marge_mm=2,
            )
        finally:
            kodo_pdf.build_barcode_drawing = original

        self.assertEqual(appels, [], "un code-barres a été dessiné pour un article sans code")
        self.assertTrue(rendu["avertissements"], "l'absence de code doit être signalée")
        self.assertTrue(os.path.exists(rendu["path"]))
        with open(rendu["path"], "rb") as fh:
            contenu = fh.read()
        self.assertNotIn(b"000000000000", contenu,
                         "le code de repli historique est réapparu sur l'étiquette")

    def test_format_trop_etroit_refuse(self):
        with self.assertRaises(kodo_pdf.BarcodeTropEtroitError):
            kodo_pdf.generer_etiquettes_lot_pdf(
                [{"name": "Article", "barcode": EAN_VALIDE_B, "size": "",
                  "price": 10, "price_sale": None, "quantity": 1}],
                self._pdf("etroit.pdf"),
                largeur_mm=20, hauteur_mm=35, marge_mm=2,
            )
        # Aucun PDF exploitable ne doit avoir été livré à la commerçante.
        chemin = self._pdf("etroit.pdf")
        self.assertFalse(os.path.exists(chemin) and os.path.getsize(chemin) > 0,
                         "un PDF a été produit malgré le refus")

    def test_largeur_suffisante_produit_bien_une_etiquette(self):
        mini = kodo_pdf.largeur_mini_ean13_mm()
        self.assertGreater(mini, 30.0)
        rendu = kodo_pdf.generer_etiquettes_lot_pdf(
            [{"name": "Article", "barcode": EAN_VALIDE_B, "size": "M",
              "price": 10, "price_sale": None, "quantity": 3}],
            self._pdf("large.pdf"),
            largeur_mm=mini + 5, hauteur_mm=35, marge_mm=2,
        )
        self.assertEqual(rendu["pages"], 3, "une étiquette par exemplaire demandé")
        self.assertTrue(os.path.getsize(rendu["path"]) > 0)

    def test_route_pdf_refuse_un_article_sans_code_barres(self):
        """Arbitrage 1 : on n'invente jamais un code pour imprimer quand même."""
        self._configurer_etiqueteuse()
        pid = self.creer_produit("Article sans code")
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/labels/pdf", {}, {},
            {"items": [{"product_id": pid, "quantity": 1}]})
        self.assertEqual(code, 422, corps)
        self.assertEqual(corps["code"], "LABEL_REQUEST_INVALID")
        self.assertIn("code-barres", corps["error"].lower())

    def test_route_pdf_produit_un_pdf_pour_un_article_code(self):
        self._configurer_etiqueteuse()
        pid = self.creer_produit("Article étiquetable", tailles="M:2")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        code, corps, entetes = kodo_app.handle_request(
            "POST", "/api/products/labels/pdf", {}, {},
            {"items": [{"product_id": pid, "size": "M", "quantity": 2}]})
        self.assertEqual(code, 200, corps)
        self.assertIsInstance(corps, bytes)
        self.assertTrue(corps.startswith(b"%PDF"))
        self.assertEqual(entetes.get("Content-Type"), "application/pdf")

    def test_route_pdf_refuse_sans_format_configure(self):
        pid = self.creer_produit("Article")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/labels/pdf", {}, {},
            {"items": [{"product_id": pid, "quantity": 1}]})
        self.assertEqual(code, 409, corps)
        self.assertEqual(corps["code"], "LABEL_FORMAT_NOT_CONFIGURED")

    def test_route_impression_refuse_sans_etiqueteuse(self):
        """Arbitrage 5 : aucun format par défaut inventé, aucune impression à l'aveugle."""
        pid = self.creer_produit("Article")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        code, corps, _ = kodo_app.handle_request(
            "POST", "/api/products/labels/print", {}, {},
            {"items": [{"product_id": pid, "quantity": 1}]})
        self.assertEqual(code, 409, corps)
        self.assertEqual(corps["code"], "LABEL_PRINTER_NOT_CONFIGURED")

    # -- utilitaire ----------------------------------------------------------------
    def _configurer_etiqueteuse(self, largeur="60", hauteur="35"):
        conn = sqlite3.connect(self.db_path)
        try:
            for cle, val in (("label_printer_name", "ETIQUETEUSE_FICTIVE"),
                             ("label_width_mm", largeur),
                             ("label_height_mm", hauteur),
                             ("label_margin_mm", "2")):
                conn.execute("INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                             (cle, val))
            conn.commit()
        finally:
            conn.close()


# =====================================================================================
# 6b. RÉSOLUTION DES LIGNES D'ÉTIQUETTES (préparation de la demande écran)
# =====================================================================================
class TestResolutionLignesEtiquettes(BaseBarcodeTest):
    """
    `_resoudre_lignes_etiquettes` est le point de passage commun aux deux routes
    d'étiquetage (aperçu PDF et impression) : c'est là que la demande de l'écran est
    confrontée à l'état réel du stock. Directement testée ici, sans passer par le
    rendu PDF, pour isoler les erreurs de logique des erreurs de mise en page.
    """

    def test_sans_taille_ni_quantite_une_ligne_par_declinaison_en_stock(self):
        pid = self.creer_produit("Article multi-tailles", tailles="S:4|M:6")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        lignes = _resoudre_lignes_etiquettes([{"product_id": pid}])
        par_taille = {l["size"]: l["quantity"] for l in lignes}
        self.assertEqual(par_taille, {"S": 4, "M": 6},
                         "chaque déclinaison en stock doit produire sa propre ligne, "
                         "à raison d'une étiquette par exemplaire en stock")

    def test_taille_demandee_hors_catalogue_leve_une_erreur_nommee(self):
        pid = self.creer_produit("Article", tailles="S:4|M:6")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        with self.assertRaises(ValueError) as ctx:
            _resoudre_lignes_etiquettes([{"product_id": pid, "size": "XXL"}])
        self.assertIn("XXL", str(ctx.exception))

    def test_quantite_explicite_prevaut_sur_le_stock(self):
        pid = self.creer_produit("Article", tailles="M:6")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        lignes = _resoudre_lignes_etiquettes(
            [{"product_id": pid, "size": "M", "quantity": 2}])
        self.assertEqual(lignes[0]["quantity"], 2,
                         "une quantité explicitement demandée ne doit pas être "
                         "remplacée par la quantité en stock")

    def test_taille_unique_normalisee_en_absence_de_declinaison(self):
        pid = self.creer_produit("Article taille unique", tailles="Default Title:5")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        lignes = _resoudre_lignes_etiquettes([{"product_id": pid}])
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]["size"], "",
                         "un libellé de taille unique ne doit jamais apparaître "
                         "littéralement sur l'étiquette")

    def test_demande_trop_volumineuse_est_refusee(self):
        pid = self.creer_produit("Article", tailles="M:1")
        InventoryManager.assign_barcode(pid, barcode=EAN_VALIDE_B)
        with self.assertRaises(ValueError) as ctx:
            _resoudre_lignes_etiquettes(
                [{"product_id": pid, "size": "M",
                  "quantity": MAX_ETIQUETTES_PAR_DEMANDE + 1}])
        self.assertIn(str(MAX_ETIQUETTES_PAR_DEMANDE), str(ctx.exception))

    def test_code_barre_a_cle_fausse_en_base_refuse_la_ligne(self):
        """
        Un code invalide n'a normalement pas pu être écrit via `assign_barcode`,
        mais une base migrée depuis une ancienne version peut en contenir un :
        la préparation de l'étiquette doit s'en apercevoir plutôt que de laisser
        le moteur PDF réécrire silencieusement un code différent.
        """
        pid = self.creer_produit("Article", code=EAN_CLE_FAUSSE, tailles="M:1")
        with self.assertRaises(ValueError) as ctx:
            _resoudre_lignes_etiquettes([{"product_id": pid, "size": "M"}])
        self.assertIn(EAN_CLE_FAUSSE, str(ctx.exception))

    def test_article_inexistant_leve_une_erreur_nommee(self):
        with self.assertRaises(ValueError) as ctx:
            _resoudre_lignes_etiquettes([{"product_id": 999999}])
        self.assertIn("existe plus", str(ctx.exception))

    def test_liste_vide_refusee(self):
        with self.assertRaises(ValueError):
            _resoudre_lignes_etiquettes([])
        with self.assertRaises(ValueError):
            _resoudre_lignes_etiquettes(None)


# =====================================================================================
# 6c. RÉGLAGES DE L'ÉTIQUETEUSE (écriture partielle, jamais de valeur inventée)
# =====================================================================================
class TestReglagesEtiquette(BaseBarcodeTest):

    def _get(self):
        return kodo_app.handle_request("GET", "/api/labels/settings", {}, {}, {})

    def _post(self, data):
        return kodo_app.handle_request("POST", "/api/labels/settings", {}, {}, data)

    def test_non_configuree_par_defaut(self):
        code, corps, _ = self._get()
        self.assertEqual(code, 200, corps)
        self.assertFalse(corps["settings"]["est_configuree"])
        self.assertIn("label_printer_name", corps["settings"]["reglages_manquants"])

    def test_ecriture_partielle_ne_touche_pas_aux_autres_reglages(self):
        code, corps, _ = self._post({"labelPrinterName": "DYMO_450"})
        self.assertEqual(code, 200, corps)
        code, corps, _ = self._post({"labelWidthMm": "57"})
        self.assertEqual(code, 200, corps)
        code, corps, _ = self._get()
        self.assertEqual(corps["settings"]["label_printer_name"], "DYMO_450",
                         "un réglage déjà enregistré ne doit pas être effacé par "
                         "une requête qui ne porte que sur un autre champ")
        self.assertEqual(corps["settings"]["label_width_mm"], "57.0")

    def test_chaine_vide_explicite_efface_le_reglage(self):
        self._post({"labelPrinterName": "DYMO_450"})
        code, corps, _ = self._post({"labelPrinterName": ""})
        self.assertEqual(code, 200, corps)
        self.assertEqual(corps["settings"]["label_printer_name"], "",
                         "une chaîne vide explicitement envoyée est une demande "
                         "d'effacement, pas un oubli à ignorer")

    def test_largeur_non_numerique_refusee(self):
        code, corps, _ = self._post({"labelWidthMm": "large"})
        self.assertEqual(code, 400, corps)

    def test_largeur_negative_ou_nulle_refusee(self):
        code, corps, _ = self._post({"labelWidthMm": "0"})
        self.assertEqual(code, 400, corps)
        code, corps, _ = self._post({"labelWidthMm": "-5"})
        self.assertEqual(code, 400, corps)

    def test_largeur_deraisonnable_refusee(self):
        """Garde-fou contre une unité confondue (cm saisis à la place de mm)."""
        code, corps, _ = self._post({"labelWidthMm": "600"})
        self.assertEqual(code, 400, corps)

    def test_orientation_invalide_refusee(self):
        code, corps, _ = self._post({"labelOrientation": "diagonale"})
        self.assertEqual(code, 400, corps)

    def test_largeur_trop_etroite_avertit_sans_bloquer_l_enregistrement(self):
        code, corps, _ = self._post({"labelWidthMm": "20", "labelHeightMm": "35"})
        self.assertEqual(code, 200, corps)
        self.assertTrue(corps["warnings"], "un support trop étroit pour un EAN-13 "
                                           "doit être signalé à l'enregistrement")

    def test_requete_sans_aucun_reglage_refusee(self):
        code, corps, _ = self._post({})
        self.assertEqual(code, 400, corps)


# =====================================================================================
# 7. DISJONCTEUR SÉPARÉ : l'étiqueteuse ne doit jamais bloquer la caisse
# =====================================================================================
class TestDisjoncteurEtiquette(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp(prefix="kodo_barcode_worker_")
        self.pdf = os.path.join(self.dossier, "faux.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4\n")
        # Aucune impression réelle : on remplace le pilote CUPS.
        self._original = kodo_printer.imprimer_pdf_etiquette
        self.appels = []

        def faux_pilote(pdf_path, printer_name, media=None, copies=1):
            self.appels.append((pdf_path, printer_name, media, copies))
            return False  # étiqueteuse débranchée

        kodo_printer.imprimer_pdf_etiquette = faux_pilote
        self.worker = kodo_print_worker.PrintWorker()

    def tearDown(self):
        self.worker._running = False
        kodo_printer.imprimer_pdf_etiquette = self._original
        shutil.rmtree(self.dossier, ignore_errors=True)

    def _attendre_fin_de_file(self, timeout=10.0):
        import time
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if self.worker._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def test_trois_echecs_etiquette_n_ouvrent_pas_le_disjoncteur_ticket(self):
        for _ in range(3):
            self.worker.enqueue_label_print(self.pdf, printer_name="ETIQUETEUSE_FICTIVE")
        self.assertTrue(self._attendre_fin_de_file(), "la file d'impression n'a pas été vidée")

        self.assertEqual(len(self.appels), 3)
        self.assertEqual(self.worker.label_circuit_breaker.state,
                         kodo_print_worker.PrinterCircuitBreaker.STATE_OPEN,
                         "le disjoncteur étiquette aurait dû s'ouvrir après 3 échecs")
        self.assertFalse(self.worker.is_label_printer_available())

        # LE POINT CRITIQUE : la caisse reste opérationnelle.
        self.assertEqual(self.worker.circuit_breaker.state,
                         kodo_print_worker.PrinterCircuitBreaker.STATE_CLOSED,
                         "une étiqueteuse en panne a ouvert le disjoncteur des tickets")
        self.assertTrue(self.worker.is_printer_available())
        self.assertEqual(self.worker.circuit_breaker.failure_count, 0)
        self.assertTrue(self.worker.get_circuit_status()["is_available"])

    def test_les_deux_disjoncteurs_sont_des_objets_distincts(self):
        self.assertIsNot(self.worker.circuit_breaker, self.worker.label_circuit_breaker)
        self.worker.label_circuit_breaker.record_failure("test")
        self.assertEqual(self.worker.circuit_breaker.failure_count, 0)

    def test_etiquette_refusee_quand_le_disjoncteur_etiquette_est_ouvert(self):
        self.worker.label_circuit_breaker.state = \
            kodo_print_worker.PrinterCircuitBreaker.STATE_OPEN
        import time
        self.worker.label_circuit_breaker.last_state_change = time.monotonic()
        job = self.worker.enqueue_label_print(self.pdf, printer_name="ETIQUETEUSE_FICTIVE")
        self.assertTrue(self._attendre_fin_de_file())
        self.assertEqual(job.status, kodo_print_worker.PrintJob.STATUS_SKIPPED)
        self.assertEqual(self.appels, [], "le pilote a été appelé malgré le disjoncteur ouvert")

    def test_enqueue_refuse_sans_etiqueteuse(self):
        with self.assertRaises(ValueError):
            self.worker.enqueue_label_print(self.pdf, printer_name="")
        with self.assertRaises(ValueError):
            self.worker.enqueue_label_print("", printer_name="ETIQUETEUSE_FICTIVE")

    def test_le_chemin_ticket_reste_le_chemin_par_defaut(self):
        """Un PrintJob construit comme avant reste un travail de type TICKET."""
        job = kodo_print_worker.PrintJob("T-0001")
        self.assertEqual(job.job_type, kodo_print_worker.PrintJob.TYPE_TICKET)
        self.assertEqual(job.payload, {})


class TestPiloteEtiquetteSansMateriel(unittest.TestCase):
    """`imprimer_pdf_etiquette` ne doit jamais appeler CUPS sur une demande invalide."""

    def setUp(self):
        self.appels = []
        self._original = kodo_printer.subprocess.run

        def faux_run(*a, **kw):
            self.appels.append(a)
            raise AssertionError("aucun appel CUPS ne doit avoir lieu dans les tests")

        kodo_printer.subprocess.run = faux_run

    def tearDown(self):
        kodo_printer.subprocess.run = self._original

    def test_pdf_introuvable(self):
        self.assertFalse(kodo_printer.imprimer_pdf_etiquette(
            "/chemin/qui/n/existe/pas.pdf", "ETIQUETEUSE"))
        self.assertEqual(self.appels, [])

    def test_sans_nom_d_imprimante(self):
        """Jamais la file par défaut : ce serait l'imprimante à tickets."""
        fd, chemin = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        try:
            self.assertFalse(kodo_printer.imprimer_pdf_etiquette(chemin, None))
            self.assertFalse(kodo_printer.imprimer_pdf_etiquette(chemin, ""))
            self.assertEqual(self.appels, [])
        finally:
            os.remove(chemin)


# =====================================================================================
# 8. MIGRATION
# =====================================================================================
class TestMigrationCodeBarres(BaseBarcodeTest):

    def _triggers(self, db_path=None):
        conn = sqlite3.connect(db_path or self.db_path)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
        finally:
            conn.close()

    def test_les_declencheurs_sont_poses_sur_une_base_neuve(self):
        noms = self._triggers()
        self.assertIn("clean_empty_barcode_insert", noms)
        self.assertIn("clean_empty_barcode_update", noms)

    def test_migration_rejouable_sans_erreur(self):
        """La 2.0.4 doit pouvoir être rejouée : DROP + CREATE, aucune donnée perdue."""
        pid = self.creer_produit("Article migré", code=EAN_VALIDE_B)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM schema_version WHERE version='2.0.4'")
            conn.commit()
        finally:
            conn.close()

        MigrationManager.run_migrations(self.db_path)   # 1er rejeu
        MigrationManager.run_migrations(self.db_path)   # 2e rejeu (no-op)

        self.assertIn("clean_empty_barcode_insert", self._triggers())
        self.assertEqual(self.code_en_base(pid), EAN_VALIDE_B,
                         "la migration a altéré un code-barres existant")

    def test_migration_survit_a_une_base_avec_doublons_et_chaines_vides(self):
        """
        Base cliente dégradée : `code_barre` sans contrainte UNIQUE, avec des doublons
        et des chaînes vides. La migration ne doit pas planter la boutique au démarrage.
        """
        db = os.path.join(self.dossier, "legacy.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute("""CREATE TABLE Produits (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                code_barre TEXT,
                                nom TEXT NOT NULL)""")
            conn.executemany("INSERT INTO Produits (code_barre, nom) VALUES (?, ?)", [
                (EAN_VALIDE_B, "Doublon 1"),
                (EAN_VALIDE_B, "Doublon 2"),
                ("", "Vide 1"),
                ("", "Vide 2"),
                ("   ", "Blanc"),
                ("\r\n", "Retour chariot seul"),
                (None, "Sans code"),
                (f"{EAN_VALIDE_A}\r", "Code avec retour chariot"),
            ])
            conn.commit()

            for sql in (kodo_migrations.BARCODE_HYGIENE_TRIGGERS_SQL
                        + kodo_migrations.BARCODE_HYGIENE_BACKFILL_SQL):
                conn.execute(sql)
            conn.commit()

            lignes = dict(conn.execute("SELECT nom, code_barre FROM Produits").fetchall())
        finally:
            conn.close()

        # Les vides deviennent NULL (seule valeur que UNIQUE accepte en série).
        for nom in ("Vide 1", "Vide 2", "Blanc", "Retour chariot seul", "Sans code"):
            self.assertIsNone(lignes[nom], f"{nom} : la valeur vide n'a pas été neutralisée")
        # Les doublons NON vides sont délibérément conservés : les rogner ferait
        # échouer la migration sur la contrainte UNIQUE, boutique à l'arrêt.
        self.assertEqual(lignes["Doublon 1"], EAN_VALIDE_B)
        self.assertEqual(lignes["Doublon 2"], EAN_VALIDE_B)
        self.assertEqual(lignes["Code avec retour chariot"], f"{EAN_VALIDE_A}\r")

    def test_le_declencheur_neutralise_les_valeurs_vides_a_l_insertion(self):
        conn = sqlite3.connect(self.db_path)
        try:
            for i, valeur in enumerate(("", "  ", "\r\n", "\t")):
                conn.execute("INSERT INTO Produits (code_barre, nom) VALUES (?, ?)",
                             (valeur, f"Vide {i}"))
            conn.commit()
            restants = conn.execute(
                "SELECT COUNT(*) FROM Produits WHERE code_barre IS NOT NULL "
                "AND TRIM(code_barre, ' ' || char(9) || char(10) || char(13)) = ''"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(restants, 0,
                         "des codes-barres vides subsistent malgré le déclencheur")

    def test_le_declencheur_rogne_le_retour_chariot_de_douchette(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("INSERT INTO Produits (code_barre, nom) VALUES (?, ?)",
                         (f"{EAN_VALIDE_B}\r", "Scanné avec CR"))
            conn.commit()
            valeur = conn.execute(
                "SELECT code_barre FROM Produits WHERE nom='Scanné avec CR'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(valeur, EAN_VALIDE_B,
                         "le retour chariot reste en base : l'article sera introuvable au scan")

    def test_le_declencheur_agit_aussi_a_la_mise_a_jour(self):
        pid = self.creer_produit("Article", code=EAN_VALIDE_B)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE Produits SET code_barre=? WHERE id=?",
                         (f"  {EAN_VALIDE_A}  ", pid))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.code_en_base(pid), EAN_VALIDE_A)


if __name__ == "__main__":
    unittest.main(verbosity=2)
