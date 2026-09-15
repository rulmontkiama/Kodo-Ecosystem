# -*- coding: utf-8 -*-
"""
Modèles métier purs du panier de vente - Kōdo POS Core.
Aucune dépendance UI ni BDD. Toute valeur monétaire est un decimal.Decimal.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Optional


def to_decimal(value, field_name: str) -> Decimal:
    """Convertit une valeur en Decimal. Rejette les float (imprécision binaire interdite)."""
    if isinstance(value, float):
        raise TypeError(
            f"{field_name}: les float sont interdits pour les montants/taux. "
            f"Utilise Decimal ou une str (ex: Decimal('{value}'))."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        return Decimal(value)
    raise TypeError(f"{field_name}: type non supporté ({type(value).__name__}).")


class DiscountType(str, Enum):
    PERCENT = "PERCENT"
    AMOUNT = "AMOUNT"


@dataclass
class CartDiscount:
    """Remise (ligne ou globale). value est soit un pourcentage (ex: 10 pour 10%),
    soit un montant TTC en euros, selon `type`."""

    type: DiscountType
    value: Decimal

    def __post_init__(self):
        self.value = to_decimal(self.value, "CartDiscount.value")
        if self.value < Decimal("0"):
            raise ValueError("CartDiscount.value ne peut pas être négatif.")
        if self.type == DiscountType.PERCENT and self.value > Decimal("100"):
            raise ValueError("CartDiscount.value en pourcentage ne peut pas dépasser 100.")


@dataclass
class CartItem:
    """Ligne d'article dans le panier. Prix unitaire TTC, quantité, taux de TVA (fraction, ex 0.20 pour 20%)."""

    unit_price_ttc: Decimal
    quantity: int
    vat_rate: Decimal
    product_id: Optional[str] = None
    name: str = ""
    discount: Optional[CartDiscount] = None

    def __post_init__(self):
        self.unit_price_ttc = to_decimal(self.unit_price_ttc, "CartItem.unit_price_ttc")
        self.vat_rate = to_decimal(self.vat_rate, "CartItem.vat_rate")

        if isinstance(self.quantity, float):
            raise TypeError("CartItem.quantity: les float sont interdits, utilise un int.")
        self.quantity = int(self.quantity)

        if self.unit_price_ttc < Decimal("0"):
            raise ValueError("CartItem.unit_price_ttc ne peut pas être négatif.")
        if self.quantity <= 0:
            raise ValueError("CartItem.quantity doit être strictement positif.")
        if self.vat_rate < Decimal("0"):
            raise ValueError("CartItem.vat_rate ne peut pas être négatif.")


@dataclass
class VatBreakdownLine:
    """Ventilation de la TVA pour un taux donné."""

    vat_rate: Decimal
    base_ht: Decimal
    montant_tva: Decimal
    montant_ttc: Decimal


@dataclass
class CartTotal:
    """Résultat agrégé du calcul de panier."""

    total_ht: Decimal
    total_tva: Decimal
    total_ttc: Decimal
    total_discount_ttc: Decimal
    vat_breakdown: List[VatBreakdownLine] = field(default_factory=list)
