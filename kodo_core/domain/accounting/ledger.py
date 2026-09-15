# -*- coding: utf-8 -*-
"""
Modèle métier pur du journal fiscal inaltérable (fiscal_ledger) - Kōdo POS Core.
Conforme aux exigences anti-fraude (NF525/LNE) : séquence stricte sans trou,
chaînage cryptographique SHA-256 des tickets. Aucune dépendance UI ni BDD.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


GENESIS_HASH = "GENESIS"


@dataclass
class FiscalLedgerEntry:
    """Une entrée scellée du journal fiscal inaltérable des ventes."""

    id: Optional[int]
    sequence_number: str
    sale_id: int
    timestamp_utc: str
    total_ttc: Decimal
    total_ht: Decimal
    total_tva: Decimal
    previous_hash: str
    current_hash: str
    signature: str


def format_sequence_number(index: int, year: int) -> str:
    """Formate le numéro de séquence strict sans trou : TCK-YYYY-NNNN."""
    return f"TCK-{year}-{index:04d}"


def build_hash_payload(sequence_number: str, timestamp_utc: str, total_ttc: Decimal, total_tva: Decimal, previous_hash: str) -> str:
    """Construit la chaîne canonique scellée par le hash SHA-256 d'une entrée du ledger fiscal."""
    ttc_str = f"{Decimal(total_ttc):.2f}"
    tva_str = f"{Decimal(total_tva):.2f}"
    return f"{sequence_number}|{timestamp_utc}|{ttc_str}|{tva_str}|{previous_hash}"
