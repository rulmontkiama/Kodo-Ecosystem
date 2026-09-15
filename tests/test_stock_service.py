# -*- coding: utf-8 -*-
"""Tests unitaires du service de gestion des stocks (kodo_core.services.stock_service)."""

import sqlite3

import pytest

from kodo_core.domain.catalog.models import StockMovementType
from kodo_core.services.stock_service import (
    InsufficientStockError,
    adjust_stock,
    decrement_stock_on_sale,
    ensure_schema,
    get_current_stock,
    increment_stock_on_return,
    initialize_stock_item,
    verifier_alertes_stock,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    ensure_schema(connection)
    yield connection
    connection.close()


def test_ensure_schema_is_idempotent(conn):
    ensure_schema(conn)
    ensure_schema(conn)


def test_initialize_stock_item_sets_quantity_and_threshold(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=10, seuil_alerte=2)
    assert get_current_stock(conn, "PROD-1") == 10


def test_decrement_stock_on_sale_reduces_quantity(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=10, seuil_alerte=2)
    movement = decrement_stock_on_sale(conn, "PROD-1", quantity=3, motif="Vente ticket #42")

    assert get_current_stock(conn, "PROD-1") == 7
    assert movement.delta == -3
    assert movement.stock_final == 7
    assert movement.type_mouvement == StockMovementType.VENTE
    assert movement.motif == "Vente ticket #42"
    assert movement.date_heure


def test_decrement_stock_on_sale_without_existing_item_starts_from_zero_and_raises(conn):
    with pytest.raises(InsufficientStockError):
        decrement_stock_on_sale(conn, "PROD-UNKNOWN", quantity=1)


def test_decrement_stock_raises_when_insufficient(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=2, seuil_alerte=0)
    with pytest.raises(InsufficientStockError):
        decrement_stock_on_sale(conn, "PROD-1", quantity=5)

    # Le stock ne doit pas avoir été modifié par la tentative échouée.
    assert get_current_stock(conn, "PROD-1") == 2


def test_decrement_stock_zero_or_negative_quantity_raises_value_error(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=10, seuil_alerte=0)
    with pytest.raises(ValueError):
        decrement_stock_on_sale(conn, "PROD-1", quantity=0)
    with pytest.raises(ValueError):
        decrement_stock_on_sale(conn, "PROD-1", quantity=-1)


def test_increment_stock_on_return_increases_quantity(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=5, seuil_alerte=0)
    movement = increment_stock_on_return(conn, "PROD-1", quantity=2, motif="Retour client")

    assert get_current_stock(conn, "PROD-1") == 7
    assert movement.delta == 2
    assert movement.type_mouvement == StockMovementType.RETOUR


def test_movements_are_traced_in_audit_table(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=10, seuil_alerte=0)
    decrement_stock_on_sale(conn, "PROD-1", quantity=3, motif="Vente")
    increment_stock_on_return(conn, "PROD-1", quantity=1, motif="Retour")

    cursor = conn.cursor()
    cursor.execute(
        "SELECT product_id, type_mouvement, delta, stock_final, motif FROM stock_movements "
        "ORDER BY id ASC"
    )
    rows = [tuple(row) for row in cursor.fetchall()]

    assert len(rows) == 2
    assert rows[0] == ("PROD-1", "VENTE", -3, 7, "Vente")
    assert rows[1] == ("PROD-1", "RETOUR", 1, 8, "Retour")


def test_variation_id_tracks_independent_stock_lines(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=5, seuil_alerte=0, variation_id="M")
    initialize_stock_item(conn, "PROD-1", quantite_initiale=8, seuil_alerte=0, variation_id="L")

    decrement_stock_on_sale(conn, "PROD-1", quantity=2, variation_id="M")

    assert get_current_stock(conn, "PROD-1", variation_id="M") == 3
    assert get_current_stock(conn, "PROD-1", variation_id="L") == 8


def test_adjust_stock_allows_manual_correction(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=10, seuil_alerte=0)
    movement = adjust_stock(
        conn, "PROD-1", delta=-2, type_mouvement=StockMovementType.INVENTAIRE, motif="Inventaire annuel"
    )

    assert get_current_stock(conn, "PROD-1") == 8
    assert movement.type_mouvement == StockMovementType.INVENTAIRE


def test_adjust_stock_allows_going_negative(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=1, seuil_alerte=0)
    adjust_stock(conn, "PROD-1", delta=-5, motif="Correction")
    assert get_current_stock(conn, "PROD-1") == -4


def test_verifier_alertes_stock_returns_items_at_or_below_threshold(conn):
    initialize_stock_item(conn, "PROD-LOW", quantite_initiale=1, seuil_alerte=2)
    initialize_stock_item(conn, "PROD-OK", quantite_initiale=10, seuil_alerte=2)
    initialize_stock_item(conn, "PROD-EXACT", quantite_initiale=3, seuil_alerte=3)

    alerts = verifier_alertes_stock(conn)
    alert_product_ids = {alert.product_id for alert in alerts}

    assert alert_product_ids == {"PROD-LOW", "PROD-EXACT"}
    assert all(alert.stock_actuel <= alert.stock_alerte for alert in alerts)


def test_verifier_alertes_stock_updates_after_sale_triggers_alert(conn):
    initialize_stock_item(conn, "PROD-1", quantite_initiale=5, seuil_alerte=3)
    assert verifier_alertes_stock(conn) == []

    decrement_stock_on_sale(conn, "PROD-1", quantity=3)

    alerts = verifier_alertes_stock(conn)
    assert len(alerts) == 1
    assert alerts[0].product_id == "PROD-1"
    assert alerts[0].stock_actuel == 2
