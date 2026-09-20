# -*- coding: utf-8 -*-
"""
Moteur de Panier et Gestion des Ventes - Kōdo POS Core
Gère les calculs financiers (Decimal, arrondis bancaires), les remises (%, fixes),
les multi-règlements (Espèces, CB, Chèque, Avoir, Carte Cadeau), les tickets en attente,
les annulations et les retours/remboursements avec certification NF525.
"""

import json
import datetime
import sqlite3
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


def apply_belgian_cash_rounding(amount: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Applique l'arrondi légal belge à 5 centimes pour les paiements en espèces (Loi du 01/12/2019).
    IMMUNITÉ FISCALE / TVA :
      Les bases et montants de TVA sont calculés sur le montant brut initial.
      L'écart d'arrondi ne modifie en aucun cas la TVA due.
    Règles de l'arrondi belge :
      - Se termine par .01, .02 -> arrondi vers le bas à .00 (écart -0.01 / -0.02)
      - Se termine par .03, .04 -> arrondi vers le haut à .05 (écart +0.02 / +0.01)
      - Se termine par .06, .07 -> arrondi vers le bas à .05 (écart -0.01 / -0.02)
      - Se termine par .08, .09 -> arrondi vers le haut à .10 (écart +0.02 / +0.01)
    Retourne :
      (montant_arrondi, ecart_arrondi)
      où ecart_arrondi = montant_arrondi - montant_brut
    """
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    amount = quantize_money(amount)
    cents = int((amount * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    remainder = cents % 5
    if remainder in (1, 2):
        rounded_cents = cents - remainder
    elif remainder in (3, 4):
        rounded_cents = cents + (5 - remainder)
    else:
        rounded_cents = cents

    rounded_amount = quantize_money(Decimal(rounded_cents) / Decimal('100'))
    ecart = quantize_money(rounded_amount - amount)
    return rounded_amount, ecart


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


# Libellés qui désignent l'absence de déclinaison : l'écran Stocks écrit « Taille Unique », l'import
# Shopify « Unique », d'anciennes versions une taille vide.
_LIBELLES_TAILLE_UNIQUE = {"", "unique", "taille unique", "taille_unique", "default title", "__no_size__"}


def _norm_taille(taille) -> str:
    return str(taille or "").strip().casefold()


def _as_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _resolve_stock_id(cursor, it: Dict[str, Any]):
    """
    Détermine la ligne `Stocks.id` à vendre pour un article du panier.

    L'écran ne connaît que l'id PRODUIT (`Produits.id`) et la taille choisie. L'id de stock est un autre
    numéro (une ligne par taille) qui n'a aucune raison de coïncider avec l'id produit : les confondre
    faisait vendre, débiter et facturer un AUTRE article (ou refuser la vente, « Article introuvable »).
    On retrouve donc ici la ligne de stock à partir de (produit, taille).

    Un `stock_id` explicite reste accepté (import Live, rejeu hors-ligne) ; quand le produit est aussi
    connu, il doit lui appartenir.
    """
    stock_id = it.get("stock_id") or it.get("id_stock")
    product_id = it.get("product_id") or it.get("id_produit")

    if stock_id:
        stock_id = _as_int(stock_id)
        if product_id:
            cursor.execute("SELECT id_produit FROM Stocks WHERE id = ?", (stock_id,))
            row = cursor.fetchone()
            if row and _as_int(row[0]) != _as_int(product_id):
                raise ValueError(
                    f"Incohérence : le stock {stock_id} n'appartient pas à l'article {product_id} : vente refusée."
                )
        return stock_id

    if not product_id:
        legacy = it.get("id")  # appelants historiques : `id` désignait déjà l'id de stock
        if legacy:
            return _as_int(legacy)
        raise ValueError("Article sans stock_id : vente refusée (prix non vérifiable).")

    pid = _as_int(product_id)
    if not isinstance(pid, int):
        raise ValueError(f"Article introuvable en base (produit={product_id}) : vente refusée.")

    cursor.execute("SELECT id, taille FROM Stocks WHERE id_produit = ? ORDER BY id", (pid,))
    rows = cursor.fetchall()
    if not rows:
        raise ValueError(f"Article introuvable en base (produit={pid}) : vente refusée.")

    taille = it.get("taille") or it.get("size") or it.get("selectedSize")
    wanted = _norm_taille(taille)
    if wanted:
        exact = [r for r in rows if _norm_taille(r[1]) == wanted]
        if exact:
            return exact[0][0]
    if wanted in _LIBELLES_TAILLE_UNIQUE:
        if len(rows) == 1:
            return rows[0][0]
        uniques = [r for r in rows if _norm_taille(r[1]) in _LIBELLES_TAILLE_UNIQUE]
        if len(uniques) == 1:
            return uniques[0][0]

    cursor.execute("SELECT nom FROM Produits WHERE id = ?", (pid,))
    nom_row = cursor.fetchone()
    nom = nom_row[0] if nom_row else f"produit {pid}"
    tailles = ", ".join(dict.fromkeys(str(r[1] or "Taille Unique") for r in rows))
    if wanted and wanted not in _LIBELLES_TAILLE_UNIQUE:
        raise ValueError(f"Taille « {taille} » introuvable pour « {nom} » (tailles : {tailles}) : vente refusée.")
    raise ValueError(f"Précisez la taille de « {nom} » ({tailles}) : vente refusée.")


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
    conn=None,
    gift_card_code: Optional[str] = None
) -> Dict[str, Any]:
    import database_manager
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        cursor = conn.cursor()
        # Le serveur HTTP est MULTI-THREADÉ (ThreadingHTTPServer) : la lecture du stock, du
        # dernier numéro de ticket et du dernier hash de la chaîne NF525, puis leur écriture,
        # doivent tenir dans une seule transaction en écriture exclusive. En DEFERRED, deux
        # encaissements simultanés (double-clic sur « Encaisser », ou deux caisses sur la même
        # base) lisent le même stock, passent tous deux la garde anti-survente et produisent du
        # stock négatif fantôme (mesuré : -3 sur un stock de 5 pour 8 ventes parallèles).
        if not conn.in_transaction:
            cursor.execute("BEGIN IMMEDIATE")

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        num_ticket = database_manager.generer_numero_ticket(cursor)

        if not cart_items:
            raise ValueError("Panier vide : impossible d'enregistrer une vente sans article.")

        # Remise globale bornée [0, 100] : une valeur négative (survalorisation) ou
        # supérieure à 100% n'a aucun sens métier et produirait un total négatif.
        discount_dec = max(Decimal('0'), min(Decimal('100'), Decimal(str(discount_percent))))

        # SÉCURITÉ FINANCIÈRE : le prix et le taux de TVA de chaque ligne sont TOUJOURS
        # relus depuis Produits/Stocks (source d'autorité), jamais acceptés tels quels
        # depuis le client. `total_tvac` fourni par l'appelant n'est plus utilisé pour le
        # calcul : sans ce recalcul serveur, n'importe quel client (DevTools, appel API
        # direct) pouvait vendre un article catalogué à 500€ pour 0,01€ tout en décrémentant
        # le vrai stock, le serveur se contentant d'enregistrer tel quel le total annoncé.
        subtotal_brut_dec = Decimal('0.00')
        lignes_brutes = []
        stock_cache: Dict[Any, Any] = {}
        qty_demandee_par_stock: Dict[Any, int] = {}
        for it in cart_items:
            stock_id = _resolve_stock_id(cursor, it)

            if stock_id not in stock_cache:
                cursor.execute("""
                    SELECT s.id, s.quantite_actuelle, p.prix_vente_tvac, p.prix_solde_tvac, p.en_solde, p.taux_tva, p.nom, p.code_barre
                    FROM Stocks s JOIN Produits p ON s.id_produit = p.id
                    WHERE s.id = ?
                """, (stock_id,))
                stock_cache[stock_id] = cursor.fetchone()

            prod_row = stock_cache[stock_id]
            if not prod_row:
                raise ValueError(f"Article introuvable en base (stock_id={stock_id}) : vente refusée.")

            _sid, dispo, prix_catalogue, prix_solde, en_solde, taux_db, nom_db, code_barre_db = prod_row
            prix_autoritaire = (
                Decimal(str(prix_solde)) if (en_solde and prix_solde is not None) else Decimal(str(prix_catalogue or 0))
            )
            px_tvac = quantize_money(prix_autoritaire)
            taux = Decimal(str(taux_db if taux_db is not None else 0.21))

            qty = int(it.get("quantite") or it.get("quantity") or 1)
            if qty <= 0:
                raise ValueError(f"Quantité invalide ({qty}) pour stock_id={stock_id} : vente refusée.")

            qty_demandee_par_stock[stock_id] = qty_demandee_par_stock.get(stock_id, 0) + qty

            ligne_tvac_brute = quantize_money(px_tvac * Decimal(str(qty)))
            subtotal_brut_dec += ligne_tvac_brute

            lignes_brutes.append({
                "stock_id": stock_id,
                "code_barre": code_barre_db or it.get("code_barre") or it.get("barcode") or "",
                "nom": nom_db or it.get("nom") or it.get("name") or "Article",
                "prix_vente_tvac": float(px_tvac),
                "quantite": qty,
                "taux_tva": float(taux),
                "ligne_tvac_brute": ligne_tvac_brute,
            })

        # Garde anti-survente en TEMPS RÉEL : uniquement sur ce chemin (vente directe au
        # comptoir), pas dans enregistrer_vente lui-même — ce dernier est aussi rejoué par
        # OfflineSyncEngine pour des ventes hors-ligne déjà physiquement conclues, qu'on ne
        # peut pas rejeter après coup (cf. stratégie Last-Write-Wins de la synchro offline).
        for sid, qty_totale in qty_demandee_par_stock.items():
            dispo = stock_cache[sid][1]
            dispo = int(dispo) if dispo is not None else 0
            if dispo < qty_totale:
                raise ValueError(
                    f"Stock insuffisant pour l'article (stock_id={sid}) : disponible={dispo}, demandé={qty_totale}"
                )

        tot_tvac_dec = quantize_money(subtotal_brut_dec * (Decimal('1.00') - discount_dec / Decimal('100.00')))

        try:
            total_annonce_dec = quantize_money(Decimal(str(total_tvac)))
            if abs(total_annonce_dec - tot_tvac_dec) > Decimal('0.01'):
                print(
                    f"[VENTE WARNING] Total annoncé par le client ({total_annonce_dec}) diffère du total "
                    f"recalculé serveur ({tot_tvac_dec}) pour le ticket {num_ticket} — le total serveur fait foi."
                )
        except Exception:
            pass

        ratio_remise = (
            (tot_tvac_dec / subtotal_brut_dec) if subtotal_brut_dec > Decimal('0.00') else Decimal('1.00')
        )

        tot_htva_dec = Decimal('0.00')
        panier_formatted = []
        for ligne in lignes_brutes:
            taux = Decimal(str(ligne.pop("taux_tva")))
            ligne_tvac_brute = ligne.pop("ligne_tvac_brute")
            ligne_tvac_remisee = quantize_money(ligne_tvac_brute * ratio_remise)
            ligne_htva = quantize_money(ligne_tvac_remisee / (Decimal('1.00') + taux))
            tot_htva_dec += ligne_htva

            ligne["taux_tva"] = float(taux)
            panier_formatted.append(ligne)

        tot_tva_dec = tot_tvac_dec - tot_htva_dec

        # Remise réellement accordée en EUROS (et non le pourcentage brut) : le pourcentage
        # seul, stocké tel quel dans Tickets.remise, faisait additionner des "10" (pour 10%)
        # comme des euros dans le total_remises du bilan Z et des exports comptables.
        remise_montant_dec = quantize_money(subtotal_brut_dec - tot_tvac_dec)

        paiements_dec = []
        for p in payments:
            montant_p = quantize_money(Decimal(str(p[1])))
            if montant_p <= Decimal('0.00'):
                raise ValueError(f"Montant de paiement invalide ({montant_p}) pour le mode {p[0]!r}.")
            paiements_dec.append((p[0], montant_p))

        # Arrondi légal belge à 5 centimes sur le solde espèces (Loi du 01/12/2019).
        # IMMUNITÉ TVA : tot_tvac_dec, tot_htva_dec et tot_tva_dec restent strictement intouchés.
        # La classification passe par la source d'autorité unique du projet : un libellé
        # hérité ("CASH", "especes") ne doit jamais échapper à l'arrondi.
        def _is_cash(m):
            return database_manager.classer_moyen_paiement(m) == "especes"

        cash_tendered = sum((p[1] for p in paiements_dec if _is_cash(p[0])), Decimal('0.00'))
        non_cash_total = sum((p[1] for p in paiements_dec if not _is_cash(p[0])), Decimal('0.00'))
        ecart_arrondi_cash = Decimal('0.00')
        effective_total_due = tot_tvac_dec

        if cash_tendered > Decimal('0.00'):
            due_in_cash_raw = max(Decimal('0.00'), tot_tvac_dec - non_cash_total)
            if due_in_cash_raw > Decimal('0.00'):
                due_in_cash, ecart_arrondi_cash = apply_belgian_cash_rounding(due_in_cash_raw)
                # Une caisse pas encore alignée sur l'arrondi (ou un solde mixte saisi au
                # centime) transmet le montant BRUT : refuser la vente immobiliserait le
                # comptoir client devant soi. On enregistre alors l'encaissement RÉEL —
                # le tiroir contient ce que le client a effectivement remis — et on
                # recalcule l'écart d'arrondi sur cette réalité, jamais sur une hypothèse.
                manque = due_in_cash - cash_tendered
                if (Decimal('0.00') < manque <= Decimal('0.02') and cash_tendered >= due_in_cash_raw):
                    due_in_cash = quantize_money(cash_tendered)
                    ecart_arrondi_cash = quantize_money(due_in_cash - due_in_cash_raw)
                effective_total_due = quantize_money(non_cash_total + due_in_cash)

        # Rendu de monnaie recalculé serveur sur la base du montant dû effectif (arrondi si espèces)
        rendu_dec, reste_du_dec = CartEngine.calculate_change_due(effective_total_due, paiements_dec)
        if reste_du_dec > Decimal('0.00'):
            raise ValueError(
                f"Paiement insuffisant : {reste_du_dec} restant dû sur un total de {effective_total_due}."
            )

        # Redemption réelle de carte cadeau/avoir : sans ce contrôle serveur, choisir
        # "Avoir" comme moyen de paiement validait la vente pour n'importe quel code
        # (même inventé ou vide), sans jamais vérifier ni débiter une vraie carte —
        # une vente "gratuite" indistinguable d'une vente payée dans le ledger. Le débit
        # se fait dans la MÊME transaction que la vente (avant tout INSERT/UPDATE de
        # celle-ci) : si la vente échoue derrière, le rollback global annule aussi le débit.
        for methode_p, montant_p in paiements_dec:
            if str(methode_p or "").strip().lower() in ("avoir", "carte cadeau", "carte_cadeau", "gift card", "giftcard"):
                database_manager.utiliser_carte_cadeau(cursor, gift_card_code, montant_p, user_name=cashier_name)

        paiements_payload = [(p[0], float(p[1])) for p in paiements_dec]
        main_payment_method = paiements_payload[0][0] if paiements_payload else "CB"

        # Retry borné sur collision de numero_ticket (contrainte UNIQUE). Le serveur HTTP est
        # MULTI-THREADÉ : la collision peut survenir entre deux encaissements du même
        # processus comme entre deux caisses partageant le fichier SQLite. Plutôt que de faire
        # échouer une vente réellement payée par le client sur cette course, on régénère un
        # numéro et on retente (la contrainte UNIQUE garantit qu'aucun doublon ne peut jamais
        # être inséré silencieusement).
        ticket_id = None
        last_integrity_error = None
        for _attempt in range(5):
            try:
                ticket_id = database_manager.enregistrer_vente(
                    cursor=cursor,
                    numero_ticket=num_ticket,
                    total_tvac=float(tot_tvac_dec),
                    total_htva=float(tot_htva_dec),
                    total_tva=float(tot_tva_dec),
                    remise=float(remise_montant_dec),
                    methode_paiement=main_payment_method,
                    id_client=client_id,
                    rendu_monnaie=float(rendu_dec),
                    panier=panier_formatted,
                    vendeur_nom=cashier_name,
                    date_heure=now_str,
                    paiements=paiements_payload,
                    caisse_id=caisse_id,
                    ecart_arrondi_cash=float(ecart_arrondi_cash)
                )
                break
            except sqlite3.IntegrityError as ie:
                if "numero_ticket" not in str(ie):
                    raise
                last_integrity_error = ie
                num_ticket = database_manager.generer_numero_ticket(cursor)
        if ticket_id is None:
            raise last_integrity_error

        conn.commit()

        # Scellement fiscal inaltérable (Conformité Loi Anti-fraude TVA)
        fiscal_totals = {
            "total_ttc": tot_tvac_dec,
            "total_ht": tot_htva_dec,
            "total_tva": tot_tva_dec,
        }
        try:
            from kodo_core.services.fiscal_service import ensure_schema as ensure_fiscal_schema, seal_sale
            ensure_fiscal_schema(conn)
            seal_sale(conn, ticket_id, fiscal_totals)
        except Exception as fe:
            # Le scellement peut échouer après le commit de la vente : on journalise la
            # vente dans fiscal_unsealed_sales pour qu'elle soit rescellée au prochain
            # démarrage (reseal_pending_sales) au lieu de disparaître silencieusement
            # du journal fiscal.
            print(f"[FISCAL LEDGER WARNING] Impossible de sceller la vente {ticket_id}: {fe}")
            try:
                from kodo_core.services.fiscal_service import ensure_schema as ensure_fiscal_schema, record_unsealed_sale
                ensure_fiscal_schema(conn)
                record_unsealed_sale(conn, ticket_id, fiscal_totals, str(fe))
            except Exception as fe2:
                print(f"[FISCAL LEDGER WARNING] Impossible de journaliser l'échec de scellement pour {ticket_id}: {fe2}")

        # Nettoyage de la session Crash Recovery (panier validé avec succès)
        try:
            from kodo_core.services.crash_recovery import CrashRecoveryService
            CrashRecoveryService().clear_session()
        except Exception:
            pass

        return {
            "success": True,
            "ticket_id": ticket_id,
            "numero_ticket": num_ticket,
            "date_heure": now_str,
            "total_tvac": float(tot_tvac_dec),
            "total_htva": float(tot_htva_dec),
            "total_tva": float(tot_tva_dec),
            "rendu_monnaie": float(rendu_dec),
            "ecart_arrondi_cash": float(ecart_arrondi_cash),
            "total_a_payer_arrondi": float(effective_total_due)
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
    quantity: int = 1,
    conn=None
) -> Dict[str, Any]:
    import database_manager
    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        cursor = conn.cursor()
        if not conn.in_transaction:
            cursor.execute("BEGIN IMMEDIATE")

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # `refund_price` (fourni par l'appelant) n'est PAS utilisé pour le calcul financier
        # réel : enregistrer_remboursement relit toujours le prix vendu en base et renvoie
        # le montant réellement remboursé, qu'on utilise ici pour la réponse plutôt que la
        # valeur annoncée par le client (potentiellement falsifiée).
        new_ref, montant_reel_rembourse = database_manager.enregistrer_remboursement(
            cursor=cursor,
            ticket_origine=original_ticket_number,
            vd_id=sales_detail_id,
            stock_id=stock_id,
            prix=Decimal(str(refund_price)),
            mode=refund_mode,
            vendeur_nom=cashier_name,
            date_heure=now_str,
            quantite=quantity
        )

        conn.commit()

        return {
            "success": True,
            "refund_ticket_number": new_ref,
            "amount_refunded": float(-montant_reel_rembourse),
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
