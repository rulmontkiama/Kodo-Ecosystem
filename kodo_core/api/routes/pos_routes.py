# -*- coding: utf-8 -*-
"""
Routes API Point de Vente (POS) & Mouvements de Caisse - Kōdo POS Core
"""

import json
import datetime
from decimal import Decimal
from typing import Dict, Any, Tuple, Optional

import database_manager
from kodo_core.domain.sales.cart_engine import (
    process_sale_transaction,
    process_return_transaction,
    park_cart,
    get_parked_carts,
    restore_parked_cart,
    delete_parked_cart
)
from kodo_core.domain.accounting.z_report import ZReportEngine
import ticket_printer


def handle_pos_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any]) -> Optional[Tuple[int, Any]]:
    """
    Gestionnaire de requêtes pour le module POS et Ventes.
    Retourne (status_code, response_data) ou None si la route ne correspond pas.
    """

    # 1. Historique des ventes
    if method == "GET" and path == "/api/sales/history":
        conn = database_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, numero_ticket, total_tvac, total_htva, total_tva, methode_paiement, 
                   id_client, vendeur_nom, date_heure
            FROM Tickets ORDER BY id DESC LIMIT 50
        """)
        rows = cursor.fetchall()
        history = []
        for r in rows:
            ticket_id = r[0]
            cursor.execute("""
                SELECT COALESCE(p.nom, 'Article'), v.quantite, v.prix_unitaire_tvac, v.id, v.id_stock
                FROM Ventes_Details v 
                LEFT JOIN Stocks s ON v.id_stock = s.id
                LEFT JOIN Produits p ON s.id_produit = p.id 
                WHERE v.id_ticket=?
            """, (ticket_id,))
            items = [
                {
                    "name": item[0],
                    "qty": item[1],
                    "price": float(item[2]),
                    "id": item[3],
                    "stock_id": item[4]
                }
                for item in cursor.fetchall()
            ]

            history.append({
                "id": str(r[0]),
                "receiptNumber": r[1],
                "totalTTC": float(r[2]),
                "totalHT": float(r[3]) if r[3] else 0.0,
                "totalTVA": float(r[4]) if r[4] else 0.0,
                "paymentMethod": r[5],
                "clientName": str(r[6]) if r[6] else "",
                "cashierName": r[7] or "Admin",
                "date": r[8],
                "items": items
            })
        conn.close()
        return 200, history

    # 2. Enregistrement d'une vente (Encaissement)
    elif method == "POST" and path == "/api/sales":
        total_ttc = float(data.get('totalTTC', 0))
        remise = float(data.get('discountPercent', 0))
        mode_paiement = data.get('paymentMethod', 'CB')
        id_client = data.get('clientId')
        rendu = float(data.get('changeGiven', 0))
        vendeur = data.get('cashierName', 'Admin')

        items = data.get('items', [])
        cart_items = []
        for item in items:
            prod = item.get('product', {})
            cart_items.append({
                "id": prod.get('id'),
                "stock_id": item.get('stock_id') or prod.get('id'),
                "code_barre": prod.get('barcode', ''),
                "nom": prod.get('name', 'Article'),
                "quantite": item.get('quantity', 1),
                "prix_vente_tvac": float(prod.get('price', 0)),
                "taux_tva": float(prod.get('vat_rate', 0.21)),
                "taille": item.get('size', '')
            })

        res = process_sale_transaction(
            cart_items=cart_items,
            total_tvac=total_ttc,
            payments=[(mode_paiement, total_ttc)],
            client_id=id_client,
            cashier_name=vendeur,
            caisse_id="POS-01",
            discount_percent=remise,
            change_given=rendu
        )

        if data.get('printReceipt', False):
            try:
                ticket_printer.imprimer_ticket_caisse(res["numero_ticket"])
            except Exception as pe:
                print(f"[IMPRESSION WARNING] {pe}")

        return 200, {"success": True, "receiptNumber": res["numero_ticket"], "ticket": res}

    # 3. Traitement d'un retour / remboursement
    elif method == "POST" and (path == "/api/sales/return" or path == "/api/sales/refund"):
        orig_ticket = data.get("ticket_number") or data.get("receiptNumber")
        vd_id = data.get("sales_detail_id") or data.get("detail_id") or 1
        stock_id = data.get("stock_id")
        price = float(data.get("price") or data.get("amount") or 0.0)
        mode = data.get("mode") or data.get("paymentMethod") or "Espèces"
        vendeur = data.get("vendeur") or data.get("cashierName") or "Admin"

        res = process_return_transaction(
            original_ticket_number=orig_ticket,
            sales_detail_id=vd_id,
            stock_id=stock_id,
            refund_price=price,
            refund_mode=mode,
            cashier_name=vendeur
        )
        return 200, res

    # 4. Liste des paniers en attente
    elif method == "GET" and path == "/api/held-tickets":
        paniers = get_parked_carts()
        return 200, paniers

    # 5. Mettre un panier en attente
    elif method == "POST" and path == "/api/held-tickets":
        items = data.get('items', [])
        total_ttc = float(data.get('totalTTC', 0))

        client_obj = data.get('client')
        client_nom = ''
        client_id = None
        if client_obj:
            client_nom = client_obj.get('name', '')
            client_id = client_obj.get('id')
        else:
            client_nom = data.get('clientName', '')

        remise = float(data.get('discountPercent', 0))
        note = data.get('note', '')

        panier_adapted = []
        for item in items:
            prod = item.get('product', {})
            qty = item.get('quantity', 1)
            for _ in range(qty):
                panier_adapted.append({
                    "nom": prod.get('name', 'Article'),
                    "taille": item.get('size', ''),
                    "prix_vente_tvac": float(prod.get('price', 0)),
                    "code_barre": prod.get('barcode', ''),
                    "en_solde": prod.get('en_solde', 0),
                    "prix_original_tvac": float(prod.get('price', 0))
                })

        ticket_id = park_cart(
            panier=panier_adapted,
            total_tvac=total_ttc,
            client_id=client_id,
            client_name=client_nom,
            discount=remise,
            note=note
        )
        return 200, {"success": True, "ticketId": ticket_id}

    # 6. Restaurer / Récupérer un panier en attente
    elif method == "POST" and path == "/api/held-tickets/restore":
        ticket_id = data.get("id") or data.get("ticketId")
        if not ticket_id:
            return 400, {"error": "ID ticket manquant"}
        res = restore_parked_cart(int(ticket_id))
        if res:
            return 200, {"success": True, "held_ticket": res}
        return 404, {"error": "Panier en attente non trouvé"}

    # 7. Supprimer un panier en attente
    elif method == "DELETE" and path == "/api/held-tickets":
        ticket_ids = query.get('id', [])
        if not ticket_ids and 'id' in data:
            ticket_ids = [str(data['id'])]
        if not ticket_ids:
            return 400, {"error": "ID ticket manquant"}

        import re
        digits = re.findall(r'\d+', str(ticket_ids[0]))
        if digits:
            db_id = int(digits[0])
            delete_parked_cart(db_id)
        return 200, {"success": True}

    # 8. Clôture Z de Caisse
    elif method == "POST" and path == "/api/cloture-z":
        fond_caisse = float(data.get('fondCaisseReel', 0))
        vendeur = data.get('vendeur', 'Admin')
        result = ZReportEngine.close_z_report(
            caisse_id="POS-01",
            fond_caisse_reel=fond_caisse,
            vendeur=vendeur
        )
        return 200, {"success": True, "cloture": result}

    # 9. Résumé du Z du jour non clôturé
    elif method == "GET" and path == "/api/cloture-z/summary":
        summary = ZReportEngine.get_daily_z_summary(caisse_id="POS-01")
        return 200, summary

    return None
