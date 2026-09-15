# -*- coding: utf-8 -*-
"""Tests unitaires du service de clôture Z (kodo_core.services.closing_service)."""

import sqlite3
from decimal import Decimal

import pytest

from kodo_core.services.closing_service import (
    DuplicateZNumberError,
    compute_z_report,
    ensure_schema,
    record_validated_sale,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    ensure_schema(connection)
    yield connection
    connection.close()


def _sale_20_percent(total_ttc: str, total_ht: str, total_tva: str):
    return [
        (
            Decimal("0.20"),
            Decimal(total_ht),
            Decimal(total_tva),
            Decimal(total_ttc),
        )
    ]


def test_ensure_schema_is_idempotent(conn):
    ensure_schema(conn)
    ensure_schema(conn)


def test_z_report_with_no_sales_has_zero_totals(conn):
    report = compute_z_report(conn, z_number=1)

    assert report.z_number == 1
    assert report.total_ht == Decimal("0.00")
    assert report.total_tva == Decimal("0.00")
    assert report.total_ttc == Decimal("0.00")
    assert report.total_discounts == Decimal("0.00")
    assert report.taxes == []
    assert report.payments == []
    assert report.previous_z_hash is None
    assert report.signature_hash


def test_z_report_aggregates_ht_tva_ttc_from_validated_sales(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
    )
    record_validated_sale(
        conn,
        date_heure="2026-09-10T10:00:00+00:00",
        mode_paiement="CARTE",
        vat_lines=_sale_20_percent("24.00", "20.00", "4.00"),
    )

    report = compute_z_report(conn, z_number=1)

    assert report.total_ht == Decimal("30.00")
    assert report.total_tva == Decimal("6.00")
    assert report.total_ttc == Decimal("36.00")


def test_z_report_ventilates_tva_by_rate(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=[
            (Decimal("0.20"), Decimal("10.00"), Decimal("2.00"), Decimal("12.00")),
            (Decimal("0.055"), Decimal("20.00"), Decimal("1.10"), Decimal("21.10")),
        ],
    )

    report = compute_z_report(conn, z_number=1)

    taxes_by_rate = {tax.taux: tax for tax in report.taxes}
    assert taxes_by_rate[Decimal("0.20")].base_ht == Decimal("10.00")
    assert taxes_by_rate[Decimal("0.20")].montant_tva == Decimal("2.00")
    assert taxes_by_rate[Decimal("0.055")].base_ht == Decimal("20.00")
    assert taxes_by_rate[Decimal("0.055")].montant_tva == Decimal("1.10")


def test_z_report_ventilates_by_payment_mode(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
    )
    record_validated_sale(
        conn,
        date_heure="2026-09-10T10:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("6.00", "5.00", "1.00"),
    )
    record_validated_sale(
        conn,
        date_heure="2026-09-10T11:00:00+00:00",
        mode_paiement="CARTE",
        vat_lines=_sale_20_percent("24.00", "20.00", "4.00"),
    )

    report = compute_z_report(conn, z_number=1)

    payments_by_mode = {payment.mode_paiement: payment for payment in report.payments}
    assert payments_by_mode["ESPECES"].total_ttc == Decimal("18.00")
    assert payments_by_mode["ESPECES"].nombre_transactions == 2
    assert payments_by_mode["CARTE"].total_ttc == Decimal("24.00")
    assert payments_by_mode["CARTE"].nombre_transactions == 1


def test_z_report_sums_total_discounts(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
        remise_ttc=Decimal("3.00"),
    )
    record_validated_sale(
        conn,
        date_heure="2026-09-10T10:00:00+00:00",
        mode_paiement="CARTE",
        vat_lines=_sale_20_percent("24.00", "20.00", "4.00"),
        remise_ttc=Decimal("1.50"),
    )

    report = compute_z_report(conn, z_number=1)
    assert report.total_discounts == Decimal("4.50")


def test_closing_marks_sales_with_z_id_atomically(conn):
    sale_id = record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
    )

    compute_z_report(conn, z_number=1)

    cursor = conn.cursor()
    cursor.execute("SELECT z_id FROM sales WHERE id = ?", (sale_id,))
    assert cursor.fetchone()[0] == 1


def test_closed_sales_are_excluded_from_next_z_report(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
    )
    first_report = compute_z_report(conn, z_number=1)
    assert first_report.total_ttc == Decimal("12.00")

    record_validated_sale(
        conn,
        date_heure="2026-09-10T11:00:00+00:00",
        mode_paiement="CARTE",
        vat_lines=_sale_20_percent("24.00", "20.00", "4.00"),
    )
    second_report = compute_z_report(conn, z_number=2, previous_z_hash=first_report.signature_hash)

    # Le second Z ne doit contenir que la nouvelle vente, pas la vente déjà clôturée.
    assert second_report.total_ttc == Decimal("24.00")


def test_non_validated_sales_are_excluded(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
        statut="ANNULEE",
    )

    report = compute_z_report(conn, z_number=1)
    assert report.total_ttc == Decimal("0.00")


def test_duplicate_z_number_raises_and_does_not_double_close(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
    )
    compute_z_report(conn, z_number=1)

    record_validated_sale(
        conn,
        date_heure="2026-09-10T11:00:00+00:00",
        mode_paiement="CARTE",
        vat_lines=_sale_20_percent("24.00", "20.00", "4.00"),
    )

    with pytest.raises(DuplicateZNumberError):
        compute_z_report(conn, z_number=1)

    # La transaction en échec ne doit pas avoir clôturé la seconde vente.
    cursor = conn.cursor()
    cursor.execute("SELECT z_id FROM sales WHERE mode_paiement = 'CARTE'")
    assert cursor.fetchone()[0] is None


def test_signature_hash_chains_with_previous_z_hash(conn):
    record_validated_sale(
        conn,
        date_heure="2026-09-10T09:00:00+00:00",
        mode_paiement="ESPECES",
        vat_lines=_sale_20_percent("12.00", "10.00", "2.00"),
    )
    first_report = compute_z_report(conn, z_number=1)

    record_validated_sale(
        conn,
        date_heure="2026-09-10T11:00:00+00:00",
        mode_paiement="CARTE",
        vat_lines=_sale_20_percent("24.00", "20.00", "4.00"),
    )
    second_report = compute_z_report(
        conn, z_number=2, previous_z_hash=first_report.signature_hash
    )

    assert second_report.previous_z_hash == first_report.signature_hash
    assert second_report.signature_hash != first_report.signature_hash

    # Rejouer le calcul du second Z avec un previous_z_hash différent doit changer la signature.
    tampered_hash = _compute_hash_with_different_previous(second_report)
    assert tampered_hash != second_report.signature_hash


def _compute_hash_with_different_previous(report) -> str:
    from kodo_core.services.closing_service import _compute_signature_hash

    return _compute_signature_hash(
        report.z_number,
        report.date_debut,
        report.date_fin,
        report.total_ht,
        report.total_tva,
        report.total_ttc,
        report.total_discounts,
        "hash-different",
    )
