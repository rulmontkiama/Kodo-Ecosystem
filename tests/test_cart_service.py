# -*- coding: utf-8 -*-
"""Tests unitaires du moteur de calcul de panier (kodo_core.services.cart_service)."""

from decimal import Decimal

import pytest

from kodo_core.domain.sales.models import CartDiscount, CartItem, DiscountType
from kodo_core.services.cart_service import calculate_cart


def test_empty_cart_returns_zero_totals():
    result = calculate_cart([])
    assert result.total_ht == Decimal("0.00")
    assert result.total_tva == Decimal("0.00")
    assert result.total_ttc == Decimal("0.00")
    assert result.total_discount_ttc == Decimal("0.00")
    assert result.vat_breakdown == []


def test_single_item_no_discount_vat_20_percent():
    item = CartItem(unit_price_ttc=Decimal("12.00"), quantity=1, vat_rate=Decimal("0.20"))
    result = calculate_cart([item])

    assert result.total_ttc == Decimal("12.00")
    assert result.total_ht == Decimal("10.00")
    assert result.total_tva == Decimal("2.00")
    assert result.total_discount_ttc == Decimal("0.00")
    assert len(result.vat_breakdown) == 1
    assert result.vat_breakdown[0].vat_rate == Decimal("0.20")
    assert result.vat_breakdown[0].base_ht == Decimal("10.00")
    assert result.vat_breakdown[0].montant_tva == Decimal("2.00")


def test_multiple_quantities():
    item = CartItem(unit_price_ttc=Decimal("5.50"), quantity=3, vat_rate=Decimal("0.055"))
    result = calculate_cart([item])
    assert result.total_ttc == Decimal("16.50")


def test_line_discount_percent():
    item = CartItem(
        unit_price_ttc=Decimal("100.00"),
        quantity=1,
        vat_rate=Decimal("0.20"),
        discount=CartDiscount(type=DiscountType.PERCENT, value=Decimal("10")),
    )
    result = calculate_cart([item])
    assert result.total_ttc == Decimal("90.00")
    assert result.total_discount_ttc == Decimal("10.00")


def test_line_discount_amount():
    item = CartItem(
        unit_price_ttc=Decimal("100.00"),
        quantity=1,
        vat_rate=Decimal("0.20"),
        discount=CartDiscount(type=DiscountType.AMOUNT, value=Decimal("15.00")),
    )
    result = calculate_cart([item])
    assert result.total_ttc == Decimal("85.00")
    assert result.total_discount_ttc == Decimal("15.00")


def test_line_discount_amount_cannot_go_negative():
    item = CartItem(
        unit_price_ttc=Decimal("10.00"),
        quantity=1,
        vat_rate=Decimal("0.20"),
        discount=CartDiscount(type=DiscountType.AMOUNT, value=Decimal("50.00")),
    )
    result = calculate_cart([item])
    assert result.total_ttc == Decimal("0.00")


def test_global_discount_percent_applies_to_all_lines():
    items = [
        CartItem(unit_price_ttc=Decimal("100.00"), quantity=1, vat_rate=Decimal("0.20")),
        CartItem(unit_price_ttc=Decimal("50.00"), quantity=1, vat_rate=Decimal("0.10")),
    ]
    global_discount = CartDiscount(type=DiscountType.PERCENT, value=Decimal("10"))
    result = calculate_cart(items, global_discount=global_discount)

    assert result.total_ttc == Decimal("135.00")
    assert result.total_discount_ttc == Decimal("15.00")


