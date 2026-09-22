# -*- coding: utf-8 -*-
"""Précision monétaire : un seul arrondi de référence, et des ventilations qui tombent juste.

Trois familles de régressions comptables sont verrouillées ici :

1. Cinq modules arrondissaient les montants avec cinq implémentations différentes.
   `fiscal_service` utilisait `Decimal(float)` (valeur binaire exacte du float) et rendait
   2.675 -> 2.67 là où les autres rendaient 2.68 : un centime d'écart sur une clôture
   fiscale scellée. `printer_service` ne quantifiait pas du tout, il formatait avec
   `f"{x:.2f}"` (arrondi banquier) et imprimait 8.345 -> 8.34 sur le ticket de la cliente.
   `cart_service` et `closing_service` plantaient sur autre chose qu'un Decimal.

2. Le garde-fou anti-float de `to_decimal` était contourné par `from_dict`, qui emballait
   la valeur JSON dans `Decimal(...)` AVANT que `__post_init__` ne puisse voir un float :
   un prix JSON 19.99 était stocké 19.98999999999999843680598132777959108352661132812500.

3. Les ventilations de TVA ne se refermaient pas sur les totaux annoncés : la somme des
   bases HT par taux pouvait différer du total HT du Z (mesuré : 2 centimes sur 40 ventes).

Base temporaire pour les tests de clôture : aucune donnée réelle touchée.
"""
import datetime
import os
import random
import sys
import tempfile
import unittest
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database_manager
from kodo_core.domain.accounting.ledger import build_hash_payload
from kodo_core.domain.accounting.z_report import ZReportEngine
from kodo_core.domain.accounting.z_report import quantize_money as q_z_report
from kodo_core.domain.sales.cart_engine import (
    CartEngine,
    apply_belgian_cash_rounding,
)
from kodo_core.domain.sales.cart_engine import CartItem as EngineCartItem
from kodo_core.domain.sales.cart_engine import quantize_money as q_cart_engine
from kodo_core.domain.sales.models import Cart, CartDiscount, CartItem, DiscountType
from kodo_core.domain.sales.models import quantize_money as q_reference
from kodo_core.hardware.printer_service import _quantize as q_printer
from kodo_core.services.cart_service import calculate_cart
from kodo_core.services.cart_service import quantize_money as q_cart_service
from kodo_core.services.closing_service import quantize_money as q_closing_service
from kodo_core.services.fiscal_service import quantize_money as q_fiscal_service

# Tous les chemins d'arrondi du projet. `printer_service` rend une chaîne : il est comparé à part.
CHEMINS_DECIMAL = [
    ("z_report", q_z_report),
    ("cart_engine", q_cart_engine),
    ("cart_service", q_cart_service),
    ("closing_service", q_closing_service),
    ("fiscal_service", q_fiscal_service),
]

# Valeurs piégeuses : demi-centime exact (là où ROUND_HALF_UP et l'arrondi banquier
# divergent), float dont l'écriture binaire tombe SOUS le demi-centime (2.675, 1.005),
# négatifs (remboursements), et les types que les appelants historiques font circuler.
VALEURS_PIEGEUSES = [
    2.675, 1.005, 0.125, 19.99, 8.345, 1234.565, -2.675, -0.125, -8.345,
    Decimal("2.675"), Decimal("1.005"), Decimal("0.125"), Decimal("19.99"),
    Decimal("8.345"), Decimal("1234.565"), Decimal("-2.675"), Decimal("-0.125"),
    "2.675", "1.005", "8.345", "19.99", "-2.675",
    0, 3, -7, None,
]

ATTENDU = {
    "2.675": "2.68", "1.005": "1.01", "0.125": "0.13", "19.99": "19.99",
    "8.345": "8.35", "1234.565": "1234.57", "-2.675": "-2.68", "-0.125": "-0.13",
    "-8.345": "-8.35", "0": "0.00", "3": "3.00", "-7": "-7.00", "None": "0.00",
}


def _cle(valeur):
    return "None" if valeur is None else str(valeur)


