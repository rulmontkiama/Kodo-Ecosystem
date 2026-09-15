# -*- coding: utf-8 -*-
"""
Service de gestion atomique des stocks - Kōdo POS Core.
Décrémentation/incrémentation transactionnelles avec traçabilité (stock_movements)
et détection des articles en alerte de stock.
"""

import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from kodo_core.db.connection import db_transaction
from kodo_core.domain.catalog.models import StockAlert, StockMovement, StockMovementType


class InsufficientStockError(Exception):
    """Levée quand une opération ferait passer le stock en négatif de façon inattendue."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crée les tables stock_items et stock_movements si elles n'existent pas."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_items (
            product_id TEXT NOT NULL,
            variation_id TEXT NOT NULL DEFAULT '',
            quantite_actuelle INTEGER NOT NULL,
            seuil_alerte INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (product_id, variation_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id TEXT NOT NULL,
            variation_id TEXT,
            type_mouvement TEXT NOT NULL,
            delta INTEGER NOT NULL,
            stock_final INTEGER NOT NULL,
            motif TEXT,
            date_heure TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _variation_key(variation_id: Optional[str]) -> str:
    return variation_id or ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_stock_item(
    conn: sqlite3.Connection,
    product_id: str,
    quantite_initiale: int,
    seuil_alerte: int = 0,
    variation_id: Optional[str] = None,
) -> None:
    """Crée ou réinitialise la ligne de stock d'un article (hors traçabilité des mouvements)."""
    variation_key = _variation_key(variation_id)
    with db_transaction(conn=conn) as cursor:
        cursor.execute(
            "INSERT INTO stock_items (product_id, variation_id, quantite_actuelle, seuil_alerte) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(product_id, variation_id) DO UPDATE SET "
            "quantite_actuelle = excluded.quantite_actuelle, seuil_alerte = excluded.seuil_alerte",
            (product_id, variation_key, quantite_initiale, seuil_alerte),
        )


def get_current_stock(
    conn: sqlite3.Connection, product_id: str, variation_id: Optional[str] = None
) -> int:
    """Retourne la quantité actuelle en stock (0 si l'article n'a pas de ligne de stock)."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT quantite_actuelle FROM stock_items WHERE product_id = ? AND variation_id = ?",
        (product_id, _variation_key(variation_id)),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def _apply_delta(
    conn: sqlite3.Connection,
    product_id: str,
    delta: int,
    type_mouvement: StockMovementType,
    variation_id: Optional[str] = None,
    motif: Optional[str] = None,
    allow_negative: bool = False,
) -> StockMovement:
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise TypeError("delta doit être un int.")
    if delta == 0:
        raise ValueError("delta ne peut pas être nul.")

    variation_key = _variation_key(variation_id)

    with db_transaction(conn=conn) as cursor:
        cursor.execute(
            "SELECT quantite_actuelle FROM stock_items WHERE product_id = ? AND variation_id = ?",
            (product_id, variation_key),
        )
        row = cursor.fetchone()
        current = int(row[0]) if row else 0

        new_quantity = current + delta
        if new_quantity < 0 and not allow_negative:
            raise InsufficientStockError(
                f"Stock insuffisant pour {product_id} "
                f"({variation_id or 'défaut'}): disponible={current}, demandé={-delta}."
            )

        if row is None:
            cursor.execute(
                "INSERT INTO stock_items (product_id, variation_id, quantite_actuelle, seuil_alerte) "
                "VALUES (?, ?, ?, 0)",
                (product_id, variation_key, new_quantity),
            )
        else:
            cursor.execute(
                "UPDATE stock_items SET quantite_actuelle = ? "
                "WHERE product_id = ? AND variation_id = ?",
                (new_quantity, product_id, variation_key),
            )

        date_heure = _now_iso()
        cursor.execute(
            "INSERT INTO stock_movements "
            "(product_id, variation_id, type_mouvement, delta, stock_final, motif, date_heure) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                product_id,
                variation_id,
                type_mouvement.value,
                delta,
                new_quantity,
                motif,
                date_heure,
            ),
        )
        movement_id = cursor.lastrowid

    return StockMovement(
        id=movement_id,
        product_id=product_id,
        variation_id=variation_id,
        type_mouvement=type_mouvement,
        delta=delta,
        stock_final=new_quantity,
        motif=motif,
        date_heure=date_heure,
    )


def decrement_stock_on_sale(
    conn: sqlite3.Connection,
    product_id: str,
    quantity: int,
    variation_id: Optional[str] = None,
    motif: Optional[str] = None,
) -> StockMovement:
    """Décrémente le stock lors d'une vente, dans une transaction atomique.

    Lève InsufficientStockError si la quantité demandée dépasse le stock disponible.
    """
    if quantity <= 0:
        raise ValueError("quantity doit être strictement positif.")
    return _apply_delta(
        conn,
        product_id,
        -quantity,
        StockMovementType.VENTE,
        variation_id=variation_id,
        motif=motif,
    )


def increment_stock_on_return(
    conn: sqlite3.Connection,
    product_id: str,
    quantity: int,
    variation_id: Optional[str] = None,
    motif: Optional[str] = None,
) -> StockMovement:
    """Incrémente le stock lors d'un retour/avoir, dans une transaction atomique."""
    if quantity <= 0:
        raise ValueError("quantity doit être strictement positif.")
    return _apply_delta(
        conn,
        product_id,
        quantity,
        StockMovementType.RETOUR,
        variation_id=variation_id,
        motif=motif,
    )


def adjust_stock(
    conn: sqlite3.Connection,
    product_id: str,
    delta: int,
    type_mouvement: StockMovementType = StockMovementType.AJUSTEMENT,
    variation_id: Optional[str] = None,
    motif: Optional[str] = None,
) -> StockMovement:
    """Ajustement manuel ou d'inventaire (delta positif ou négatif, autorisé sous zéro)."""
    return _apply_delta(
        conn,
        product_id,
        delta,
        type_mouvement,
        variation_id=variation_id,
        motif=motif,
        allow_negative=True,
    )


def verifier_alertes_stock(conn: sqlite3.Connection) -> List[StockAlert]:
    """Retourne les articles dont le stock actuel est <= à leur seuil d'alerte."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT product_id, variation_id, quantite_actuelle, seuil_alerte "
        "FROM stock_items WHERE quantite_actuelle <= seuil_alerte "
        "ORDER BY quantite_actuelle ASC"
    )
    rows = cursor.fetchall()
    return [
        StockAlert(
            product_id=row[0],
            variation_id=row[1] or None,
            stock_actuel=int(row[2]),
            stock_alerte=int(row[3]),
        )
        for row in rows
    ]
