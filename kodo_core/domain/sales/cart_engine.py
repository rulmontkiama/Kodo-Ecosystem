# -*- coding: utf-8 -*-
"""
Moteur de Panier et Gestion des Ventes - Kōdo POS Core
Gère les calculs financiers (Decimal, arrondis bancaires), les remises (%, fixes),
les multi-règlements (Espèces, CB, Chèque, Avoir, Carte Cadeau), les tickets en attente,
les annulations et les retours/remboursements avec certification NF525.
"""

import json
import datetime
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Optional, Tuple

from kodo_core.db.connection import get_connection

TWO_DECIMALS = Decimal('0.01')
FOUR_DECIMALS = Decimal('0.0001')


def quantize_money(amount: Decimal) -> Decimal:
    """Arrondit un montant monétaire à 2 décimales selon la règle ROUND_HALF_UP."""
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return amount.quantize(TWO_DECIMALS, rounding=ROUND_HALF_UP)


class CartItem:
    """Représente une ligne d'article dans le panier d'achat."""

    def __init__(
        self,
        product_id: Optional[int] = None,
        stock_id: Optional[int] = None,
        name: str = "Article",
        barcode: str = "",
        unit_price_tvac: float = 0.0,
        quantity: int = 1,
        vat_rate: float = 0.21,
        discount_percent: float = 0.0,
        discount_amount: float = 0.0,
        size: str = "",
        brand: str = "",
        category: str = "",
        is_sale: bool = False,
        original_price_tvac: Optional[float] = None
    ):
        self.product_id = product_id
        self.stock_id = stock_id
        self.name = name
        self.barcode = barcode
        self.unit_price_tvac = Decimal(str(unit_price_tvac))
        self.quantity = int(quantity)
        self.vat_rate = Decimal(str(vat_rate))
        self.discount_percent = Decimal(str(discount_percent))
        self.discount_amount = Decimal(str(discount_amount))
        self.size = size
        self.brand = brand
        self.category = category
        self.is_sale = is_sale
        self.original_price_tvac = Decimal(str(original_price_tvac)) if original_price_tvac is not None else self.unit_price_tvac

    def get_effective_unit_price(self) -> Decimal:
        """Prix unitaire net TVAC après remises spécifiques ligne."""
        price = self.unit_price_tvac
        if self.discount_percent > Decimal('0'):
            price = price * (Decimal('1.00') - (self.discount_percent / Decimal('100.00')))
        if self.discount_amount > Decimal('0'):
            price = max(Decimal('0.00'), price - self.discount_amount)
        return quantize_money(price)

    def get_line_total_tvac(self) -> Decimal:
        """Total ligne TVAC net."""
        return quantize_money(self.get_effective_unit_price() * Decimal(str(self.quantity)))

    def get_line_total_htva(self) -> Decimal:
        """Total ligne HTVA net."""
        total_tvac = self.get_line_total_tvac()
        return quantize_money(total_tvac / (Decimal('1.00') + self.vat_rate))

    def get_line_total_tva(self) -> Decimal:
        """Total TVA de la ligne."""
        return self.get_line_total_tvac() - self.get_line_total_htva()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product_id": self.product_id,
            "stock_id": self.stock_id,
            "name": self.name,
            "code_barre": self.barcode,
            "prix_vente_tvac": float(self.unit_price_tvac),
            "quantite": self.quantity,
            "taux_tva": float(self.vat_rate),
            "discount_percent": float(self.discount_percent),
            "discount_amount": float(self.discount_amount),
            "taille": self.size,
            "brand": self.brand,
            "category": self.category,
            "is_sale": self.is_sale,
            "original_price_tvac": float(self.original_price_tvac),
            "effective_unit_price": float(self.get_effective_unit_price()),
            "line_total_tvac": float(self.get_line_total_tvac()),
            "line_total_htva": float(self.get_line_total_htva()),
            "line_total_tva": float(self.get_line_total_tva())
        }


