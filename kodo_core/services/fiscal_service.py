# -*- coding: utf-8 -*-
"""
Service de conformité fiscale anti-fraude - Journal des ventes inaltérable (fiscal_ledger).
Scelle chaque vente par chaînage cryptographique SHA-256 (NF525/LNE) et audite l'intégrité
de la chaîne complète. Aucune dépendance UI.
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from kodo_core.domain.accounting.ledger import GENESIS_HASH, build_hash_payload, format_sequence_number

TWO_DECIMALS = Decimal("0.01")


class FiscalTamperDetectedError(Exception):
    """Levée quand le journal fiscal a été altéré (montant ou chaînage rompu)."""

    def __init__(self, sequence_number: str, reason: str):
        self.sequence_number = sequence_number
        self.reason = reason
        super().__init__(f"Falsification détectée sur le ticket {sequence_number} : {reason}")


def quantize_money(amount: Decimal) -> Decimal:
    """Arrondit un montant monétaire à 2 décimales (ROUND_HALF_UP)."""
    return Decimal(amount).quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_sha256(data_string: str) -> str:
    """Calcule le hachage SHA-256 d'une chaîne UTF-8."""
    return hashlib.sha256(data_string.encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée les tables fiscal_ledger et fiscal_unsealed_sales si elles n'existent pas."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fiscal_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence_number TEXT NOT NULL UNIQUE,
            sale_id INTEGER NOT NULL,
            timestamp_utc TEXT NOT NULL,
            total_ttc DECIMAL NOT NULL,
            total_ht DECIMAL NOT NULL,
            total_tva DECIMAL NOT NULL,
            previous_hash TEXT NOT NULL,
            current_hash TEXT NOT NULL,
            signature TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fiscal_unsealed_sales (
            sale_id INTEGER PRIMARY KEY,
            total_ttc DECIMAL NOT NULL,
            total_ht DECIMAL NOT NULL,
            total_tva DECIMAL NOT NULL,
            timestamp_utc TEXT NOT NULL,
            failure_reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def record_unsealed_sale(conn: sqlite3.Connection, sale_id: int, totals: dict, reason: str) -> None:
    """Journalise une vente dont le scellement fiscal a échoué, pour reprise ultérieure.

    Appelé quand `seal_sale` lève une exception après le commit de la vente : sans ce
    filet, la vente reste enregistrée dans Tickets mais disparaît silencieusement du
    journal fiscal inaltérable (trou de conformité NF525 indétectable en usage normal).
    """
    conn.execute(
        """
        INSERT INTO fiscal_unsealed_sales
            (sale_id, total_ttc, total_ht, total_tva, timestamp_utc, failure_reason, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sale_id) DO UPDATE SET
            failure_reason = excluded.failure_reason,
            recorded_at = excluded.recorded_at
        """,
        (
            sale_id,
            str(quantize_money(Decimal(str(totals["total_ttc"])))),
            str(quantize_money(Decimal(str(totals["total_ht"])))),
            str(quantize_money(Decimal(str(totals["total_tva"])))),
            totals.get("timestamp_utc") or _now_iso(),
            str(reason),
            _now_iso(),
        ),
    )
    conn.commit()


def reseal_pending_sales(conn: sqlite3.Connection) -> list:
    """Retente le scellement de toutes les ventes en attente (ex. au démarrage de l'app).

    Retourne la liste des sale_id rescellés avec succès. Une vente qui échoue à nouveau
    reste en attente pour la prochaine tentative.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT sale_id, total_ttc, total_ht, total_tva, timestamp_utc FROM fiscal_unsealed_sales")
    pending = cursor.fetchall()

    resealed = []
    for sale_id, total_ttc, total_ht, total_tva, timestamp_utc in pending:
        try:
            seal_sale(conn, sale_id, {
                "total_ttc": total_ttc,
                "total_ht": total_ht,
                "total_tva": total_tva,
                "timestamp_utc": timestamp_utc,
            })
        except Exception as fe:
            record_unsealed_sale(conn, sale_id, {
                "total_ttc": total_ttc, "total_ht": total_ht, "total_tva": total_tva,
                "timestamp_utc": timestamp_utc,
            }, str(fe))
            continue
        conn.execute("DELETE FROM fiscal_unsealed_sales WHERE sale_id = ?", (sale_id,))
        conn.commit()
        resealed.append(sale_id)

    return resealed


def _next_sequence_number(cursor: sqlite3.Cursor, year: int) -> str:
    """Calcule le prochain numéro de séquence strict sans trou pour l'année donnée."""
    cursor.execute(
        "SELECT sequence_number FROM fiscal_ledger WHERE sequence_number LIKE ? ORDER BY id DESC LIMIT 1",
        (f"TCK-{year}-%",),
    )
    row = cursor.fetchone()
    if row is None:
        return format_sequence_number(1, year)
    last_index = int(row[0].rsplit("-", 1)[1])
    return format_sequence_number(last_index + 1, year)


def seal_sale(db_conn: sqlite3.Connection, sale_id: int, totals: dict) -> dict:
    """
    Enregistre une vente dans le journal fiscal inaltérable, de manière atomique.
    `totals` doit fournir total_ttc, total_ht, total_tva.
    Retourne l'entrée scellée sous forme de dict.
    """
    total_ttc = quantize_money(Decimal(str(totals["total_ttc"])))
    total_ht = quantize_money(Decimal(str(totals["total_ht"])))
    total_tva = quantize_money(Decimal(str(totals["total_tva"])))
    timestamp_utc = totals.get("timestamp_utc") or _now_iso()
    year = datetime.fromisoformat(timestamp_utc).year

    cursor = db_conn.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")

        sequence_number = _next_sequence_number(cursor, year)

        cursor.execute("SELECT current_hash FROM fiscal_ledger ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        previous_hash = row[0] if row else GENESIS_HASH

        payload = build_hash_payload(sequence_number, timestamp_utc, total_ttc, total_tva, previous_hash)
        current_hash = compute_sha256(payload)
        signature = compute_sha256(f"{current_hash}|{sequence_number}")

        cursor.execute(
            """
            INSERT INTO fiscal_ledger (
                sequence_number, sale_id, timestamp_utc, total_ttc, total_ht,
                total_tva, previous_hash, current_hash, signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence_number,
                sale_id,
                timestamp_utc,
                str(total_ttc),
                str(total_ht),
                str(total_tva),
                previous_hash,
                current_hash,
                signature,
            ),
        )
        db_conn.commit()
    except Exception:
        db_conn.rollback()
        raise

    return {
        "sequence_number": sequence_number,
        "sale_id": sale_id,
        "timestamp_utc": timestamp_utc,
        "total_ttc": total_ttc,
        "total_ht": total_ht,
        "total_tva": total_tva,
        "previous_hash": previous_hash,
        "current_hash": current_hash,
        "signature": signature,
    }


def audit_ledger_integrity(db_conn: sqlite3.Connection) -> bool:
    """
    Vérifie la chaîne séquentielle complète du journal fiscal, du premier au dernier ticket.
    Lève FiscalTamperDetectedError si un montant, un hash ou la séquence a été altéré.
    Retourne True si la chaîne est intègre (y compris si le journal est vide).
    """
    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT sequence_number, timestamp_utc, total_ttc, total_ht, total_tva,
               previous_hash, current_hash, signature
        FROM fiscal_ledger
        ORDER BY id ASC
        """
    )
    rows = cursor.fetchall()

    expected_previous_hash = GENESIS_HASH
    last_index_by_year = {}
    for row in rows:
        (
            sequence_number,
            timestamp_utc,
            total_ttc,
            total_ht,
            total_tva,
            previous_hash,
            current_hash,
            signature,
        ) = row

        total_ttc = Decimal(str(total_ttc))
        total_tva = Decimal(str(total_tva))

        _, seq_year, seq_index = sequence_number.split("-")
        seq_year = int(seq_year)
        seq_index = int(seq_index)
        expected_index = last_index_by_year.get(seq_year, 0) + 1
        if seq_index != expected_index:
            raise FiscalTamperDetectedError(sequence_number, "rupture de séquence (numéro manquant ou dupliqué)")
        last_index_by_year[seq_year] = seq_index

        if previous_hash != expected_previous_hash:
            raise FiscalTamperDetectedError(sequence_number, "chaînage rompu (previous_hash incohérent)")

        recomputed_payload = build_hash_payload(sequence_number, timestamp_utc, total_ttc, total_tva, previous_hash)
        recomputed_hash = compute_sha256(recomputed_payload)
        if recomputed_hash != current_hash:
            raise FiscalTamperDetectedError(sequence_number, "montant ou horodatage falsifié (current_hash invalide)")

        recomputed_signature = compute_sha256(f"{current_hash}|{sequence_number}")
        if recomputed_signature != signature:
            raise FiscalTamperDetectedError(sequence_number, "signature invalide")

        expected_previous_hash = current_hash

    return True