@pytest.mark.parametrize("valeur", VALEURS_PIEGEUSES, ids=_cle)
def test_tous_les_chemins_darrondi_donnent_le_meme_resultat(valeur):
    """Les cinq chemins Decimal + le formatage du ticket doivent être rigoureusement alignés."""
    attendu = ATTENDU[_cle(valeur)]

    for nom, fonction in CHEMINS_DECIMAL:
        obtenu = fonction(valeur)
        assert isinstance(obtenu, Decimal), f"{nom} ne rend pas un Decimal pour {valeur!r}"
        assert str(obtenu) == attendu, f"{nom}({valeur!r}) = {obtenu} au lieu de {attendu}"

    assert q_printer(valeur) == attendu, f"printer_service({valeur!r}) = {q_printer(valeur)}"


def test_reference_unique_reellement_partagee():
    """Les modules doivent réutiliser la référence, pas en recopier une variante."""
    for nom, fonction in CHEMINS_DECIMAL:
        assert fonction is q_reference, f"{nom} n'utilise pas la référence unique quantize_money"


def test_arrondi_est_bien_half_up_et_non_banquier():
    """8.345 et 0.125 distinguent ROUND_HALF_UP de l'arrondi banquier (qui rendrait 8.34 / 0.12)."""
    assert q_reference(Decimal("8.345")) == Decimal("8.35")
    assert q_reference(Decimal("0.125")) == Decimal("0.13")
    assert q_reference(Decimal("2.665")) == Decimal("2.67")
    assert q_printer(Decimal("8.345")) == "8.35"
    assert q_printer(Decimal("0.125")) == "0.13"


# --- B. Le garde-fou anti-float ne doit plus être contournable par from_dict -------------------

def test_from_dict_ne_laisse_plus_fuiter_la_valeur_binaire_dun_float():
    """Un prix JSON 19.99 doit valoir exactement Decimal('19.99'), pas 19.9899999999999984..."""
    item = CartItem.from_dict({"unit_price_ttc": 19.99, "quantity": 2, "vat_rate": 0.21})
    assert item.unit_price_ttc == Decimal("19.99")
    assert item.vat_rate == Decimal("0.21")
    # Le test ci-dessus passerait avec une simple égalité numérique : on vérifie l'écriture exacte.
    assert format(item.unit_price_ttc, "f") == "19.99"
    assert format(item.vat_rate, "f") == "0.21"


def test_from_dict_accepte_toujours_les_chaines_produites_par_to_dict():
    """Le format canonique (to_dict écrit des str) doit continuer de faire l'aller-retour."""
    origine = CartItem(
        unit_price_ttc=Decimal("19.99"), quantity=2, vat_rate=Decimal("0.21"),
        discount=CartDiscount(type=DiscountType.PERCENT, value=Decimal("10")),
    )
    copie = CartItem.from_dict(origine.to_dict())
    assert copie.unit_price_ttc == origine.unit_price_ttc
    assert copie.vat_rate == origine.vat_rate
    assert copie.discount.value == origine.discount.value


def test_cart_discount_et_cart_from_dict_ne_fuitent_pas_non_plus():
    panier = Cart.from_dict({
        "items": [{"unit_price_ttc": 19.99, "quantity": 1, "vat_rate": 0.21}],
        "global_discount": {"type": "PERCENT", "value": 10.1},
    })
    assert format(panier.items[0].unit_price_ttc, "f") == "19.99"
    assert format(panier.global_discount.value, "f") == "10.1"


def test_le_garde_fou_refuse_toujours_un_float_passe_en_direct():
    """La conversion tolérante est réservée à l'entrée JSON : le constructeur reste strict."""
    with pytest.raises(TypeError):
        CartItem(unit_price_ttc=19.99, quantity=1, vat_rate=Decimal("0.21"))
    with pytest.raises(TypeError):
        CartDiscount(type=DiscountType.PERCENT, value=10.5)


# --- C. Ventilations de TVA : elles doivent se refermer exactement sur les totaux -------------

def _panier_engine(lignes, remise_pct=0.0, remise_montant=0.0):
    moteur = CartEngine()
    for index, (prix, quantite, taux) in enumerate(lignes):
        moteur.add_item(EngineCartItem(
            product_id=None, stock_id=index + 1, name=f"Article {index}",
            unit_price_tvac=prix, quantity=quantite, vat_rate=taux,
        ))
    moteur.set_global_discount(percent=remise_pct, amount=remise_montant)
    return moteur.calculate_totals()


