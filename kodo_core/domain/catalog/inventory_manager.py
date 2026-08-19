# -*- coding: utf-8 -*-
"""
Gestionnaire de Catalogue et de Stock - Kōdo POS Core
Gère les produits, catégories, marques, la mise à jour des stocks multi-tailles,
les codes-barres et les alertes de stock bas.
"""

import random
import re
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple

from kodo_core.db.connection import get_connection


class InventoryManager:
    """
    Gestionnaire du catalogue de produits et des mouvements de stock.
    """

    @staticmethod
    def clean_barcode(barcode: Optional[str]) -> Optional[str]:
        """Formatte et nettoie un code-barres (retourne None si vide)."""
        if not barcode:
            return None
        cleaned = barcode.strip()
        return cleaned if cleaned else None

    @staticmethod
    def generate_internal_barcode() -> str:
        """Génère un code-barres interne valide à 13 chiffres (EAN-13)."""
        base = "200" + "".join([str(random.randint(0, 9)) for _ in range(9)])
        odd_sum = sum(int(base[i]) for i in range(0, 12, 2))
        even_sum = sum(int(base[i]) for i in range(1, 12, 2))
        total = odd_sum + (even_sum * 3)
        checksum = (10 - (total % 10)) % 10
        return base + str(checksum)

    @classmethod
    def get_all_products(
        cls,
        category: Optional[str] = None,
        brand: Optional[str] = None,
        search: Optional[str] = None,
        conn=None
    ) -> List[Dict[str, Any]]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()

            query = """
                SELECT p.id, p.code_barre, p.nom, p.categorie, p.prix_achat_htva, 
                       p.prix_vente_tvac, p.taux_tva, p.image_path, p.en_solde, 
                       p.prix_solde_tvac, p.type_vente, p.unite_mesure, p.marque, p.attributs_json,
                       COALESCE(SUM(s.quantite_actuelle), 0) as stock_total
                FROM Produits p
                LEFT JOIN Stocks s ON p.id = s.id_produit
                WHERE 1=1
            """
            params: List[Any] = []

            if category:
                query += " AND LOWER(p.categorie) = LOWER(?)"
                params.append(category)

            if brand:
                query += " AND LOWER(p.marque) = LOWER(?)"
                params.append(brand)

            if search:
                query += " AND (LOWER(p.nom) LIKE LOWER(?) OR p.code_barre LIKE ?)"
                search_param = f"%{search}%"
                params.extend([search_param, search_param])

            query += " GROUP BY p.id ORDER BY p.nom ASC"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            products = []
            for r in rows:
                prod_id = r[0]

                cursor.execute("""
                    SELECT id, taille, quantite_actuelle, seuil_alerte 
                    FROM Stocks 
                    WHERE id_produit = ?
                """, (prod_id,))
                stock_rows = cursor.fetchall()
                stocks_detail = [
                    {
                        "stock_id": s[0],
                        "size": s[1] or "Taille Unique",
                        "quantity": int(s[2]),
                        "alert_threshold": int(s[3])
                    }
                    for s in stock_rows
                ]

                sizes_str = "|".join([f"{s['size']}:{s['quantity']}" for s in stocks_detail])

                px_tvac = float(r[5]) if r[5] is not None else 0.0
                px_solde = float(r[9]) if r[9] is not None else None

                products.append({
                    "id": str(r[0]),
                    "product_id": r[0],
                    "barcode": r[1] or "",
                    "name": r[2],
                    "category": r[3] or "Général",
                    "purchase_price_htva": float(r[4]) if r[4] is not None else 0.0,
                    "price": px_tvac,
                    "price_tvac": px_tvac,
                    "vat_rate": float(r[6]) if r[6] is not None else 0.21,
                    "image_path": r[7] or "",
                    "en_solde": bool(r[8]),
                    "prix_solde_tvac": px_solde,
                    "type": "service" if r[10] == "service" else "product",
                    "type_vente": r[10] or "unite",
                    "unite_mesure": r[11] or "pce",
                    "brand": r[12] or "",
                    "attributes_json": r[13] or "",
                    "sizes": sizes_str,
                    "stock": int(r[14]),
                    "stocks": stocks_detail
                })

            return products
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_product_by_id(cls, product_id: int, conn=None) -> Optional[Dict[str, Any]]:
        prods = cls.get_all_products(conn=conn)
        for p in prods:
            if int(p["product_id"]) == int(product_id):
                return p
        return None

    @classmethod
    def get_product_by_barcode(cls, barcode: str, conn=None) -> Optional[Dict[str, Any]]:
        cleaned = cls.clean_barcode(barcode)
        if not cleaned:
            return None
        prods = cls.get_all_products(search=cleaned, conn=conn)
        for p in prods:
            if p["barcode"] == cleaned:
                return p
        return None

    @classmethod
    def save_product(cls, data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()

            prod_id = data.get("id") or data.get("product_id")
            name = data.get("name") or data.get("nom")
            category = data.get("category") or data.get("categorie") or "Général"
            brand = data.get("brand") or data.get("marque") or ""
            barcode = cls.clean_barcode(data.get("barcode") or data.get("code_barre"))
            price = Decimal(str(data.get("price") or data.get("prix_vente_tvac") or 0.0))
            purchase_price = Decimal(str(data.get("purchase_price_htva") or data.get("prix_achat_htva") or 0.0))
            vat_rate = Decimal(str(data.get("vat_rate") or data.get("taux_tva") or 0.21))
            sizes_str = data.get("sizes") or ""
            stock_default = int(data.get("stock") or 0)
            is_sale = 1 if data.get("en_solde") else 0
            prix_solde = Decimal(str(data.get("prix_solde_tvac"))) if data.get("prix_solde_tvac") is not None else None

            if not name:
                raise ValueError("Le nom du produit est obligatoire")

            if category:
                cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (category,))
            if brand:
                cursor.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (brand,))

            existing_id = None
            if prod_id is not None:
                try:
                    pid_int = int(prod_id)
                    cursor.execute("SELECT id FROM Produits WHERE id = ?", (pid_int,))
                    row = cursor.fetchone()
                    if row:
                        existing_id = row[0]
                except (ValueError, TypeError):
                    existing_id = None

            if not existing_id and barcode:
                cursor.execute("SELECT id FROM Produits WHERE code_barre = ?", (barcode,))
                row = cursor.fetchone()
                if row:
                    existing_id = row[0]

            if existing_id:
                cursor.execute("""
                    UPDATE Produits
                    SET code_barre=?, nom=?, categorie=?, prix_achat_htva=?, 
                        prix_vente_tvac=?, taux_tva=?, en_solde=?, prix_solde_tvac=?, marque=?
                    WHERE id=?
                """, (barcode, name, category, float(purchase_price), float(price), float(vat_rate), is_sale, float(prix_solde) if prix_solde else None, brand, existing_id))
                prod_id = existing_id
            else:
                cursor.execute("""
                    INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, taux_tva, en_solde, prix_solde_tvac, marque)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (barcode, name, category, float(purchase_price), float(price), float(vat_rate), is_sale, float(prix_solde) if prix_solde else None, brand))
                prod_id = cursor.lastrowid

            if sizes_str:
                active_sizes = []
                parts = sizes_str.split('|')
                for part in parts:
                    if ':' in part:
                        sz, qty_str = part.split(':', 1)
                        sz = sz.strip()
                        try:
                            qty = int(qty_str.strip())
                        except ValueError:
                            qty = 0
                        active_sizes.append((sz, qty))

                active_set = set(sz for sz, _ in active_sizes)
                for sz, qty in active_sizes:
                    cursor.execute("SELECT id FROM Stocks WHERE id_produit=? AND taille=?", (prod_id, sz))
                    s_row = cursor.fetchone()
                    if s_row:
                        cursor.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (qty, s_row[0]))
                    else:
                        cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, 2)", (prod_id, sz, qty))

                cursor.execute("SELECT id, taille FROM Stocks WHERE id_produit=?", (prod_id,))
                for sid, t in cursor.fetchall():
                    if t not in active_set:
                        cursor.execute("DELETE FROM Stocks WHERE id=?", (sid,))
            else:
                cursor.execute("SELECT id FROM Stocks WHERE id_produit=? AND (taille='Taille Unique' OR taille IS NULL OR taille='')", (prod_id,))
                s_row = cursor.fetchone()
                if s_row:
                    cursor.execute("UPDATE Stocks SET quantite_actuelle=? WHERE id=?", (stock_default, s_row[0]))
                else:
                    cursor.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, 'Taille Unique', ?, 2)", (prod_id, stock_default))

            conn.commit()
            return {"success": True, "product_id": str(prod_id)}

        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def delete_product(cls, product_id: int, conn=None) -> bool:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM Produits WHERE id=?", (product_id,))
            cursor.execute("DELETE FROM Stocks WHERE id_produit=?", (product_id,))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    # Catégories et Marques

    @classmethod
    def get_categories(cls, conn=None) -> List[str]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT nom FROM Categories ORDER BY nom ASC")
            return [r[0] for r in cursor.fetchall()]
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def add_category(cls, name: str, conn=None) -> bool:
        if not name or not name.strip():
            return False
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (name.strip(),))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def delete_category(cls, name: str, conn=None) -> bool:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM Categories WHERE nom=?", (name,))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_brands(cls, conn=None) -> List[str]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT nom FROM Marques ORDER BY nom ASC")
            return [r[0] for r in cursor.fetchall()]
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def add_brand(cls, name: str, conn=None) -> bool:
        if not name or not name.strip():
            return False
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (name.strip(),))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_low_stock_alerts(cls, conn=None) -> List[Dict[str, Any]]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.id, p.nom, p.code_barre, s.taille, s.quantite_actuelle, s.seuil_alerte
                FROM Stocks s
                JOIN Produits p ON s.id_produit = p.id
                WHERE s.quantite_actuelle <= s.seuil_alerte
                ORDER BY s.quantite_actuelle ASC
            """)
            rows = cursor.fetchall()
            alerts = []
            for r in rows:
                alerts.append({
                    "product_id": r[0],
                    "product_name": r[1],
                    "barcode": r[2] or "",
                    "size": r[3] or "Unique",
                    "current_stock": int(r[4]),
                    "alert_threshold": int(r[5]),
                    "status": "RUPTURE" if r[4] <= 0 else "BAS"
                })
            return alerts
        finally:
            if should_close and conn:
                conn.close()
