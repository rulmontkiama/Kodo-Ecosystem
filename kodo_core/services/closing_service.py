# -*- coding: utf-8 -*-
"""
Service de clôture comptable Z étanche - Kōdo POS Core.
Agrège les ventes validées non clôturées depuis le dernier Z, calcule la ventilation
TVA / modes de règlement en Decimal, et scelle le rapport par chaînage SHA-256.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import List, Optional, Sequence, Tuple

from kodo_core.db.connection import db_transaction
from kodo_core.domain.accounting.models import PaymentBreakdown, TaxBreakdown, ZReport

TWO_DECIMALS = Decimal("0.01")


class DuplicateZNumberError(Exception):
    """Levée quand un rapport Z avec ce numéro a déjà été généré (chaînage rompu)."""


def quantize_money(amount: Decimal) -> Decimal:
    """Arrondit un montant monétaire à 2 décimales (ROUND_HALF_UP)."""
    return amount.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée les tables sales, sale_vat_lines et z_reports si elles n'existent pas."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_heure TEXT NOT NULL,
            total_ht DECIMAL NOT NULL,
            total_tva DECIMAL NOT NULL,
            total_ttc DECIMAL NOT NULL,
            remise_ttc DECIMAL NOT NULL DEFAULT '0.00',
            mode_paiement TEXT NOT NULL,
            statut TEXT NOT NULL DEFAULT 'VALIDEE',
            z_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sale_vat_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            taux DECIMAL NOT NULL,
            base_ht DECIMAL NOT NULL,
            montant_tva DECIMAL NOT NULL,
            montant_ttc DECIMAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS z_reports (
            z_number INTEGER PRIMARY KEY,
            date_debut TEXT NOT NULL,
            date_fin TEXT NOT NULL,
            total_ht DECIMAL NOT NULL,
            total_tva DECIMAL NOT NULL,
            total_ttc DECIMAL NOT NULL,
            total_discounts DECIMAL NOT NULL,
            signature_hash TEXT NOT NULL,
            previous_z_hash TEXT
        )
        """
    )
    conn.commit()


def record_validated_sale(
    conn: sqlite3.Connection,
    date_heure: str,
    mode_paiement: str,
    vat_lines: Sequence[Tuple[Decimal, Decimal, Decimal, Decimal]],
    remise_ttc: Decimal = Decimal("0.00"),
    statut: str = "VALIDEE",
) -> int:
    """Enregistre une vente validée et sa ventilation TVA (taux, base_ht, montant_tva, montant_ttc)."""
    total_ht = sum((line[1] for line in vat_lines), Decimal("0.00"))
    total_tva = sum((line[2] for line in vat_lines), Decimal("0.00"))
    total_ttc = sum((line[3] for line in vat_lines), Decimal("0.00"))

    with db_transaction(conn=conn) as cursor:
        cursor.execute(
            "INSERT INTO sales "
            "(date_heure, total_ht, total_tva, total_ttc, remise_ttc, mode_paiement, statut) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_heure, total_ht, total_tva, total_ttc, remise_ttc, mode_paiement, statut),
        )
        sale_id = cursor.lastrowid
        for taux, base_ht, montant_tva, montant_ttc in vat_lines:
            cursor.execute(
                "INSERT INTO sale_vat_lines (sale_id, taux, base_ht, montant_tva, montant_ttc) "
                "VALUES (?, ?, ?, ?, ?)",
                (sale_id, taux, base_ht, montant_tva, montant_ttc),
            )

    return sale_id


