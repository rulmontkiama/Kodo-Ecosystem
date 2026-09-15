# -*- coding: utf-8 -*-
"""
Moteur de calcul de panier - pur, sans dépendance UI ni BDD.
Toutes les valeurs sont manipulées en decimal.Decimal, arrondies ROUND_HALF_UP à 2 décimales.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from kodo_core.domain.sales.models import (
    CartDiscount,
    CartItem,
    CartTotal,
    DiscountType,
    VatBreakdownLine,
)

TWO_DECIMALS = Decimal("0.01")
HUNDRED = Decimal("100")


def quantize_money(amount: Decimal) -> Decimal:
    """Arrondit un montant monétaire à 2 décimales (ROUND_HALF_UP)."""
    return amount.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


def _apply_line_discount(item: CartItem) -> Decimal:
    """Retourne le total TTC de la ligne, après remise de ligne, arrondi à 2 décimales."""
    line_total_ttc = item.unit_price_ttc * Decimal(item.quantity)

    if item.discount is not None:
        if item.discount.type == DiscountType.PERCENT:
            line_total_ttc = line_total_ttc * (Decimal("1") - item.discount.value / HUNDRED)
        else:  # AMOUNT
            line_total_ttc = max(Decimal("0"), line_total_ttc - item.discount.value)

    return quantize_money(line_total_ttc)


def _apply_global_discount(
    lines_ttc: List[Decimal], global_discount: Optional[CartDiscount]
) -> List[Decimal]:
    """Ventile la remise globale sur les lignes (proportionnellement si en montant)."""
    if global_discount is None or not lines_ttc:
        return list(lines_ttc)

    if global_discount.type == DiscountType.PERCENT:
        factor = Decimal("1") - global_discount.value / HUNDRED
        return [quantize_money(line * factor) for line in lines_ttc]

    # AMOUNT: ventilation proportionnelle avec réconciliation de l'arrondi sur la dernière ligne.
    total_before = sum(lines_ttc, Decimal("0"))
    discount_amount = min(global_discount.value, total_before)

    if total_before == Decimal("0"):
        return list(lines_ttc)

    result = []
    allocated_so_far = Decimal("0")
    for index, line in enumerate(lines_ttc):
        is_last = index == len(lines_ttc) - 1
        if is_last:
            allocation = discount_amount - allocated_so_far
        else:
            allocation = quantize_money(line / total_before * discount_amount)
            allocated_so_far += allocation
        result.append(max(Decimal("0"), line - allocation))
    return result


def calculate_cart(
    items: List[CartItem], global_discount: Optional[CartDiscount] = None
) -> CartTotal:
    """Calcule les totaux d'un panier (HT/TVA/TTC ventilés par taux), remises comprises."""
    if not items:
        return CartTotal(
            total_ht=Decimal("0.00"),
            total_tva=Decimal("0.00"),
            total_ttc=Decimal("0.00"),
            total_discount_ttc=Decimal("0.00"),
            vat_breakdown=[],
        )

    gross_total_ttc = sum(
        (item.unit_price_ttc * Decimal(item.quantity) for item in items), Decimal("0")
    )

    lines_after_line_discount = [_apply_line_discount(item) for item in items]
    lines_final_ttc = _apply_global_discount(lines_after_line_discount, global_discount)

    breakdown_by_rate = {}
    total_ht = Decimal("0.00")
    total_tva = Decimal("0.00")
    total_ttc = Decimal("0.00")

    for item, line_ttc in zip(items, lines_final_ttc):
        line_ht = quantize_money(line_ttc / (Decimal("1") + item.vat_rate))
        line_tva = line_ttc - line_ht

        total_ht += line_ht
        total_tva += line_tva
        total_ttc += line_ttc

        rate_key = item.vat_rate
        if rate_key not in breakdown_by_rate:
            breakdown_by_rate[rate_key] = VatBreakdownLine(
                vat_rate=rate_key,
                base_ht=Decimal("0.00"),
                montant_tva=Decimal("0.00"),
                montant_ttc=Decimal("0.00"),
            )
        entry = breakdown_by_rate[rate_key]
        entry.base_ht += line_ht
        entry.montant_tva += line_tva
        entry.montant_ttc += line_ttc

    total_discount_ttc = quantize_money(gross_total_ttc - total_ttc)

    return CartTotal(
        total_ht=total_ht,
        total_tva=total_tva,
        total_ttc=total_ttc,
        total_discount_ttc=total_discount_ttc,
        vat_breakdown=sorted(breakdown_by_rate.values(), key=lambda entry: entry.vat_rate),
    )
