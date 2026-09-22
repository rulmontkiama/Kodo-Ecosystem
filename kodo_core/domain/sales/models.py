# -*- coding: utf-8 -*-
"""
Modèles métier purs du panier de vente - Kōdo POS Core.
Aucune dépendance UI ni BDD. Toute valeur monétaire est un decimal.Decimal.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import List, Optional

TWO_DECIMALS = Decimal("0.01")


def quantize_money(value) -> Decimal:
    """Arrondit un montant monétaire à 2 décimales (ROUND_HALF_UP). RÉFÉRENCE UNIQUE du projet.

    Ce module est le seul de la chaîne comptable à ne dépendre de rien (ni BDD, ni UI) :
    c'est pourquoi la référence est ancrée ici, et réexportée par cart_engine, cart_service,
    closing_service, fiscal_service, z_report et printer_service. Il y avait auparavant cinq
    implémentations divergentes, et un même montant ne s'arrondissait pas pareil selon le
    module qui le traitait :

      - `Decimal(value)` sur un float prenait la valeur BINAIRE exacte du float. 2.675 est
        stocké 2.67499999... en binaire, donc arrondi à 2.67 au lieu de 2.68 : un centime
        d'écart, et c'était le chemin du scellement fiscal (fiscal_service).
      - `value.quantize(...)` sans conversion plantait (AttributeError) dès qu'un appelant
        historique passait un float, une str ou une valeur sortie de SQLite.
      - `f"{value:.2f}"` (printer_service) ne quantifiait pas : il formatait avec l'arrondi
        BANQUIER de Python, et imprimait 8.345 -> 8.34 sur le ticket remis à la cliente.

    On passe donc toujours par `str(value)` : c'est la valeur DÉCIMALE écrite par l'appelant
    (`str(2.675)` == "2.675"), jamais son approximation binaire. `None` vaut 0.00, parce que
    SQLite rend NULL sur un cumul vide et qu'un bilan ne doit pas planter pour autant.
    """
    if value is None:
        return Decimal("0.00")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


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


def decimal_from_json(value, field_name: str) -> Decimal:
    """Convertit une valeur issue d'un payload JSON en Decimal, sans perte.

    `to_decimal` refuse les float À DESSEIN : à l'intérieur du domaine, un montant qui arrive
    en float est un bug d'appelant. Mais en frontière JSON on ne choisit pas le type : un
    client qui écrit `{"unit_price_ttc": 19.99}` produit un float, et json.loads le rend tel
    quel. Les `from_dict` emballaient cette valeur dans `Decimal(...)` AVANT que le garde-fou
    de `__post_init__` puisse voir un float : celui-ci ne se déclenchait donc jamais, et le
    prix était stocké 19.98999999999999843680598132777959108352661132812500.

    On convertit ici via `str()` — le texte décimal que le client a réellement écrit — plutôt
    que de refuser durement : `to_dict` sérialise en str, donc l'aller-retour canonique est
    déjà textuel, et un refus casserait tout appelant JSON qui envoie un nombre. Le garde-fou
    strict reste entier sur le chemin direct (construction d'un CartItem en Python).
    """
    if isinstance(value, float):
        return Decimal(str(value))
    return to_decimal(value, field_name)


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

    def to_dict(self) -> dict:
        return {"type": self.type.value, "value": str(self.value)}

    @classmethod
    def from_dict(cls, data: dict) -> "CartDiscount":
        return cls(
            type=DiscountType(data["type"]),
            value=decimal_from_json(data["value"], "CartDiscount.value"),
        )


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

    def to_dict(self) -> dict:
        return {
            "unit_price_ttc": str(self.unit_price_ttc),
            "quantity": self.quantity,
            "vat_rate": str(self.vat_rate),
            "product_id": self.product_id,
            "name": self.name,
            "discount": self.discount.to_dict() if self.discount is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CartItem":
        discount_data = data.get("discount")
        return cls(
            unit_price_ttc=decimal_from_json(data["unit_price_ttc"], "CartItem.unit_price_ttc"),
            quantity=data["quantity"],
            vat_rate=decimal_from_json(data["vat_rate"], "CartItem.vat_rate"),
            product_id=data.get("product_id"),
            name=data.get("name", ""),
            discount=CartDiscount.from_dict(discount_data) if discount_data else None,
        )


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


@dataclass
class Cart:
    """Panier de vente en cours : lignes + remise globale éventuelle."""

    items: List[CartItem] = field(default_factory=list)
    global_discount: Optional[CartDiscount] = None

    def to_dict(self) -> dict:
        return {
            "items": [item.to_dict() for item in self.items],
            "global_discount": (
                self.global_discount.to_dict() if self.global_discount is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Cart":
        global_discount_data = data.get("global_discount")
        return cls(
            items=[CartItem.from_dict(item) for item in data.get("items", [])],
            global_discount=(
                CartDiscount.from_dict(global_discount_data)
                if global_discount_data
                else None
            ),
        )