def test_cart_engine_ventilation_se_referme_sur_les_totaux():
    """Panier piégeux : plusieurs taux, remise globale, quantités multiples."""
    totaux = _panier_engine(
        [(19.99, 3, 0.21), (7.35, 2, 0.06), (4.17, 5, 0.12), (0.99, 7, 0.21)],
        remise_pct=13.0,
    )
    tvac = Decimal(str(totaux["total_tvac"]))
    htva = Decimal(str(totaux["total_htva"]))
    tva = Decimal(str(totaux["total_tva"]))
    ventilation = totaux["vat_breakdown"].values()

    assert htva + tva == tvac
    assert sum((Decimal(str(v["tvac"])) for v in ventilation), Decimal("0.00")) == tvac
    assert sum((Decimal(str(v["htva"])) for v in ventilation), Decimal("0.00")) == htva
    assert sum((Decimal(str(v["tva"])) for v in ventilation), Decimal("0.00")) == tva


def test_cart_engine_ventilation_se_referme_sur_2000_paniers_aleatoires():
    """Le centime perdu n'apparaissait que sur ~17% des paniers : un cas isolé ne suffit pas."""
    alea = random.Random(20260921)
    taux_possibles = [0.21, 0.06, 0.12, 0.0]

    for _ in range(2000):
        lignes = [
            (round(alea.uniform(0.01, 300), 2), alea.randint(1, 9), alea.choice(taux_possibles))
            for _ in range(alea.randint(1, 6))
        ]
        totaux = _panier_engine(
            lignes,
            remise_pct=round(alea.uniform(0, 60), 2) if alea.random() < 0.6 else 0.0,
            remise_montant=round(alea.uniform(0, 50), 2) if alea.random() < 0.3 else 0.0,
        )
        tvac = Decimal(str(totaux["total_tvac"]))
        htva = Decimal(str(totaux["total_htva"]))
        tva = Decimal(str(totaux["total_tva"]))
        ventilation = totaux["vat_breakdown"].values()

        assert htva + tva == tvac, lignes
        assert sum((Decimal(str(v["tvac"])) for v in ventilation), Decimal("0.00")) == tvac, lignes
        assert sum((Decimal(str(v["htva"])) for v in ventilation), Decimal("0.00")) == htva, lignes
        assert sum((Decimal(str(v["tva"])) for v in ventilation), Decimal("0.00")) == tva, lignes
        for valeurs in totaux["vat_breakdown"].values():
            montants = (Decimal(str(valeurs["htva"])), Decimal(str(valeurs["tva"])), Decimal(str(valeurs["tvac"])))
            assert montants[0] + montants[1] == montants[2], lignes


def test_libelle_de_taux_de_tva_ne_contient_quun_seul_pourcent():
    """`f"{x:.1f}%".rstrip('0') + "%"` ne strippait rien (la chaîne finit par '%') : « 21.0%% »."""
    totaux = _panier_engine([(19.99, 1, 0.21), (7.35, 1, 0.06), (10.00, 1, 0.0)])
    for libelle in totaux["vat_breakdown"]:
        assert libelle.count("%") == 1, f"libellé de taux incorrect : {libelle!r}"
    assert set(totaux["vat_breakdown"]) == {"21%", "6%", "0%"}


def test_cart_service_ventilation_se_referme_sur_les_totaux():
    lignes = [
        CartItem(unit_price_ttc=Decimal("19.99"), quantity=3, vat_rate=Decimal("0.21")),
        CartItem(unit_price_ttc=Decimal("7.35"), quantity=2, vat_rate=Decimal("0.06")),
        CartItem(unit_price_ttc=Decimal("4.17"), quantity=5, vat_rate=Decimal("0.12")),
    ]
    for remise in (
        None,
        CartDiscount(type=DiscountType.PERCENT, value=Decimal("13")),
        CartDiscount(type=DiscountType.AMOUNT, value=Decimal("11.11")),
    ):
        resultat = calculate_cart(lignes, remise)
        assert sum((l.base_ht for l in resultat.vat_breakdown), Decimal("0.00")) == resultat.total_ht
        assert sum((l.montant_tva for l in resultat.vat_breakdown), Decimal("0.00")) == resultat.total_tva
        assert sum((l.montant_ttc for l in resultat.vat_breakdown), Decimal("0.00")) == resultat.total_ttc
        assert resultat.total_ht + resultat.total_tva == resultat.total_ttc