def test_global_discount_amount_ventilated_proportionally_preserves_vat_split():
    items = [
        CartItem(unit_price_ttc=Decimal("80.00"), quantity=1, vat_rate=Decimal("0.20")),
        CartItem(unit_price_ttc=Decimal("20.00"), quantity=1, vat_rate=Decimal("0.055")),
    ]
    global_discount = CartDiscount(type=DiscountType.AMOUNT, value=Decimal("10.00"))
    result = calculate_cart(items, global_discount=global_discount)

    # Le total TTC doit refléter exactement la remise globale de 10.00€.
    assert result.total_ttc == Decimal("90.00")
    assert result.total_discount_ttc == Decimal("10.00")

    # La somme des bases HT + TVA ventilées doit reconstituer le TTC total (au centime).
    reconstructed_ttc = sum(
        (entry.montant_ttc for entry in result.vat_breakdown), Decimal("0.00")
    )
    assert reconstructed_ttc == result.total_ttc

    # Deux taux de TVA distincts doivent apparaître.
    rates = {entry.vat_rate for entry in result.vat_breakdown}
    assert rates == {Decimal("0.20"), Decimal("0.055")}


def test_global_discount_amount_reconciles_rounding_on_last_line():
    # 3 lignes égales de 10.00€, remise globale de 10.00€ (répartition non ronde: 3.33/3.33/3.34)
    items = [
        CartItem(unit_price_ttc=Decimal("10.00"), quantity=1, vat_rate=Decimal("0.20"))
        for _ in range(3)
    ]
    global_discount = CartDiscount(type=DiscountType.AMOUNT, value=Decimal("10.00"))
    result = calculate_cart(items, global_discount=global_discount)

    assert result.total_ttc == Decimal("20.00")
    assert result.total_discount_ttc == Decimal("10.00")


def test_global_discount_amount_capped_at_total():
    item = CartItem(unit_price_ttc=Decimal("10.00"), quantity=1, vat_rate=Decimal("0.20"))
    global_discount = CartDiscount(type=DiscountType.AMOUNT, value=Decimal("999.00"))
    result = calculate_cart([item], global_discount=global_discount)

    assert result.total_ttc == Decimal("0.00")
    assert result.total_discount_ttc == Decimal("10.00")


def test_vat_breakdown_groups_same_rate_across_lines():
    items = [
        CartItem(unit_price_ttc=Decimal("10.00"), quantity=1, vat_rate=Decimal("0.20")),
        CartItem(unit_price_ttc=Decimal("20.00"), quantity=1, vat_rate=Decimal("0.20")),
    ]
    result = calculate_cart(items)
    assert len(result.vat_breakdown) == 1
    assert result.vat_breakdown[0].montant_ttc == Decimal("30.00")


def test_float_unit_price_raises_type_error():
    with pytest.raises(TypeError):
        CartItem(unit_price_ttc=12.0, quantity=1, vat_rate=Decimal("0.20"))


def test_float_vat_rate_raises_type_error():
    with pytest.raises(TypeError):
        CartItem(unit_price_ttc=Decimal("12.00"), quantity=1, vat_rate=0.20)


def test_float_quantity_raises_type_error():
    with pytest.raises(TypeError):
        CartItem(unit_price_ttc=Decimal("12.00"), quantity=1.0, vat_rate=Decimal("0.20"))


def test_float_discount_value_raises_type_error():
    with pytest.raises(TypeError):
        CartDiscount(type=DiscountType.PERCENT, value=10.0)


def test_string_amounts_are_accepted_and_converted():
    item = CartItem(unit_price_ttc="12.00", quantity=1, vat_rate="0.20")
    result = calculate_cart([item])
    assert result.total_ttc == Decimal("12.00")


def test_negative_unit_price_raises_value_error():
    with pytest.raises(ValueError):
        CartItem(unit_price_ttc=Decimal("-1.00"), quantity=1, vat_rate=Decimal("0.20"))


def test_zero_quantity_raises_value_error():
    with pytest.raises(ValueError):
        CartItem(unit_price_ttc=Decimal("1.00"), quantity=0, vat_rate=Decimal("0.20"))


def test_percent_discount_over_100_raises_value_error():
    with pytest.raises(ValueError):
        CartDiscount(type=DiscountType.PERCENT, value=Decimal("150"))
