# -*- coding: utf-8 -*-
"""
Gestionnaire CRM Client et Programme de Fidélité - Kōdo POS Core
Gère les fiches clients, l'historique d'achats et les points de fidélité.
"""

from decimal import Decimal
from typing import List, Dict, Any, Optional

from kodo_core.db.connection import get_connection


class CRMManager:
    """
    Gestionnaire de la relation client (CRM) et de la fidélité.
    """

    @classmethod
    def get_all_customers(cls, search: Optional[str] = None, conn=None) -> List[Dict[str, Any]]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(Clients)")
            cols = [row[1] for row in cursor.fetchall()]
            has_phone = 'telephone' in cols

            query = "SELECT id, nom, email, "
            query += "telephone, " if has_phone else "'' as telephone, "
            query += "total_depense, points_fidelite, taille_haut, taille_bas, pointure, pref_couleurs, date_anniversaire FROM Clients"

            params: List[Any] = []
            if search:
                query += " WHERE LOWER(nom) LIKE LOWER(?) OR LOWER(email) LIKE LOWER(?)"
                if has_phone:
                    query += " OR telephone LIKE ?"
                    params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
                else:
                    params.extend([f"%{search}%", f"%{search}%"])

            query += " ORDER BY nom ASC"
            cursor.execute(query, params)
            rows = cursor.fetchall()

            customers = []
            for r in rows:
                customers.append({
                    "id": str(r[0]),
                    "client_id": r[0],
                    "name": r[1],
                    "nom": r[1],
                    "email": r[2] or "",
                    "phone": r[3] or "",
                    "telephone": r[3] or "",
                    "total_spent": float(r[4]) if r[4] is not None else 0.0,
                    "points": int(r[5]) if r[5] is not None else 0,
                    "taille_haut": r[6] or "",
                    "taille_bas": r[7] or "",
                    "pointure": r[8] or "",
                    "pref_couleurs": r[9] or "",
                    "date_anniversaire": r[10] or ""
                })

            return customers
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_customer_by_id(cls, client_id: int, conn=None) -> Optional[Dict[str, Any]]:
        customers = cls.get_all_customers(conn=conn)
        for c in customers:
            if int(c["client_id"]) == int(client_id):
                return c
        return None

    @classmethod
    def save_customer(cls, data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(Clients)")
            cols = [row[1] for row in cursor.fetchall()]
            has_phone = 'telephone' in cols

            client_id = data.get("id") or data.get("client_id")
            name = data.get("name") or data.get("nom")
            email = data.get("email") or ""
            phone = data.get("phone") or data.get("telephone") or ""
            points = int(data.get("points") or data.get("points_fidelite") or 0)
            taille_haut = data.get("taille_haut") or ""
            taille_bas = data.get("taille_bas") or ""
            pointure = data.get("pointure") or ""
            pref_couleurs = data.get("pref_couleurs") or ""
            date_anniv = data.get("date_anniversaire") or ""

            if not name:
                raise ValueError("Le nom du client est obligatoire")

            is_existing = False
            if client_id:
                try:
                    cid_int = int(client_id)
                    cursor.execute("SELECT id FROM Clients WHERE id=?", (cid_int,))
                    if cursor.fetchone():
                        is_existing = True
                except ValueError:
                    pass

            if is_existing:
                if has_phone:
                    cursor.execute("""
                        UPDATE Clients 
                        SET nom=?, email=?, telephone=?, points_fidelite=?, 
                            taille_haut=?, taille_bas=?, pointure=?, pref_couleurs=?, date_anniversaire=?
                        WHERE id=?
                    """, (name, email, phone, points, taille_haut, taille_bas, pointure, pref_couleurs, date_anniv, client_id))
                else:
                    cursor.execute("""
                        UPDATE Clients 
                        SET nom=?, email=?, points_fidelite=?, 
                            taille_haut=?, taille_bas=?, pointure=?, pref_couleurs=?, date_anniversaire=?
                        WHERE id=?
                    """, (name, email, points, taille_haut, taille_bas, pointure, pref_couleurs, date_anniv, client_id))
                res_id = client_id
            else:
                if has_phone:
                    cursor.execute("""
                        INSERT INTO Clients (nom, email, telephone, points_fidelite, taille_haut, taille_bas, pointure, pref_couleurs, date_anniversaire)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (name, email, phone, points, taille_haut, taille_bas, pointure, pref_couleurs, date_anniv))
                else:
                    cursor.execute("""
                        INSERT INTO Clients (nom, email, points_fidelite, taille_haut, taille_bas, pointure, pref_couleurs, date_anniversaire)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (name, email, points, taille_haut, taille_bas, pointure, pref_couleurs, date_anniv))
                res_id = cursor.lastrowid

            conn.commit()
            return {"success": True, "client_id": str(res_id)}

        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def delete_customer(cls, client_id: int, conn=None) -> bool:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM Clients WHERE id=?", (client_id,))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_customer_purchase_history(cls, client_id: int, limit: int = 50, conn=None) -> List[Dict[str, Any]]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, numero_ticket, date_heure, total_tvac, total_htva, total_tva, 
                       remise, methode_paiement, vendeur_nom 
                FROM Tickets 
                WHERE id_client = ? 
                ORDER BY id DESC 
                LIMIT ?
            """, (client_id, limit))
            tickets = cursor.fetchall()

            history = []
            for t in tickets:
                ticket_id = t[0]
                cursor.execute("""
                    SELECT COALESCE(p.nom, 'Article'), v.quantite, v.prix_unitaire_tvac, s.taille
                    FROM Ventes_Details v
                    LEFT JOIN Stocks s ON v.id_stock = s.id
                    LEFT JOIN Produits p ON s.id_produit = p.id
                    WHERE v.id_ticket = ?
                """, (ticket_id,))
                items = [
                    {
                        "name": item[0],
                        "quantity": item[1],
                        "price_tvac": float(item[2]),
                        "size": item[3] or ""
                    }
                    for item in cursor.fetchall()
                ]

                history.append({
                    "ticket_id": t[0],
                    "receipt_number": t[1],
                    "date_heure": t[2],
                    "total_tvac": float(t[3]),
                    "total_htva": float(t[4]) if t[4] else 0.0,
                    "total_tva": float(t[5]) if t[5] else 0.0,
                    "discount": float(t[6]) if t[6] else 0.0,
                    "payment_method": t[7],
                    "cashier_name": t[8] or "Admin",
                    "items": items
                })

            return history
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def adjust_loyalty_points(cls, client_id: int, points_delta: int, conn=None) -> int:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE Clients SET points_fidelite = MAX(0, points_fidelite + ?) WHERE id=?", (points_delta, client_id))
            cursor.execute("SELECT points_fidelite FROM Clients WHERE id=?", (client_id,))
            row = cursor.fetchone()
            new_points = row[0] if row else 0
            conn.commit()
            return new_points
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def redeem_points_for_discount(cls, client_id: int, points_to_redeem: int, rate_per_point: float = 0.10, conn=None) -> Dict[str, Any]:
        client = cls.get_customer_by_id(client_id, conn=conn)
        if not client:
            return {"success": False, "error": "Client introuvable"}

        available_points = client["points"]
        if points_to_redeem > available_points:
            return {"success": False, "error": f"Points insuffisants ({available_points} disponibles)"}

        discount_value = round(points_to_redeem * rate_per_point, 2)
        new_points = cls.adjust_loyalty_points(client_id, -points_to_redeem, conn=conn)

        return {
            "success": True,
            "redeemed_points": points_to_redeem,
            "discount_value": discount_value,
            "remaining_points": new_points
        }