class CartEngine:
    """Moteur de calcul et de traitement des paniers de vente."""

    def __init__(self):
        self.items: List[CartItem] = []
        self.global_discount_percent = Decimal('0.00')
        self.global_discount_amount = Decimal('0.00')
        self.client_id: Optional[int] = None
        self.client_name: str = ""
        self.note: str = ""

    def add_item(self, item: CartItem) -> None:
        for existing in self.items:
            if (existing.stock_id and existing.stock_id == item.stock_id) or \
               (existing.product_id and existing.product_id == item.product_id and existing.size == item.size and existing.unit_price_tvac == item.unit_price_tvac):
                existing.quantity += item.quantity
                return
        self.items.append(item)

    def remove_item(self, index: int) -> bool:
        if 0 <= index < len(self.items):
            self.items.pop(index)
            return True
        return False

    def clear(self) -> None:
        self.items = []
        self.global_discount_percent = Decimal('0.00')
        self.global_discount_amount = Decimal('0.00')
        self.client_id = None
        self.client_name = ""
        self.note = ""

    def set_global_discount(self, percent: float = 0.0, amount: float = 0.0) -> None:
        self.global_discount_percent = Decimal(str(percent))
        self.global_discount_amount = Decimal(str(amount))

    def calculate_subtotal_tvac(self) -> Decimal:
        subtotal = sum((item.get_line_total_tvac() for item in self.items), Decimal('0.00'))
        return quantize_money(subtotal)

    def calculate_total_discount(self) -> Decimal:
        subtotal = self.calculate_subtotal_tvac()
        disc = Decimal('0.00')
        if self.global_discount_percent > Decimal('0'):
            disc += subtotal * (self.global_discount_percent / Decimal('100.00'))
        if self.global_discount_amount > Decimal('0'):
            disc += self.global_discount_amount
        return min(subtotal, quantize_money(disc))

    def calculate_totals(self) -> Dict[str, Any]:
        subtotal_tvac = self.calculate_subtotal_tvac()
        total_discount = self.calculate_total_discount()
        final_tvac = max(Decimal('0.00'), subtotal_tvac - total_discount)

        ratio = (final_tvac / subtotal_tvac) if subtotal_tvac > Decimal('0.00') else Decimal('1.00')

        vat_breakdown: Dict[str, Dict[str, Decimal]] = {}
        total_htva = Decimal('0.00')

        for item in self.items:
            rate_str = f"{float(item.vat_rate) * 100:.1f}%".rstrip('0').rstrip('.') + "%"
            item_tvac = quantize_money(item.get_line_total_tvac() * ratio)
            item_htva = quantize_money(item_tvac / (Decimal('1.00') + item.vat_rate))
            item_tva = item_tvac - item_htva

            total_htva += item_htva

            if rate_str not in vat_breakdown:
                vat_breakdown[rate_str] = {
                    "htva": Decimal('0.00'),
                    "tva": Decimal('0.00'),
                    "tvac": Decimal('0.00'),
                    "rate": item.vat_rate
                }
            vat_breakdown[rate_str]["htva"] += item_htva
            vat_breakdown[rate_str]["tva"] += item_tva
            vat_breakdown[rate_str]["tvac"] += item_tvac

        total_tva = final_tvac - total_htva

        vat_breakdown_serializable = {
            rate: {
                "htva": float(vals["htva"]),
                "tva": float(vals["tva"]),
                "tvac": float(vals["tvac"]),
                "rate": float(vals["rate"])
            }
            for rate, vals in vat_breakdown.items()
        }

        return {
            "subtotal_tvac": float(subtotal_tvac),
            "discount_amount": float(total_discount),
            "discount_percent": float(self.global_discount_percent),
            "total_tvac": float(final_tvac),
            "total_htva": float(total_htva),
            "total_tva": float(total_tva),
            "vat_breakdown": vat_breakdown_serializable,
            "items_count": sum(item.quantity for item in self.items)
        }

    @staticmethod
    def calculate_change_due(total_due: Decimal, payments: List[Tuple[str, Decimal]]) -> Tuple[Decimal, Decimal]:
        total_paid = sum((p[1] for p in payments), Decimal('0.00'))
        cash_paid = sum((p[1] for p in payments if p[0].lower() in ["espèces", "especes", "cash"]), Decimal('0.00'))

        if total_paid >= total_due:
            overpayment = total_paid - total_due
            rendu = min(cash_paid, overpayment)
            return quantize_money(rendu), Decimal('0.00')
        else:
            return Decimal('0.00'), quantize_money(total_due - total_paid)


