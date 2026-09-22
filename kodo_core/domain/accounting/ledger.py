# -*- coding: utf-8 -*-
"""
Modèle métier pur du journal fiscal inaltérable (fiscal_ledger) - Kōdo POS Core.
Conforme aux exigences anti-fraude (NF525/LNE) : séquence stricte sans trou,
chaînage cryptographique SHA-256 des tickets. Aucune dépendance UI ni BDD.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
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


def _montant_canonique(valeur) -> str:
    """
    Représentation textuelle d'un montant DANS la chaîne scellée. Deux pièges y étaient posés.

    1. `Decimal(valeur)` sur un `float` prend la valeur BINAIRE exacte du flottant, pas le
       nombre écrit. Un même montant scellé depuis un Decimal puis relu en flottant ne
       produisait donc pas le même texte, donc pas le même hash : la chaîne devenait
       invérifiable alors que rien n'avait été falsifié. On passe par `str()`, le texte
       réellement porté par la valeur.
    2. `f"{x:.2f}"` n'arrondit pas comme le reste du logiciel : c'est l'arrondi *bancaire*
       (`ROUND_HALF_EVEN`), qui renvoie 8.34 là où le projet retient 8.35 (cf. la référence
       unique d'arrondi, `kodo_core/domain/sales/models.quantize_money`). Le scellement se
       fait sur des montants déjà arrondis à deux décimales par `seal_sale`, donc aucune
       empreinte existante ne change ; mais la divergence n'a plus lieu d'être ici.
    """
    montant = valeur if isinstance(valeur, Decimal) else Decimal(str(valeur))
    return str(montant.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build_hash_payload(sequence_number: str, timestamp_utc: str, total_ttc: Decimal, total_tva: Decimal, previous_hash: str) -> str:
    """Construit la chaîne canonique scellée par le hash SHA-256 d'une entrée du ledger fiscal."""
    ttc_str = _montant_canonique(total_ttc)
    tva_str = _montant_canonique(total_tva)
    return f"{sequence_number}|{timestamp_utc}|{ttc_str}|{tva_str}|{previous_hash}"
