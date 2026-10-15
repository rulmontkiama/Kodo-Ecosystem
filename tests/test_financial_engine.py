# -*- coding: utf-8 -*-
"""
Tests unitaires stricts du moteur financier pur (FinancialEngine) - Kōdo POS Core.
Vérification des invariants comptables et de l'immunité fiscale NF525.
"""

import unittest
from decimal import Decimal

from kodo_core.domain.sales.models import (
    CartDiscount,
    CartItem,
    DiscountType,
    FinancialReceipt,
    quantize_money,
)
from kodo_core.domain.sales.financial_engine import (
    FinancialEngine,
    apply_belgian_cash_rounding,
    is_cash_payment,
)


class TestFinancialEngine(unittest.TestCase):

    def test_belgian_cash_rounding_official_rules(self):
        """Vérifie les 4 cas de la Loi belge du 01/12/2019 sur l'arrondi espèces."""
        # Cas 1 & 2 : finissant par 1 ou 2 -> arrondi à .00
        arr, ecart = apply_belgian_cash_rounding(Decimal("10.01"))
        self.assertEqual(arr, Decimal("10.00"))
        self.assertEqual(ecart, Decimal("-0.01"))

        arr, ecart = apply_belgian_cash_rounding(Decimal("10.02"))
        self.assertEqual(arr, Decimal("10.00"))
        self.assertEqual(ecart, Decimal("-0.02"))

        # Cas 3 & 4 : finissant par 3 ou 4 -> arrondi à .05
        arr, ecart = apply_belgian_cash_rounding(Decimal("10.03"))
        self.assertEqual(arr, Decimal("10.05"))
        self.assertEqual(ecart, Decimal("0.02"))

        arr, ecart = apply_belgian_cash_rounding(Decimal("10.04"))
        self.assertEqual(arr, Decimal("10.05"))
        self.assertEqual(ecart, Decimal("0.01"))

        # Cas 5 : finissant par 5 -> aucun arrondi
        arr, ecart = apply_belgian_cash_rounding(Decimal("10.05"))
        self.assertEqual(arr, Decimal("10.05"))
        self.assertEqual(ecart, Decimal("0.00"))

        # Cas 6 & 7 : finissant par 6 ou 7 -> arrondi à .05
        arr, ecart = apply_belgian_cash_rounding(Decimal("10.06"))
        self.assertEqual(arr, Decimal("10.05"))
        self.assertEqual(ecart, Decimal("-0.01"))

        arr, ecart = apply_belgian_cash_rounding(Decimal("10.07"))
        self.assertEqual(arr, Decimal("10.05"))
        self.assertEqual(ecart, Decimal("-0.02"))

        # Cas 8 & 9 : finissant par 8 ou 9 -> arrondi à .10
        arr, ecart = apply_belgian_cash_rounding(Decimal("10.08"))
        self.assertEqual(arr, Decimal("10.10"))
        self.assertEqual(ecart, Decimal("0.02"))

        arr, ecart = apply_belgian_cash_rounding(Decimal("10.09"))
        self.assertEqual(arr, Decimal("10.10"))
        self.assertEqual(ecart, Decimal("0.01"))

    def test_cash_rounding_guard_strictly_zero_on_non_cash(self):
        """GARDE-FOU CTO : Si la vente est payée sans espèces (ex: 100% CB), ecart_arrondi_cash doit être 0.00."""
        items = [
            CartItem(unit_price_ttc=Decimal("19.99"), quantity=1, vat_rate=Decimal("0.21"), name="Chemise")
        ]
        # Total TTC = 19.99 €. En cash, cela s'arrondirait à 20.00 € (écart +0.01 €).
        # Mais le client paie par Carte Bancaire : aucun arrondi ne doit être opéré !
        receipt = FinancialEngine.compute_financial_receipt(
            items=items,
            payments=[("CB", Decimal("19.99"))],
        )

        self.assertEqual(receipt.total_tvac, Decimal("19.99"))
        self.assertEqual(receipt.ecart_arrondi_cash, Decimal("0.00"), "Paiement CB -> ecart_arrondi_cash STRICTEMENT 0.00")
        self.assertEqual(receipt.rendu, Decimal("0.00"))
        self.assertEqual(receipt.total_htva + receipt.total_tva, receipt.total_tvac)

    def test_cash_rounding_applied_when_cash_tendered(self):
        """Si des espèces sont remises, l'arrondi s'applique sur le solde cash."""
        items = [
            CartItem(unit_price_ttc=Decimal("19.99"), quantity=1, vat_rate=Decimal("0.21"), name="Chemise")
        ]
        # Total TTC = 19.99 €. Arrondi belge 5c -> 20.00 € (écart +0.01 €).
        # Le client donne un billet de 20.00 € en espèces.
        receipt = FinancialEngine.compute_financial_receipt(
            items=items,
            payments=[("Espèces", Decimal("20.00"))],
        )

        self.assertEqual(receipt.total_tvac, Decimal("19.99"), "L'assiette fiscale TTC ne doit JAMAIS changer")
        self.assertEqual(receipt.ecart_arrondi_cash, Decimal("0.01"), "Écart d'arrondi +0.01 € isolé")
        self.assertEqual(receipt.rendu, Decimal("0.00"), "20.00 € remis pour 20.00 € dû arrondi -> 0.00 rendu")
        self.assertEqual(receipt.total_htva + receipt.total_tva, receipt.total_tvac)

    def test_multi_vat_breakdown_and_largest_remainder_reconciliation(self):
        """Vérifie la ventilation multi-taux (21%, 12%, 6%) et la réconciliation du centime d'arrondi."""
        items = [
            CartItem(unit_price_ttc=Decimal("10.00"), quantity=1, vat_rate=Decimal("0.21"), name="Vêtement 21%"),
            CartItem(unit_price_ttc=Decimal("5.50"), quantity=2, vat_rate=Decimal("0.06"), name="Livre 6%"),
            CartItem(unit_price_ttc=Decimal("8.25"), quantity=1, vat_rate=Decimal("0.12"), name="Restauration 12%"),
        ]
        # Total brut = 10.00 + 11.00 + 8.25 = 29.25 €
        receipt = FinancialEngine.compute_financial_receipt(
            items=items,
            payments=[("CB", Decimal("29.25"))],
        )

        self.assertEqual(receipt.total_tvac, Decimal("29.25"))
        self.assertEqual(receipt.total_htva + receipt.total_tva, receipt.total_tvac)

        # Vérification de la somme des ventilations
        sum_vat_lines_ttc = sum((v.montant_ttc for v in receipt.vat_breakdown), Decimal("0.00"))
        sum_vat_lines_ht = sum((v.base_ht for v in receipt.vat_breakdown), Decimal("0.00"))
        sum_vat_lines_tva = sum((v.montant_tva for v in receipt.vat_breakdown), Decimal("0.00"))

        self.assertEqual(sum_vat_lines_ttc, receipt.total_tvac, "La somme des TVAC ventilées doit égaler le total TTC")
        self.assertEqual(sum_vat_lines_ht, receipt.total_htva)
        self.assertEqual(sum_vat_lines_tva, receipt.total_tva)

    def test_split_tender_mixed_payment(self):
        """Paiement mixte : Avoir + Carte Bancaire + Espèces."""
        items = [
            CartItem(unit_price_ttc=Decimal("54.33"), quantity=1, vat_rate=Decimal("0.21"), name="Pantalon")
        ]
        # Total TTC = 54.33 €
        # Paiements non-cash : Avoir 20.00 €, CB 20.00 € -> total non-cash = 40.00 €
        # Reste brut en espèces = 14.33 € -> arrondi belge à 5c = 14.35 € (écart +0.02 €)
        # Espèces remises = 20.00 €
        # Total effectif dû = 40.00 + 14.35 = 54.35 €
        # Rendu monnaie = 60.00 - 54.35 = 5.65 €
        receipt = FinancialEngine.compute_financial_receipt(
            items=items,
            payments=[
                ("Avoir", Decimal("20.00")),
                ("CB", Decimal("20.00")),
                ("Cash", Decimal("20.00")),
            ],
        )

        self.assertEqual(receipt.total_tvac, Decimal("54.33"), "Total fiscal TVAC inchangé")
        self.assertEqual(receipt.ecart_arrondi_cash, Decimal("0.02"), "Arrondi sur le solde espèces 14.33 -> 14.35")
        self.assertEqual(receipt.rendu, Decimal("5.65"), "Rendu exact sur le paiement total de 60.00 €")
        self.assertEqual(receipt.total_htva + receipt.total_tva, receipt.total_tvac)

    def test_global_discount_proportional_distribution(self):
        """Une remise globale est répartie proportionnellement sans distorsion d'arrondi."""
        items = [
            CartItem(unit_price_ttc=Decimal("50.00"), quantity=1, vat_rate=Decimal("0.21")),
            CartItem(unit_price_ttc=Decimal("50.00"), quantity=1, vat_rate=Decimal("0.06")),
        ]
        # Remise 10% sur 100.00 € -> 90.00 € net
        receipt = FinancialEngine.compute_financial_receipt(
            items=items,
            payments=[("CB", Decimal("90.00"))],
            global_discount_percent=Decimal("10.00"),
        )

        self.assertEqual(receipt.total_tvac, Decimal("90.00"))
        self.assertEqual(receipt.remise, Decimal("10.00"))
        self.assertEqual(receipt.total_htva + receipt.total_tva, receipt.total_tvac)


if __name__ == "__main__":
    unittest.main()