def _compute_signature_hash(
    z_number: int,
    date_debut: str,
    date_fin: str,
    total_ht: Decimal,
    total_tva: Decimal,
    total_ttc: Decimal,
    total_discounts: Decimal,
    previous_z_hash: Optional[str],
) -> str:
    """SHA-256 des totaux + chaînage avec le hash du Z précédent (inaltérabilité fiscale)."""
    payload = "|".join(
        [
            str(z_number),
            date_debut,
            date_fin,
            str(quantize_money(total_ht)),
            str(quantize_money(total_tva)),
            str(quantize_money(total_ttc)),
            str(quantize_money(total_discounts)),
            previous_z_hash or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_z_report(
    conn: sqlite3.Connection,
    z_number: int,
    previous_z_hash: Optional[str] = None,
) -> ZReport:
    """Agrège les ventes validées non clôturées, scelle et clôture le Z de façon atomique.

    Lève DuplicateZNumberError si un rapport Z portant ce numéro existe déjà.
    """
    try:
        with db_transaction(conn=conn) as cursor:
            cursor.execute(
                "SELECT id, date_heure, total_ht, total_tva, total_ttc, remise_ttc, mode_paiement "
                "FROM sales WHERE statut = 'VALIDEE' AND z_id IS NULL ORDER BY date_heure ASC"
            )
            sales_rows = cursor.fetchall()

            now = _now_iso()
            date_debut = sales_rows[0]["date_heure"] if sales_rows else now
            date_fin = sales_rows[-1]["date_heure"] if sales_rows else now

            total_ht = Decimal("0.00")
            total_tva = Decimal("0.00")
            total_ttc = Decimal("0.00")
            total_discounts = Decimal("0.00")

            payments: dict = {}
            sale_ids: List[int] = []

            for row in sales_rows:
                sale_ids.append(row["id"])
                total_ht += Decimal(str(row["total_ht"]))
                total_tva += Decimal(str(row["total_tva"]))
                total_ttc += Decimal(str(row["total_ttc"]))
                total_discounts += Decimal(str(row["remise_ttc"]))

                mode = row["mode_paiement"]
                entry = payments.setdefault(
                    mode, {"total_ttc": Decimal("0.00"), "count": 0}
                )
                entry["total_ttc"] += Decimal(str(row["total_ttc"]))
                entry["count"] += 1

            taxes: dict = {}
            if sale_ids:
                placeholders = ",".join("?" for _ in sale_ids)
                cursor.execute(
                    "SELECT taux, base_ht, montant_tva, montant_ttc FROM sale_vat_lines "
                    f"WHERE sale_id IN ({placeholders})",
                    sale_ids,
                )
                for line in cursor.fetchall():
                    taux = Decimal(str(line["taux"]))
                    entry = taxes.setdefault(
                        taux,
                        {
                            "base_ht": Decimal("0.00"),
                            "montant_tva": Decimal("0.00"),
                            "montant_ttc": Decimal("0.00"),
                        },
                    )
                    entry["base_ht"] += Decimal(str(line["base_ht"]))
                    entry["montant_tva"] += Decimal(str(line["montant_tva"]))
                    entry["montant_ttc"] += Decimal(str(line["montant_ttc"]))

            tax_breakdowns = [
                TaxBreakdown(
                    taux=taux,
                    base_ht=data["base_ht"],
                    montant_tva=data["montant_tva"],
                    montant_ttc=data["montant_ttc"],
                )
                for taux, data in sorted(taxes.items())
            ]
            payment_breakdowns = [
                PaymentBreakdown(
                    mode_paiement=mode,
                    total_ttc=data["total_ttc"],
                    nombre_transactions=data["count"],
                )
                for mode, data in sorted(payments.items())
            ]

            signature_hash = _compute_signature_hash(
                z_number,
                date_debut,
                date_fin,
                total_ht,
                total_tva,
                total_ttc,
                total_discounts,
                previous_z_hash,
            )

            cursor.execute(
                "INSERT INTO z_reports "
                "(z_number, date_debut, date_fin, total_ht, total_tva, total_ttc, "
                "total_discounts, signature_hash, previous_z_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    z_number,
                    date_debut,
                    date_fin,
                    total_ht,
                    total_tva,
                    total_ttc,
                    total_discounts,
                    signature_hash,
                    previous_z_hash,
                ),
            )

            if sale_ids:
                placeholders = ",".join("?" for _ in sale_ids)
                cursor.execute(
                    f"UPDATE sales SET z_id = ? WHERE id IN ({placeholders})",
                    [z_number] + sale_ids,
                )
    except sqlite3.IntegrityError as exc:
        raise DuplicateZNumberError(
            f"Un rapport Z portant le numéro {z_number} existe déjà."
        ) from exc

    nombre_transactions = len(sale_ids)
    panier_moyen = (
        quantize_money(total_ttc / nombre_transactions)
        if nombre_transactions
        else Decimal("0.00")
    )

    return ZReport(
        z_number=z_number,
        date_debut=date_debut,
        date_fin=date_fin,
        total_ht=total_ht,
        total_tva=total_tva,
        total_ttc=total_ttc,
        taxes=tax_breakdowns,
        payments=payment_breakdowns,
        total_discounts=total_discounts,
        nombre_transactions=nombre_transactions,
        panier_moyen=panier_moyen,
        signature_hash=signature_hash,
        previous_z_hash=previous_z_hash,
    )
