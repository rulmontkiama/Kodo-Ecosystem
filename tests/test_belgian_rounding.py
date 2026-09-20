# -*- coding: utf-8 -*-
"""
Tests unitaires et d'intégration : Arrondi légal belge à 5 centimes (Loi du 01/12/2019)
et immunité fiscale de la TVA - Kōdo POS Core.
"""

import unittest
from decimal import Decimal
from kodo_core.domain.sales.cart_engine import apply_belgian_cash_rounding, quantize_money, CartEngine
from kodo_core.hardware.print_worker import PrinterCircuitBreaker, PrintWorker, PrintJob


class TestBelgianRounding(unittest.TestCase):
    """Vérification stricte de la conformité à la Loi belge du 01/12/2019 sur l'arrondi espèces."""

    def test_arrondi_belge_5_centimes_table_officielle(self):
        """
        Table officielle SPF Économie :
        Montant se terminant par :
        - .01, .02 -> .00 (écart -0.01, -0.02)
        - .03, .04 -> .05 (écart +0.02, +0.01)
        - .05 -> .05 (écart 0.00)
        - .06, .07 -> .05 (écart -0.01, -0.02)
        - .08, .09 -> .10 (écart +0.02, +0.01)
        - .00 -> .00 (écart 0.00)
        """
        cases = [
            ("10.00", "10.00", "0.00"),
            ("10.01", "10.00", "-0.01"),
            ("10.02", "10.00", "-0.02"),
            ("10.03", "10.05", "0.02"),
            ("10.04", "10.05", "0.01"),
            ("10.05", "10.05", "0.00"),
            ("10.06", "10.05", "-0.01"),
            ("10.07", "10.05", "-0.02"),
            ("10.08", "10.10", "0.02"),
            ("10.09", "10.10", "0.01"),
            ("10.10", "10.10", "0.00"),
            ("0.02", "0.00", "-0.02"),
            ("0.03", "0.05", "0.02"),
            ("123.47", "123.45", "-0.02"),
            ("123.48", "123.50", "0.02"),
        ]

        for brut, att_arrondi, att_ecart in cases:
            arrondi, ecart = apply_belgian_cash_rounding(Decimal(brut))
            self.assertEqual(arrondi, Decimal(att_arrondi), f"Échec arrondi pour {brut}")
            self.assertEqual(ecart, Decimal(att_ecart), f"Échec écart pour {brut}")
            # Règle d'intégrité comptable : brut + écart == arrondi
            self.assertEqual(quantize_money(Decimal(brut) + ecart), arrondi)

    def test_rendu_monnaie_avec_arrondi_especes(self):
        """
        Vérifie le calcul du rendu de monnaie :
        Exemple : Total 10.03€ arrondi à 10.05€, payé avec un billet de 20€ -> Rendu = 9.95€.
        Exemple : Total 10.02€ arrondi à 10.00€, payé avec un billet de 20€ -> Rendu = 10.00€.
        """
        # Cas 1 : arrondi vers le haut
        rendu1, reste1 = CartEngine.calculate_change_due(Decimal("10.05"), [("Espèces", Decimal("20.00"))])
        self.assertEqual(rendu1, Decimal("9.95"))
        self.assertEqual(reste1, Decimal("0.00"))

        # Cas 2 : arrondi vers le bas
        rendu2, reste2 = CartEngine.calculate_change_due(Decimal("10.00"), [("Espèces", Decimal("20.00"))])
        self.assertEqual(rendu2, Decimal("10.00"))
        self.assertEqual(reste2, Decimal("0.00"))


class TestPrinterCircuitBreaker(unittest.TestCase):
    """Vérifie le comportement du Circuit Breaker matériel."""

    def test_circuit_breaker_transitions(self):
        cb = PrinterCircuitBreaker(failure_threshold=3, recovery_timeout=0.2)
        self.assertEqual(cb.state, PrinterCircuitBreaker.STATE_CLOSED)
        self.assertTrue(cb.can_attempt())

        # Échec 1 et 2 -> reste CLOSED
        cb.record_failure("Erreur 1")
        self.assertEqual(cb.state, PrinterCircuitBreaker.STATE_CLOSED)
        cb.record_failure("Erreur 2")
        self.assertEqual(cb.state, PrinterCircuitBreaker.STATE_CLOSED)

        # Échec 3 -> bascule en OPEN
        cb.record_failure("Erreur 3")
        self.assertEqual(cb.state, PrinterCircuitBreaker.STATE_OPEN)
        self.assertFalse(cb.can_attempt())

        # Attente du recovery_timeout (0.2s) -> bascule en HALF_OPEN
        import time
        time.sleep(0.25)
        self.assertTrue(cb.can_attempt())
        self.assertEqual(cb.state, PrinterCircuitBreaker.STATE_HALF_OPEN)

        # Succès en HALF_OPEN -> revient en CLOSED
        cb.record_success()
        self.assertEqual(cb.state, PrinterCircuitBreaker.STATE_CLOSED)
        self.assertEqual(cb.failure_count, 0)


if __name__ == '__main__':
    unittest.main()