# Functions top-level pour la gestion des ventes et tickets

def process_sale_transaction(
    cart_items: List[Dict[str, Any]],
    total_tvac: float,
    payments: List[Tuple[str, float]],
    client_id: Optional[int] = None,
    cashier_name: str = "Admin",
    caisse_id: str = "POS-01",
    discount_percent: float = 0.0,
    change_given: float = 0.0,
    conn=None
) -> Dict[str, Any]:
    import database_manager
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        num_ticket = database_manager.generer_numero_ticket(cursor)

        tot_tvac_dec = quantize_money(Decimal(str(total_tvac)))
        discount_dec = quantize_money(Decimal(str(discount_percent)))
        change_dec = quantize_money(Decimal(str(change_given)))

        tot_htva_dec = Decimal('0.00')
        panier_formatted = []
        for it in cart_items:
            stock_id = it.get("stock_id") or it.get("id_stock") or it.get("id")
            px_tvac = quantize_money(Decimal(str(it.get("prix_vente_tvac") or it.get("prix_tvac") or it.get("price") or 0)))
            taux = Decimal(str(it.get("taux_tva", 0.21)))
            qty = int(it.get("quantite") or it.get("quantity") or 1)

            px_htva = quantize_money(px_tvac / (Decimal('1.00') + taux))
            tot_htva_dec += px_htva * Decimal(str(qty))

            panier_formatted.append({
                "stock_id": stock_id,
                "code_barre": it.get("code_barre") or it.get("barcode") or "",
                "nom": it.get("nom") or it.get("name") or "Article",
                "prix_vente_tvac": float(px_tvac),
                "quantite": qty,
                "taux_tva": float(taux)
            })

        tot_tva_dec = tot_tvac_dec - tot_htva_dec
        paiements_dec = [(p[0], float(quantize_money(Decimal(str(p[1]))))) for p in payments]
        main_payment_method = paiements_dec[0][0] if paiements_dec else "CB"

        ticket_id = database_manager.enregistrer_vente(
            cursor=cursor,
            numero_ticket=num_ticket,
            total_tvac=float(tot_tvac_dec),
            total_htva=float(tot_htva_dec),
            total_tva=float(tot_tva_dec),
            remise=float(discount_dec),
            methode_paiement=main_payment_method,
            id_client=client_id,
            rendu_monnaie=float(change_dec),
            panier=panier_formatted,
            vendeur_nom=cashier_name,
            date_heure=now_str,
            paiements=paiements_dec,
            caisse_id=caisse_id
        )

        conn.commit()

        return {
            "success": True,
            "ticket_id": ticket_id,
            "numero_ticket": num_ticket,
            "date_heure": now_str,
            "total_tvac": float(tot_tvac_dec),
            "total_htva": float(tot_htva_dec),
            "total_tva": float(tot_tva_dec),
            "rendu_monnaie": float(change_dec)
        }

    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if should_close and conn:
            conn.close()


