# -*- coding: utf-8 -*-
"""Tests unitaires du service de crash recovery du panier (kodo_core.services.crash_recovery)."""

import json
import os
from decimal import Decimal

import pytest

from kodo_core.domain.sales.models import Cart, CartDiscount, CartItem, DiscountType
from kodo_core.services.crash_recovery import CrashRecoveryService


@pytest.fixture
def session_path(tmp_path):
    return str(tmp_path / "panier_session.json")


@pytest.fixture
def service(session_path):
    return CrashRecoveryService(session_path=session_path)


def make_cart():
    item = CartItem(
        unit_price_ttc=Decimal("12.50"),
        quantity=2,
        vat_rate=Decimal("0.20"),
        product_id="SKU-1",
        name="T-shirt",
        discount=CartDiscount(type=DiscountType.PERCENT, value=Decimal("10")),
    )
    return Cart(items=[item], global_discount=CartDiscount(type=DiscountType.AMOUNT, value=Decimal("1.00")))


def test_no_pending_recovery_when_no_snapshot(service):
    assert service.has_pending_recovery() is False


def test_save_snapshot_creates_pending_recovery(service):
    service.save_snapshot(make_cart())
    assert service.has_pending_recovery() is True


def test_save_snapshot_uses_atomic_write_no_leftover_tmp(service, session_path):
    service.save_snapshot(make_cart())
    assert not os.path.exists(f"{session_path}.tmp")
    assert os.path.exists(session_path)


def test_restore_cart_session_roundtrip(service):
    original = make_cart()
    service.save_snapshot(original)

    restored = service.restore_cart_session()

    assert len(restored.items) == 1
    restored_item = restored.items[0]
    assert restored_item.unit_price_ttc == Decimal("12.50")
    assert restored_item.quantity == 2
    assert restored_item.vat_rate == Decimal("0.20")
    assert restored_item.product_id == "SKU-1"
    assert restored_item.name == "T-shirt"
    assert restored_item.discount.type == DiscountType.PERCENT
    assert restored_item.discount.value == Decimal("10")

    assert restored.global_discount.type == DiscountType.AMOUNT
    assert restored.global_discount.value == Decimal("1.00")


def test_restore_cart_without_discounts(service):
    cart = Cart(items=[CartItem(unit_price_ttc=Decimal("5.00"), quantity=1, vat_rate=Decimal("0.055"))])
    service.save_snapshot(cart)

    restored = service.restore_cart_session()

    assert restored.items[0].discount is None
    assert restored.global_discount is None


def test_clear_session_removes_file(service):
    service.save_snapshot(make_cart())
    assert service.has_pending_recovery() is True

    service.clear_session()

    assert service.has_pending_recovery() is False


def test_clear_session_is_idempotent_when_no_file(service):
    service.clear_session()
    service.clear_session()
    assert service.has_pending_recovery() is False


def test_save_snapshot_overwrites_previous_snapshot(service):
    service.save_snapshot(make_cart())
    new_cart = Cart(items=[CartItem(unit_price_ttc=Decimal("1.00"), quantity=1, vat_rate=Decimal("0.20"))])
    service.save_snapshot(new_cart)

    restored = service.restore_cart_session()
    assert len(restored.items) == 1
    assert restored.items[0].unit_price_ttc == Decimal("1.00")


def test_snapshot_content_is_valid_json_with_decimals_as_strings(service, session_path):
    service.save_snapshot(make_cart())

    with open(session_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["items"][0]["unit_price_ttc"] == "12.50"
    assert data["global_discount"]["value"] == "1.00"


def test_default_session_path_lives_under_shopconfig_sessions_dir(monkeypatch, tmp_path):
    from kodo_core.config import ShopConfig

    monkeypatch.setattr(ShopConfig, "get_sessions_dir", classmethod(lambda cls: str(tmp_path)))

    default_service = CrashRecoveryService()

    assert default_service.session_path == str(tmp_path / "panier_session.json")
