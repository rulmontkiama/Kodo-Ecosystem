# -*- coding: utf-8 -*-
"""
Routes API Point de Vente (POS) & Mouvements de Caisse - Kōdo POS Core
"""

import json
import re
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
                # L'écran n'envoie que l'id PRODUIT et la taille : la ligne de stock est retrouvée côté
                # serveur (cart_engine._resolve_stock_id). Ne JAMAIS recopier l'id produit dans stock_id.
                "product_id": prod.get('product_id') or prod.get('id'),
                "stock_id": item.get('stock_id'),
                "code_barre": prod.get('barcode', ''),
                "nom": prod.get('name', 'Article'),
                "quantite": item.get('quantity', 1),
                "prix_vente_tvac": float(prod.get('price', 0)),
                "taux_tva": float(prod.get('vat_rate', 0.21)),
                "taille": item.get('size') or item.get('selectedSize') or ''
            })

        # Support Split Payment (Paiements multiples CB/Espèces/QR)
        split_details = data.get('splitDetails')
        payments = []
        if split_details and isinstance(split_details, dict):
            if float(split_details.get('cb', 0)) > 0:
                payments.append(('CB', float(split_details['cb'])))
            if float(split_details.get('especes', 0)) > 0:
                payments.append(('Espèces', float(split_details['especes'])))
            if float(split_details.get('qr', 0)) > 0:
                payments.append(('QR', float(split_details['qr'])))
        elif data.get('payments') and isinstance(data.get('payments'), list):
            payments = [(p[0], float(p[1])) for p in data['payments'] if float(p[1]) > 0]
        
        if not payments:
            payments = [(mode_paiement, total_ttc)]

        try:
            res = process_sale_transaction(
                cart_items=cart_items,
                total_tvac=total_ttc,
                payments=payments,
                client_id=id_client,
                cashier_name=vendeur,
                caisse_id="POS-01",
                discount_percent=remise,
                change_given=rendu,
                gift_card_code=data.get('giftCardCode')
            )
        except ValueError as ve:
            # Rejet métier légitime (stock insuffisant, paiement insuffisant, article
            # introuvable...) : sans ce try/except, l'exception remontait non gérée
            # jusqu'au serveur HTTP, qui ne renvoie alors aucune réponse JSON exploitable
            # au frontend (connexion coupée au lieu d'un message d'erreur clair).
            return 400, {"error": str(ve)}

        print_status = "SKIPPED"
        job_id = None
        if data.get('printReceipt', False):
            try:
                from kodo_core.hardware.print_worker import get_print_worker
                worker = get_print_worker()
                job = worker.enqueue_ticket_print(res["numero_ticket"])
                print_status = "ENQUEUED"
                job_id = job.job_id
            except Exception as pe:
                print(f"[IMPRESSION ENQUEUE WARNING] {pe}")
                print_status = "ERROR"

        return 200, {
            "success": True,
            "receiptNumber": res["numero_ticket"],
            "ticket": res,
            "print_status": print_status,
            "print_job_id": job_id
        }

    # 3. Recherche d'un ticket par numéro, avec quantité restant remboursable par ligne
    elif method == "GET" and path == "/api/sales/lookup":
        numero = (query.get("ticket") or query.get("numero") or query.get("receiptNumber") or [None])[0]
        if not numero:
            return 400, {"error": "Paramètre 'ticket' manquant"}
        result = database_manager.rechercher_ticket_pour_remboursement(numero)
        if not result:
            return 404, {"error": f"Ticket introuvable ou non remboursable : {numero}"}
        return 200, result

    # 3bis. Traitement d'un retour / remboursement
    elif method == "POST" and (path == "/api/sales/return" or path == "/api/sales/refund"):
        orig_ticket = data.get("ticket_number") or data.get("receiptNumber")
        vd_id = data.get("sales_detail_id") or data.get("detail_id")
        stock_id = data.get("stock_id")
        price = float(data.get("price") or data.get("amount") or 0.0)
        mode = data.get("mode") or data.get("paymentMethod") or "Espèces"
        vendeur = data.get("vendeur") or data.get("cashierName") or "Admin"
        quantity = int(data.get("quantity") or data.get("quantite") or 1)

        if not orig_ticket or not vd_id:
            return 400, {"error": "ticket_number et sales_detail_id sont requis"}

        try:
            res = process_return_transaction(
                original_ticket_number=orig_ticket,
                sales_detail_id=vd_id,
                stock_id=stock_id,
                refund_price=price,
                refund_mode=mode,
                cashier_name=vendeur,
                quantity=quantity
            )
        except ValueError as ve:
            return 400, {"error": str(ve)}
        return 200, res

    # 3ter. Réimpression d'un ticket existant (asynchrone via PrintWorker)
    elif method == "POST" and path == "/api/sales/reprint":
        numero_ticket = data.get("receiptNumber")
        if not numero_ticket:
            ticket_id = data.get("ticket_id") or data.get("ticketId")
            if not ticket_id:
                return 400, {"error": "receiptNumber ou ticket_id manquant"}
            conn = database_manager.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT numero_ticket FROM Tickets WHERE id = ?", (ticket_id,))
            row = cursor.fetchone()
            conn.close()
            if not row:
                return 404, {"error": "Ticket introuvable"}
            numero_ticket = row[0]

        try:
            from kodo_core.hardware.print_worker import get_print_worker
            worker = get_print_worker()
            job = worker.enqueue_ticket_print(numero_ticket)
            return 200, {"success": True, "receiptNumber": numero_ticket, "print_job_id": job.job_id, "status": "ENQUEUED"}
        except Exception as pe:
            print(f"[IMPRESSION REPRINT WARNING] {pe}")
            return 500, {"success": False, "error": str(pe)}

    # 3quater. État du spouleur et circuit breaker imprimante
    elif method == "GET" and path == "/api/printer/status":
        from kodo_core.hardware.print_worker import get_print_worker
        worker = get_print_worker()
        return 200, worker.get_circuit_status()

    # 3quinquies. Liste des tâches d'impression récentes
    elif method == "GET" and path == "/api/printer/jobs":
        from kodo_core.hardware.print_worker import get_print_worker
        worker = get_print_worker()
        return 200, {"jobs": worker.get_recent_jobs()}

    # 3sexies. État de sanctuarisation du stock et des données magasin
    elif method == "GET" and path == "/api/sanctuary/status":
        from kodo_core.db.sanctuary_shield import SanctuaryShield
        conn = database_manager.get_connection()
        try:
            fp = SanctuaryShield.compute_sanctuary_fingerprint(conn)
            return 200, {
                "success": True,
                "sanctuary": fp,
                "status": "PROTECTED"
            }
        finally:
            conn.close()

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

        digits = re.findall(r'\d+', str(ticket_ids[0]))
        if digits:
            db_id = int(digits[0])
            delete_parked_cart(db_id)
        return 200, {"success": True}

    # 8. Clôture Z de Caisse
    elif method == "POST" and path == "/api/cloture-z":
        # fondCaisseReel absent/null = pas de comptage physique (rattrapage d'un ancien jour).
        raw_reel = data.get('fondCaisseReel', 0)
        fond_caisse = None if raw_reel is None else float(raw_reel)
        fond_caisse_matin = float(data.get('fondCaisseMatin', 0) or 0)
        vendeur = data.get('vendeur', 'Admin')
        jusqu_au = data.get('jusquAu') or None
        if jusqu_au is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(jusqu_au)):
            return 400, {"success": False, "error": "Date de clôture invalide (attendu AAAA-MM-JJ)"}
        result = ZReportEngine.close_z_report(
            caisse_id="POS-01",
            fond_caisse_reel=fond_caisse,
            fond_caisse_matin=fond_caisse_matin,
            vendeur=vendeur,
            jusqu_au=jusqu_au
        )
        return 200, {"success": True, "cloture": result}

    # 8bis. Clôture Z séquentielle automatique des journées antérieures en retard
    elif method == "POST" and path == "/api/cloture-z/batch-pending":
        vendeur = data.get('vendeur', 'Admin')
        results = ZReportEngine.close_all_pending_days_sequentially(
            caisse_id="POS-01",
            vendeur=vendeur
        )
        return 200, {
            "success": True,
            "closed_days_count": len(results),
            "reports": results
        }

    # 9. Résumé du Z non clôturé (optionnellement limité à un jour : ?jusqu_au=AAAA-MM-JJ)
    elif method == "GET" and path == "/api/cloture-z/summary":
        jusqu_au = (query.get("jusqu_au") or [None])[0] if isinstance(query, dict) else None
        if jusqu_au is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(jusqu_au)):
            return 400, {"error": "Date invalide (attendu AAAA-MM-JJ)"}
        summary = ZReportEngine.get_daily_z_summary(caisse_id="POS-01", jusqu_au=jusqu_au)
        return 200, summary

    # 9bis. Mouvements de caisse (apports / prélèvements d'espèces)
    elif method == "GET" and path == "/api/cash-movements":
        mouvements = database_manager.lister_mouvements_caisse(caisse_id="POS-01")
        return 200, mouvements

    elif method == "POST" and path == "/api/cash-movements":
        mvt_type = str(data.get("type", "")).lower()
        type_mouvement = "APPORT" if mvt_type == "apport" else "PRELEVEMENT" if mvt_type == "prelevement" else None
        if type_mouvement is None:
            return 400, {"error": "Type de mouvement invalide (attendu: 'apport' ou 'prelevement')"}

        try:
            montant = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return 400, {"error": "Montant invalide"}

        motif = str(data.get("reason", "")).strip()
        vendeur = data.get("userName") or data.get("vendeur") or "Admin"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = database_manager.get_connection()
        try:
            cursor = conn.cursor()
            mvt_id = database_manager.enregistrer_mouvement_caisse(
                cursor=cursor,
                type_mouvement=type_mouvement,
                montant=montant,
                motif=motif,
                vendeur_nom=vendeur,
                date_heure=now_str,
                caisse_id="POS-01"
            )
            conn.commit()
        except ValueError as ve:
            conn.rollback()
            return 400, {"error": str(ve)}
        finally:
            conn.close()

        return 200, {
            "success": True,
            "id": mvt_id,
            "type": mvt_type,
            "amount": montant,
            "reason": motif,
            "userName": vendeur,
            "date_heure": now_str
        }

    # 9ter. Cartes Cadeaux / Avoirs — émission et consultation réelles côté serveur.
    # Avant cette route, un avoir "émis" depuis Retours/Avoirs ne vivait qu'en mémoire
    # navigateur (localStorage) : invisible d'une autre caisse et jamais vérifiable à la
    # dépense (cf. le contrôle de redemption dans /api/sales ci-dessus).
    elif method == "GET" and path == "/api/gift-cards":
        return 200, database_manager.lister_cartes_cadeaux()

    elif method == "POST" and path == "/api/gift-cards":
        try:
            montant = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return 400, {"error": "Montant invalide"}

        conn = database_manager.get_connection()
        try:
            cursor = conn.cursor()
            carte = database_manager.emettre_carte_cadeau(
                cursor=cursor,
                montant=montant,
                code=data.get("code"),
                client_id=data.get("clientId"),
                client_nom=data.get("clientName"),
                notes=data.get("notes"),
                emis_par=data.get("userName") or data.get("cashierName") or "Admin",
                prefix=(data.get("prefix") or "AVOIR"),
            )
            conn.commit()
        except ValueError as ve:
            conn.rollback()
            return 400, {"error": str(ve)}
        finally:
            conn.close()

        return 200, {"success": True, **carte}

    elif method == "POST" and path == "/api/gift-cards/void":
        code = data.get("code")
        conn = database_manager.get_connection()
        try:
            cursor = conn.cursor()
            database_manager.annuler_carte_cadeau(cursor, code)
            conn.commit()
        except ValueError as ve:
            conn.rollback()
            return 400, {"error": str(ve)}
        finally:
            conn.close()
        return 200, {"success": True}

    # 10. Crash Recovery : Sauvegarde / Récupération du panier actif
    elif method == "POST" and path == "/api/cart/session":
        try:
            from kodo_core.services.crash_recovery import CrashRecoveryService
            from kodo_core.domain.sales.models import Cart
            cart = Cart.from_dict(data.get("cart", {}))
            CrashRecoveryService().save_snapshot(cart)
            return 200, {"success": True}
        except Exception as e:
            return 400, {"error": str(e)}

    elif method == "GET" and path == "/api/cart/session":
        try:
            from kodo_core.services.crash_recovery import CrashRecoveryService
            recovery = CrashRecoveryService()
            if recovery.has_pending_recovery():
                cart = recovery.restore_cart_session()
                return 200, {"has_recovery": True, "cart": cart.to_dict()}
            return 200, {"has_recovery": False}
        except Exception as e:
            return 500, {"error": str(e)}

    elif method == "DELETE" and path == "/api/cart/session":
        try:
            from kodo_core.services.crash_recovery import CrashRecoveryService
            CrashRecoveryService().clear_session()
            return 200, {"success": True}
        except Exception as e:
            return 500, {"error": str(e)}

    # 11. Calcul Panier pur avec Decimal (cart_service)
    elif method == "POST" and path == "/api/cart/calculate":
        try:
            from kodo_core.services.cart_service import compute_cart_totals
            from kodo_core.domain.sales.models import Cart
            cart = Cart.from_dict(data)
            totals = compute_cart_totals(cart)
            return 200, {
                "subtotal_ttc": str(totals.subtotal_ttc),
                "total_discount": str(totals.total_discount),
                "total_ht": str(totals.total_ht),
                "total_tva": str(totals.total_tva),
                "total_ttc": str(totals.total_ttc),
                "vat_breakdown": [
                    {
                        "rate": str(line.rate),
                        "base_ht": str(line.base_ht),
                        "vat_amount": str(line.vat_amount),
                        "total_ttc": str(line.total_ttc)
                    }
                    for line in totals.vat_breakdown
                ]
            }
        except Exception as e:
            return 400, {"error": str(e)}

    return None