def test_remise_globale_en_montant_se_conserve_au_centime():
    lignes = [
        CartItem(unit_price_ttc=Decimal("19.99"), quantity=3, vat_rate=Decimal("0.21")),
        CartItem(unit_price_ttc=Decimal("7.35"), quantity=2, vat_rate=Decimal("0.06")),
        CartItem(unit_price_ttc=Decimal("4.17"), quantity=5, vat_rate=Decimal("0.12")),
    ]
    brut = sum((l.unit_price_ttc * l.quantity for l in lignes), Decimal("0.00"))
    for demandee in ("0.01", "11.11", "33.33", "0.07"):
        resultat = calculate_cart(lignes, CartDiscount(type=DiscountType.AMOUNT, value=Decimal(demandee)))
        assert resultat.total_discount_ttc == Decimal(demandee)
        assert resultat.total_ttc == q_reference(brut) - Decimal(demandee)


# --- Arrondi belge à 5 centimes (Loi du 01/12/2019) -------------------------------------------

def test_arrondi_belge_tombe_toujours_sur_un_multiple_de_cinq_centimes():
    for centimes in range(0, 200):
        montant = q_reference(Decimal(centimes) / Decimal("100"))
        arrondi, ecart = apply_belgian_cash_rounding(montant)
        assert int(arrondi * 100) % 5 == 0, montant
        assert arrondi - montant == ecart, montant
        assert abs(ecart) <= Decimal("0.02"), montant


def test_arrondi_belge_sens_legal_par_terminaison():
    attendu = {
        "10.01": "10.00", "10.02": "10.00", "10.03": "10.05", "10.04": "10.05",
        "10.06": "10.05", "10.07": "10.05", "10.08": "10.10", "10.09": "10.10",
        "10.00": "10.00", "10.05": "10.05",
    }
    for brut, cible in attendu.items():
        arrondi, _ = apply_belgian_cash_rounding(Decimal(brut))
        assert arrondi == Decimal(cible), brut


def test_arrondi_belge_cas_exact_du_demi_centime():
    """Un .x25 / .x75 est d'abord ramené au centime en ROUND_HALF_UP, jamais tronqué."""
    assert apply_belgian_cash_rounding(Decimal("10.025"))[0] == Decimal("10.05")  # -> 10.03 -> 10.05
    assert apply_belgian_cash_rounding(Decimal("10.075"))[0] == Decimal("10.10")  # -> 10.08 -> 10.10
    assert apply_belgian_cash_rounding(Decimal("0.025"))[0] == Decimal("0.05")


