# -*- coding: utf-8 -*-
"""Tests unitaires du journal fiscal inaltérable (kodo_core.services.fiscal_service)."""

import sqlite3
from decimal import Decimal

import pytest

from kodo_core.services.fiscal_service import (
    FiscalTamperDetectedError,
    audit_ledger_integrity,
    ensure_schema,
    seal_sale,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    ensure_schema(connection)
    yield connection
    connection.close()


def _totals(ttc: str, ht: str, tva: str, timestamp: str = "2026-09-10T09:00:00+00:00"):
    return {
        "total_ttc": Decimal(ttc),
        "total_ht": Decimal(ht),
        "total_tva": Decimal(tva),
        "timestamp_utc": timestamp,
    }


def test_ensure_schema_is_idempotent(conn):
    ensure_schema(conn)
    ensure_schema(conn)


def test_first_sale_is_sealed_with_genesis_previous_hash(conn):
    entry = seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))

    assert entry["sequence_number"] == "TCK-2026-0001"
    assert entry["previous_hash"] == "GENESIS"
    assert entry["current_hash"]
    assert entry["signature"]


def test_sequence_number_increments_without_gap(conn):
    e1 = seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))
    e2 = seal_sale(conn, sale_id=2, totals=_totals("24.00", "20.00", "4.00"))
    e3 = seal_sale(conn, sale_id=3, totals=_totals("6.00", "5.00", "1.00"))

    assert [e1["sequence_number"], e2["sequence_number"], e3["sequence_number"]] == [
        "TCK-2026-0001",
        "TCK-2026-0002",
        "TCK-2026-0003",
    ]


def test_chain_links_current_hash_to_next_previous_hash(conn):
    e1 = seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))
    e2 = seal_sale(conn, sale_id=2, totals=_totals("24.00", "20.00", "4.00"))

    assert e2["previous_hash"] == e1["current_hash"]


def test_audit_passes_on_untampered_chain(conn):
    seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))
    seal_sale(conn, sale_id=2, totals=_totals("24.00", "20.00", "4.00"))
    seal_sale(conn, sale_id=3, totals=_totals("6.00", "5.00", "1.00"))

    assert audit_ledger_integrity(conn) is True


def test_audit_passes_on_empty_ledger(conn):
    assert audit_ledger_integrity(conn) is True


def test_audit_detects_tampered_amount(conn):
    seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))
    seal_sale(conn, sale_id=2, totals=_totals("24.00", "20.00", "4.00"))
    seal_sale(conn, sale_id=3, totals=_totals("6.00", "5.00", "1.00"))

    conn.execute("UPDATE fiscal_ledger SET total_ttc = '999.00' WHERE sequence_number = 'TCK-2026-0002'")
    conn.commit()

    with pytest.raises(FiscalTamperDetectedError) as excinfo:
        audit_ledger_integrity(conn)

    assert excinfo.value.sequence_number == "TCK-2026-0002"


def test_audit_detects_tampered_hash(conn):
    seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))
    seal_sale(conn, sale_id=2, totals=_totals("24.00", "20.00", "4.00"))

    conn.execute("UPDATE fiscal_ledger SET current_hash = 'deadbeef' WHERE sequence_number = 'TCK-2026-0001'")
    conn.commit()

    with pytest.raises(FiscalTamperDetectedError) as excinfo:
        audit_ledger_integrity(conn)

    assert excinfo.value.sequence_number == "TCK-2026-0001"


def test_audit_detects_sequence_gap(conn):
    seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))
    seal_sale(conn, sale_id=2, totals=_totals("24.00", "20.00", "4.00"))
    seal_sale(conn, sale_id=3, totals=_totals("6.00", "5.00", "1.00"))

    conn.execute("DELETE FROM fiscal_ledger WHERE sequence_number = 'TCK-2026-0002'")
    conn.commit()

    with pytest.raises(FiscalTamperDetectedError) as excinfo:
        audit_ledger_integrity(conn)

    assert excinfo.value.sequence_number == "TCK-2026-0003"


def test_seal_sale_is_atomic_on_failure(conn):
    seal_sale(conn, sale_id=1, totals=_totals("12.00", "10.00", "2.00"))

    with pytest.raises(KeyError):
        seal_sale(conn, sale_id=2, totals={"total_ttc": Decimal("24.00")})

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM fiscal_ledger")
    assert cursor.fetchone()[0] == 1