def process_return_transaction(
    original_ticket_number: str,
    sales_detail_id: int,
    stock_id: Optional[int],
    refund_price: float,
    refund_mode: str = "Espèces",
    cashier_name: str = "Admin",
    conn=None
) -> Dict[str, Any]:
    import database_manager
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        new_ref = database_manager.enregistrer_remboursement(
            cursor=cursor,
            ticket_origine=original_ticket_number,
            vd_id=sales_detail_id,
            stock_id=stock_id,
            prix=Decimal(str(refund_price)),
            mode=refund_mode,
            vendeur_nom=cashier_name,
            date_heure=now_str
        )

        conn.commit()

        return {
            "success": True,
            "refund_ticket_number": new_ref,
            "amount_refunded": refund_price,
            "refund_mode": refund_mode,
            "date_heure": now_str
        }

    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if should_close and conn:
            conn.close()


# Façades de gestion des paniers en attente

def park_cart(
    panier: List[Dict[str, Any]],
    total_tvac: float,
    client_id: Optional[int] = None,
    client_name: str = "",
    discount: float = 0.0,
    note: str = "",
    conn=None
) -> int:
    import json
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
        
    try:
        c = conn.cursor()
        panier_serializable = []
        for item in panier:
            panier_serializable.append({
                "nom": item.get("nom"),
                "taille": item.get("taille"),
                "prix_vente_tvac": str(item.get("prix_vente_tvac")),
                "taux_tva": str(item.get("taux_tva", '0.21')),
                "stock_id": item.get("stock_id"),
                "en_solde": item.get("en_solde", 0),
                "prix_original_tvac": str(item["prix_original_tvac"]) if item.get("prix_original_tvac") is not None else None,
                "remise_label": item.get("remise_label", ""),
                "code_barre": item.get("code_barre")
            })
            
        panier_json = json.dumps(panier_serializable, ensure_ascii=False)
        c.execute("""
            INSERT INTO Paniers_En_Attente (client_id, client_nom, total_tvac, remise, panier_json, note)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (client_id, client_name, str(total_tvac), str(discount), panier_json, note))
        
        panier_id = c.lastrowid
        conn.commit()
        return panier_id
    finally:
        if should_close:
            conn.close()


def get_parked_carts(conn=None) -> List[Dict[str, Any]]:
    import json
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
        
    try:
        c = conn.cursor()
        c.execute("SELECT id, date_creation, client_id, client_nom, total_tvac, remise, panier_json, note FROM Paniers_En_Attente ORDER BY id DESC")
        rows = c.fetchall()
        paniers = []
        for r in rows:
            paniers.append({
                "id": r[0],
                "date_creation": r[1],
                "client_id": r[2],
                "client_nom": r[3],
                "total_tvac": float(r[4]) if r[4] is not None else 0.0,
                "remise": float(r[5]) if r[5] is not None else 0.0,
                "panier": json.loads(r[6]),
                "note": r[7] or ""
            })
        return paniers
    finally:
        if should_close:
            conn.close()


def restore_parked_cart(cart_id: int, conn=None) -> Optional[Dict[str, Any]]:
    import json
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
        
    try:
        c = conn.cursor()
        c.execute("SELECT id, date_creation, client_id, client_nom, total_tvac, remise, panier_json, note FROM Paniers_En_Attente WHERE id=?", (cart_id,))
        r = c.fetchone()
        if not r:
            return None
            
        res = {
            "id": r[0],
            "date_creation": r[1],
            "client_id": r[2],
            "client_nom": r[3],
            "total_tvac": float(r[4]) if r[4] is not None else 0.0,
            "remise": float(r[5]) if r[5] is not None else 0.0,
            "panier_raw": json.loads(r[6]),
            "note": r[7] or ""
        }
        
        c.execute("DELETE FROM Paniers_En_Attente WHERE id=?", (cart_id,))
        conn.commit()
        return res
    finally:
        if should_close:
            conn.close()


def delete_parked_cart(cart_id: int, conn=None) -> bool:
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True
    try:
        c = conn.cursor()
        c.execute("DELETE FROM Paniers_En_Attente WHERE id=?", (cart_id,))
        conn.commit()
        return True
    finally:
        if should_close:
            conn.close()