class TestVentilationTvaDuZ(unittest.TestCase):
    """La ventilation TVA du Z doit se refermer exactement sur les totaux du bilan.

    Le bilan somme `Tickets.total_htva` (arrondi ligne par ligne au moment de la vente),
    tandis que la ventilation ré-arrondissait une seule fois par taux agrégé : les deux ne
    tombaient pas sur le même centime (mesuré : -0.02 sur 40 ventes).
    """

    def setUp(self):
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._ancienne_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()
        self.conn = database_manager.get_connection()

    def tearDown(self):
        self.conn.close()
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._ancienne_db
        os.close(self.fd)
        for suffixe in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffixe):
                os.remove(self.path + suffixe)

    def _catalogue(self):
        cursor = self.conn.cursor()
        references = []
        for nom, prix, taux in (
            ("Robe", 19.99, 0.21), ("Pain", 7.35, 0.06),
            ("Livre", 4.17, 0.06), ("Sac", 121.35, 0.21),
        ):
            cursor.execute(
                "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva, en_solde) VALUES (?, ?, ?, 0)",
                (nom, prix, taux),
            )
            produit_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'Unique', 100000)",
                (produit_id,),
            )
            references.append((produit_id, cursor.lastrowid))
        self.conn.commit()
        return references

    def test_ventilation_tva_du_z_egale_les_totaux_du_bilan(self):
        from kodo_core.domain.sales.cart_engine import process_sale_transaction

        references = self._catalogue()
        alea = random.Random(7)
        for _ in range(40):
            articles = [
                {"stock_id": stock_id, "product_id": produit_id,
                 "quantite": alea.randint(1, 5), "taille": "Unique"}
                for produit_id, stock_id in
                [alea.choice(references) for _ in range(alea.randint(1, 4))]
            ]
            process_sale_transaction(
                cart_items=articles, total_tvac=0.0, payments=[("CB", 100000.0)],
                discount_percent=alea.choice([0.0, 0.0, 7.0, 13.0, 33.33]),
                conn=self.conn, cashier_name="Test",
            )
        self.conn.commit()

        bilan = ZReportEngine.get_daily_z_summary(conn=self.conn)
        ventilation = bilan["vat_breakdown"].values()

        total_tvac = Decimal(str(bilan["total_tvac"]))
        total_htva = Decimal(str(bilan["total_htva"]))
        total_tva = Decimal(str(bilan["total_tva"]))

        self.assertEqual(
            sum((Decimal(str(v["tvac"])) for v in ventilation), Decimal("0.00")), total_tvac)
        self.assertEqual(
            sum((Decimal(str(v["htva"])) for v in ventilation), Decimal("0.00")), total_htva)
        self.assertEqual(
            sum((Decimal(str(v["tva"])) for v in ventilation), Decimal("0.00")), total_tva)

    def test_ventilation_tva_du_z_se_referme_aussi_quand_le_centime_porte_sur_le_tvac(self):
        """
        Le résidu d'arrondi ne tombe pas toujours sur la base HT.

        La ventilation arrondit une fois par couple (ticket, taux) ; `Tickets.total_tvac` a
        été scellé en une seule fois. Avec deux taux dans le même panier et une remise, la
        somme des TVAC ventilés peut dépasser d'un centime le total scellé. La réconciliation
        ne corrigeait QUE la base HT, et ne se déclenchait que si le TVAC tombait déjà juste :
        dans ce cas-là, elle ne faisait rien et le Z publiait une ventilation annonçant plus
        de TVAC que son propre total.

        Cas reproduit par le vrai chemin de vente : 10,01 € à 21 % + 10,01 € à 6 %, remise 50 %.
        """
        from kodo_core.domain.sales.cart_engine import process_sale_transaction

        cursor = self.conn.cursor()
        references = []
        for taux in (0.21, 0.06):
            cursor.execute(
                "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva, en_solde) VALUES (?, 10.01, ?, 0)",
                (f"Article {taux}", taux))
            produit_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'Unique', 100)",
                (produit_id,))
            references.append((produit_id, cursor.lastrowid))
        self.conn.commit()

        process_sale_transaction(
            cart_items=[{"stock_id": stock_id, "product_id": produit_id,
                         "quantite": 1, "taille": "Unique"}
                        for produit_id, stock_id in references],
            total_tvac=0.0, payments=[("CB", 100.0)], discount_percent=50.0,
            conn=self.conn, cashier_name="Test")
        self.conn.commit()

        bilan = ZReportEngine.get_daily_z_summary(conn=self.conn)
        ventilation = bilan["vat_breakdown"].values()

        self.assertEqual(
            sum((Decimal(str(v["tvac"])) for v in ventilation), Decimal("0.00")),
            Decimal(str(bilan["total_tvac"])),
            "La ventilation annonce un TVAC différent du total du Z : un centime encaissé "
            "nulle part, ou encaissé deux fois, selon le sens.")
        self.assertEqual(
            sum((Decimal(str(v["htva"])) for v in ventilation), Decimal("0.00")),
            Decimal(str(bilan["total_htva"])))
        self.assertEqual(
            sum((Decimal(str(v["tva"])) for v in ventilation), Decimal("0.00")),
            Decimal(str(bilan["total_tva"])))

    def test_un_vrai_trou_de_donnees_reste_visible_et_nest_pas_recale(self):
        """
        La tolérance ne doit rattraper QUE de l'arrondi.

        Un ticket sans aucune ligne de vente rattachée compte dans le total du Z mais
        n'apparaît dans aucun taux : l'écart vaut son montant entier. Le masquer dans le plus
        gros taux rendrait le trou indétectable — c'est exactement ce que le garde-fou existe
        pour empêcher, et desserrer l'égalité stricte ne doit pas l'avoir supprimé.
        """
        from kodo_core.domain.sales.cart_engine import process_sale_transaction

        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO Produits (nom, prix_vente_tvac, taux_tva, en_solde) VALUES ('Robe', 19.99, 0.21, 0)")
        produit_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'Unique', 100)",
            (produit_id,))
        stock_id = cursor.lastrowid
        self.conn.commit()

        process_sale_transaction(
            cart_items=[{"stock_id": stock_id, "product_id": produit_id,
                         "quantite": 1, "taille": "Unique"}],
            total_tvac=0.0, payments=[("CB", 100.0)], discount_percent=0.0,
            conn=self.conn, cashier_name="Test")

        # Le trou : un ticket encaissé dont aucune ligne ne dit ce qui a été vendu.
        cursor.execute(
            "INSERT INTO Tickets (numero_ticket, date_heure, total_tvac, total_htva, total_tva, "
            "methode_paiement) VALUES ('TCK-ORPHELIN', ?, 50.00, 41.32, 8.68, 'CB')",
            (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        self.conn.commit()

        bilan = ZReportEngine.get_daily_z_summary(conn=self.conn)
        ventilation = bilan["vat_breakdown"].values()
        somme_tvac = sum((Decimal(str(v["tvac"])) for v in ventilation), Decimal("0.00"))

        self.assertNotEqual(
            somme_tvac, Decimal(str(bilan["total_tvac"])),
            "Un ticket sans ligne de vente a été absorbé dans la ventilation : le Z semble "
            "cohérent alors qu'il manque la justification de 50 € encaissés.")

    def test_libelle_de_taux_du_z_ne_contient_quun_seul_pourcent(self):
        from kodo_core.domain.sales.cart_engine import process_sale_transaction

        references = self._catalogue()
        for produit_id, stock_id in references:
            process_sale_transaction(
                cart_items=[{"stock_id": stock_id, "product_id": produit_id,
                             "quantite": 1, "taille": "Unique"}],
                total_tvac=0.0, payments=[("CB", 100000.0)],
                conn=self.conn, cashier_name="Test",
            )
        self.conn.commit()

        bilan = ZReportEngine.get_daily_z_summary(conn=self.conn)
        self.assertTrue(bilan["vat_breakdown"], "ventilation vide : le test ne prouve rien")
        for libelle in bilan["vat_breakdown"]:
            self.assertEqual(libelle.count("%"), 1, f"libellé de taux incorrect : {libelle!r}")
        self.assertEqual(set(bilan["vat_breakdown"]), {"21%", "6%"})


class TestScellementFiscalCanonique(unittest.TestCase):
    """
    La chaîne scellée du journal fiscal inaltérable doit dire le montant de la MÊME façon,
    quel que soit le type par lequel il arrive, et avec l'arrondi du projet.

    `build_hash_payload` faisait `f"{Decimal(v):.2f}"`. Deux pièges y étaient posés :
    `Decimal(v)` sur un flottant prend la valeur binaire du flottant et non le nombre écrit,
    et `:.2f` applique l'arrondi *bancaire*, celui-là même qui faisait imprimer 8,34 sur le
    ticket là où la vente scellait 8,35. Un montant scellé puis relu par un autre chemin
    pouvait donc ne plus produire la même empreinte : la chaîne devenait invérifiable sans
    que rien n'ait été falsifié.
    """

    def montant(self, valeur):
        return build_hash_payload("TCK-2026-0001", "2026-01-01T10:00:00",
                                  valeur, Decimal("0.00"), "GENESIS").split("|")[2]

    def test_un_meme_montant_donne_la_meme_empreinte_quel_que_soit_son_type(self):
        attendu = self.montant(Decimal("19.99"))
        self.assertEqual(self.montant("19.99"), attendu)
        self.assertEqual(self.montant(19.99), attendu)

    def test_l_arrondi_est_celui_du_projet_et_pas_l_arrondi_bancaire(self):
        self.assertEqual(self.montant(Decimal("8.345")), "8.35")
        self.assertEqual(self.montant(Decimal("0.015")), "0.02")
        self.assertEqual(q_reference(Decimal("8.345")), Decimal(self.montant(Decimal("8.345"))),
                         "le scellement doit arrondir comme la référence unique du projet")

    def test_aucune_empreinte_deja_scellee_ne_change(self):
        """
        `seal_sale` arrondit à deux décimales AVANT de sceller : sur ces montants-là, ancienne
        et nouvelle écriture coïncident. Le correctif ne réécrit donc aucun passé.
        """
        for centimes in range(0, 20000):
            valeur = Decimal(centimes) / Decimal(100)
            ancienne = f"{valeur:.2f}"
            self.assertEqual(self.montant(valeur), ancienne)


if __name__ == "__main__":
    unittest.main()
