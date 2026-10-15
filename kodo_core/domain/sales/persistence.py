# -*- coding: utf-8 -*-
"""
Port de persistance SQLite du domaine des ventes - Kōdo POS Core.
Fait le pont entre les Value Objects du domaine pur (FinancialReceipt) et les tables SQLite.
RÈGLE D'OR : N'ouvre, ne commite et ne ferme AUCUNE transaction : l'appelant détient
strictement le BEGIN IMMEDIATE et le commit unique (garantie anti-trou NF525).
"""

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import database_manager
from kodo_core.domain.sales.models import FinancialReceipt
from kodo_core.services.fiscal_service import ensure_schema, seal_sale


@dataclass
class SalePersistenceContext:
    """Contexte d'infrastructure pour l'enregistrement d'une vente en base."""

    numero_ticket: str
    now_local: str
    now_utc_iso: str
    lines: List[Dict[str, Any]]
    main_method: str
    cashier: str = "Admin"
    client_id: Optional[int] = None
    caisse_id: str = "POS-01"


def persist_receipt(cursor: sqlite3.Cursor, receipt: FinancialReceipt, ctx: SalePersistenceContext) -> int:
    """Persiste le reçu financier et son scellement fiscal dans la transaction active de l'appelant.

    N'ouvre, ne commite et ne ferme aucune transaction : l'appelant détient BEGIN IMMEDIATE.
    L'écriture du ticket et le scellement SHA-256 sont indivisibles. Tout échec annule tout.
    """
    conn = cursor.connection
    if not conn.in_transaction:
        raise RuntimeError("persist_receipt exige une transaction BEGIN IMMEDIATE ouverte par l'appelant.")

    ticket_id = database_manager.enregistrer_vente(
        cursor=cursor,
        numero_ticket=ctx.numero_ticket,
        date_heure=ctx.now_local,
        total_tvac=float(receipt.total_tvac),
        total_htva=float(receipt.total_htva),
        total_tva=float(receipt.total_tva),
        remise=float(receipt.remise),
        rendu_monnaie=float(receipt.rendu),
        ecart_arrondi_cash=float(receipt.ecart_arrondi_cash),
        paiements=[(m, float(a)) for m, a in receipt.payments],
        panier=ctx.lines,
        methode_paiement=ctx.main_method,
        id_client=ctx.client_id,
        vendeur_nom=ctx.cashier,
        caisse_id=ctx.caisse_id,
    )

    ensure_schema(conn)
    seal_sale(
        conn,
        ticket_id,
        {
            "total_ttc": receipt.total_tvac,
            "total_ht": receipt.total_htva,
            "total_tva": receipt.total_tva,
            "timestamp_utc": ctx.now_utc_iso,
        },
    )

    return ticket_id
