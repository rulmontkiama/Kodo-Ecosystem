"""Tests unitaires pour kodo_core/hardware/printer_service.py (flux simulés, pas d'accès matériel réel)."""
import subprocess
import sys
from decimal import Decimal

import pytest

from kodo_core.hardware import printer_service


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------------------
# check_printer_status
# ---------------------------------------------------------------------------

def test_check_printer_status_ready(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        return _FakeCompletedProcess(0, b"printer ThermalPOS is idle.  enabled since Mon")

    monkeypatch.setattr(subprocess, "run", fake_run)

    is_ready, status = printer_service.check_printer_status("ThermalPOS")
    assert is_ready is True
    assert status == "ready"


def test_check_printer_status_busy(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        return _FakeCompletedProcess(0, b"printer ThermalPOS now printing ThermalPOS-1.")

    monkeypatch.setattr(subprocess, "run", fake_run)

    is_ready, status = printer_service.check_printer_status("ThermalPOS")
    assert is_ready is False
    assert status == "busy"


def test_check_printer_status_offline_disabled(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        return _FakeCompletedProcess(0, b"printer ThermalPOS disabled since Mon - reason unknown")

    monkeypatch.setattr(subprocess, "run", fake_run)

    is_ready, status = printer_service.check_printer_status("ThermalPOS")
    assert is_ready is False
    assert status == "offline"


def test_check_printer_status_command_failure(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        return _FakeCompletedProcess(1, b"lpstat: unknown printer")

    monkeypatch.setattr(subprocess, "run", fake_run)

    is_ready, status = printer_service.check_printer_status("Ghost")
    assert is_ready is False
    assert status == "offline"


def test_check_printer_status_exception(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        raise FileNotFoundError("lpstat not found")

    monkeypatch.setattr(subprocess, "run", fake_run)

    is_ready, status = printer_service.check_printer_status("ThermalPOS")
    assert is_ready is False
    assert status == "unknown"


def test_check_printer_status_empty_name():
    is_ready, status = printer_service.check_printer_status("")
    assert is_ready is False
    assert status == "unknown"


# ---------------------------------------------------------------------------
# generate_esc_pos_receipt
# ---------------------------------------------------------------------------

def _sample_ticket_data():
    return {
        "shop_name": "Kodo POS",
        "numero": "0001",
        "items": [
            {"qty": 2, "label": "Café", "total": Decimal("5.00")},
            {"qty": 1, "label": "Croissant", "total": Decimal("1.50")},
        ],
        "vat_breakdown": [
            {"rate": "10", "base": Decimal("5.91"), "amount": Decimal("0.59")},
        ],
        "total_ttc": Decimal("6.50"),
    }


def test_generate_esc_pos_receipt_returns_bytes():
    receipt = printer_service.generate_esc_pos_receipt(_sample_ticket_data(), "abc123fiscalhash")
    assert isinstance(receipt, bytes)


def test_generate_esc_pos_receipt_starts_with_init_and_ends_with_cut():
    receipt = printer_service.generate_esc_pos_receipt(_sample_ticket_data(), "abc123fiscalhash")
    assert receipt.startswith(printer_service.ESC_INIT)
    assert receipt.rstrip(b"\n").endswith(printer_service.GS_CUT_FUNCTION) or printer_service.GS_CUT_FUNCTION in receipt
    assert receipt.endswith(printer_service.GS_CUT_FUNCTION)


def test_generate_esc_pos_receipt_contains_fiscal_hash():
    fiscal_hash = "sealed-hash-xyz"
    receipt = printer_service.generate_esc_pos_receipt(_sample_ticket_data(), fiscal_hash)
    assert fiscal_hash.encode("ascii") in receipt


def test_generate_esc_pos_receipt_contains_items_and_total():
    receipt = printer_service.generate_esc_pos_receipt(_sample_ticket_data(), "hash")
    assert b"Croissant" in receipt
    assert b"6.50" in receipt


def test_generate_esc_pos_receipt_contains_vat_breakdown():
    receipt = printer_service.generate_esc_pos_receipt(_sample_ticket_data(), "hash")
    assert b"TVA 10%" in receipt


def test_generate_esc_pos_receipt_contains_barcode_sequence():
    receipt = printer_service.generate_esc_pos_receipt(_sample_ticket_data(), "hash")
    assert (printer_service.GS + b"k" + bytes([73])) in receipt


def test_generate_esc_pos_receipt_empty_items():
    data = _sample_ticket_data()
    data["items"] = []
    receipt = printer_service.generate_esc_pos_receipt(data, "hash")
    assert isinstance(receipt, bytes)
    assert receipt.endswith(printer_service.GS_CUT_FUNCTION)


# ---------------------------------------------------------------------------
# open_cash_drawer_sequence
# ---------------------------------------------------------------------------

def test_open_cash_drawer_sequence_matches_standard_pulse():
    sequence = printer_service.open_cash_drawer_sequence()
    assert isinstance(sequence, bytes)
    assert sequence == printer_service.ESC_INIT + b'\x1bp\x00\x19\xfa'


def test_open_cash_drawer_sequence_contains_esc_p_command():
    sequence = printer_service.open_cash_drawer_sequence()
    assert b'\x1bp\x00\x19\xfa' in sequence
