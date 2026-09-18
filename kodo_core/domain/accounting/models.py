# -*- coding: utf-8 -*-
"""
Modèles métier purs de la comptabilité / clôture de caisse - Kōdo POS Core.
Aucune dépendance UI ni BDD.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional


@dataclass
class TaxBreakdown:
    """Ventilation de la TVA pour un taux donné sur la période du Z."""

    taux: Decimal
    base_ht: Decimal
    montant_tva: Decimal
    montant_ttc: Decimal


@dataclass
class PaymentBreakdown:
    """Ventilation des encaissements par mode de règlement sur la période du Z."""

    mode_paiement: str
    total_ttc: Decimal
    nombre_transactions: int


@dataclass
class ZReport:
    """Rapport Z de clôture de caisse, scellé par chaînage de hash SHA-256."""

    z_number: int
    date_debut: str
    date_fin: str
    total_ht: Decimal
    total_tva: Decimal
    total_ttc: Decimal
    taxes: List[TaxBreakdown]
    payments: List[PaymentBreakdown]
    total_discounts: Decimal
    nombre_transactions: int
    panier_moyen: Decimal
    signature_hash: str
    previous_z_hash: Optional[str] = None
