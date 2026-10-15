# -*- coding: utf-8 -*-
"""
Moteur financier de vente (FinancialEngine) - Kōdo POS Core.
Calculs financiers purs, déterministes et immuables pour la conformité NF525 / LNE.

RÈGLES D'OR DE L'IMMUNITÉ FISCALE :
1. Zéro float : Arithmétique 100% Decimal avec arrondi monétaire ROUND_HALF_UP à 0.01.
2. Invariant de dérivation TVA :
     total_htva = sum(lignes_htva)
     total_tva  = total_tvac - total_htva
   La TVA est TOUJOURS dérivée du montant TTC net après remises.
3. Arrondi légal belge (Loi du 01/12/2019) :
   - Strictement restreint au solde payé en ESPÈCES.
   - Si paiement 100% non-espèces (CB, Virement, Avoir, etc.) : ecart_arrondi_cash == Decimal('0.00').
   - L'écart d'arrondi espèces est une opération de caisse isolée ; il ne modifie JAMAIS la base fiscale (HTVA/TVA).
4. Ventilation multi-taux :
   - Le résidu d'arrondi (quelques centimes) est affecté au taux au TVAC le plus lourd (déterministe).
   - La somme des TVAC ventilées est STRICTEMENT égale au total_tvac facturé.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from kodo_core.domain.sales.models import (
    CartDiscount,
    CartItem,
    CartTotal,
    DiscountType,
    FinancialReceipt,
    VatBreakdownLine,
    quantize_money,
    to_decimal,
)


ZERO = Decimal("0.00")
ONE_HUNDRED = Decimal("100.00")
FIVE_CENTS = Decimal("0.05")


def is_cash_payment(method: str) -> bool:
    """Détermine si un moyen de paiement relève des espèces physiques soumises à l'arrondi belge.

    Délègue à `database_manager.classer_moyen_paiement` (source d'autorité unique du projet :
    les anciennes versions écrivent « QR_Code » / « Bancontact », une liste locale divergeait).
    Import différé : le module reste importable sans base de données.
    """
    from database_manager import classer_moyen_paiement
    return classer_moyen_paiement(method) == "especes"


def _first_present(source: Dict[str, Any], keys: Sequence[str], default: Any) -> Any:
    """Première clé présente ET non None. Un `0` explicite (taux de TVA 0 %, remise 0) est une
    valeur, pas une absence : l'ancien `a or b or défaut` le remplaçait par le défaut (21 %)."""
    for key in keys:
        if source.get(key) is not None:
            return source[key]
    return default


def _clamp_percent(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE_HUNDRED, value))


def apply_belgian_cash_rounding(amount: Union[Decimal, str, int, float]) -> Tuple[Decimal, Decimal]:
    """Applique l'arrondi légal belge aux 5 centimes les plus proches (Loi du 01/12/2019).

    Règles :
      - Se termine par .01, .02 -> arrondi vers le bas à .00 (écart -0.01 / -0.02)
      - Se termine par .03, .04 -> arrondi vers le haut à .05 (écart +0.02 / +0.01)
      - Se termine par .06, .07 -> arrondi vers le bas à .05 (écart -0.01 / -0.02)
      - Se termine par .08, .09 -> arrondi vers le haut à .10 (écart +0.02 / +0.01)

    Retourne :
      (montant_arrondi, ecart_arrondi)
      où ecart_arrondi = montant_arrondi - montant_brut
    """
    amount_dec = quantize_money(to_decimal(amount, "apply_belgian_cash_rounding.amount"))
    if amount_dec <= ZERO:
        return ZERO, ZERO

    cents = int((amount_dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    remainder = cents % 5
    if remainder in (1, 2):
        rounded_cents = cents - remainder
    elif remainder in (3, 4):
        rounded_cents = cents + (5 - remainder)
    else:
        rounded_cents = cents

    rounded_amount = quantize_money(Decimal(rounded_cents) / Decimal("100"))
    ecart = quantize_money(rounded_amount - amount_dec)
    return rounded_amount, ecart


class FinancialEngine:
    """Moteur financier pur et sans état pour le traitement comptable des paniers et reçus."""

    @staticmethod
    def compute_cart_totals(
        items: Sequence[Union[CartItem, Dict[str, Any]]],
        global_discount_percent: Union[Decimal, float, str] = ZERO,
        global_discount_amount: Union[Decimal, float, str] = ZERO,
    ) -> CartTotal:
        """Calcule les totaux purs d'un panier d'articles avec ventilation TVA exacte."""
        parsed_items: List[Tuple[Decimal, int, Decimal]] = []
        subtotal_brut = ZERO

        for it in items:
            if isinstance(it, CartItem):
                px_unit = quantize_money(it.unit_price_ttc)
                qty = it.quantity
                rate = to_decimal(it.vat_rate, "vat_rate")
                if it.discount:
                    if it.discount.type == DiscountType.PERCENT:
                        px_unit = quantize_money(px_unit * (Decimal("1.00") - (it.discount.value / ONE_HUNDRED)))
                    elif it.discount.type == DiscountType.AMOUNT:
                        px_unit = max(ZERO, quantize_money(px_unit - it.discount.value))
            elif isinstance(it, dict):
                px_raw = _first_present(it, ("prix_vente_tvac", "unit_price_tvac", "unit_price_ttc"), 0)
                px_unit = quantize_money(to_decimal(px_raw, "dict.prix"))
                qty = int(_first_present(it, ("quantite", "quantity"), 1))
                rate = to_decimal(_first_present(it, ("taux_tva", "vat_rate"), Decimal("0.21")), "dict.rate")
                # Remises ligne éventuelles (pourcentage borné [0, 100] : 150 % donnait un prix négatif)
                disc_pct = _clamp_percent(to_decimal(it.get("discount_percent") or 0, "disc_pct"))
                disc_amt = to_decimal(it.get("discount_amount") or 0, "disc_amt")
                if disc_pct > ZERO:
                    px_unit = quantize_money(px_unit * (Decimal("1.00") - (disc_pct / ONE_HUNDRED)))
                if disc_amt > ZERO:
                    px_unit = max(ZERO, quantize_money(px_unit - disc_amt))
            else:
                raise TypeError(f"Type d'article non supporté : {type(it)}")

            if qty <= 0:
                raise ValueError(f"Quantité invalide ({qty}) : doit être strictement positive.")

            line_tvac = quantize_money(px_unit * Decimal(str(qty)))
            subtotal_brut += line_tvac
            parsed_items.append((line_tvac, qty, rate))

        # Remise globale
        disc_pct_dec = _clamp_percent(to_decimal(global_discount_percent, "global_discount_percent"))
        disc_amt_dec = max(ZERO, to_decimal(global_discount_amount, "global_discount_amount"))
        # Pourcentage appliqué au sous-total EN UNE FOIS puis arrondi (identique à la production :
        # `quantize(sous_total * (1 - pct/100))`), puis remise en montant ; remise = sous-total - final.
        final_tvac = quantize_money(subtotal_brut * (Decimal("1.00") - disc_pct_dec / ONE_HUNDRED))
        final_tvac = max(ZERO, quantize_money(final_tvac - disc_amt_dec))
        total_discount = quantize_money(subtotal_brut - final_tvac)
        ratio = (final_tvac / subtotal_brut) if subtotal_brut > ZERO else Decimal("1.00")

        # Ventilation par taux de TVA
        vat_buckets: Dict[Decimal, Dict[str, Decimal]] = {}
        total_htva = ZERO

        for line_tvac_brute, _qty, rate in parsed_items:
            line_tvac_net = quantize_money(line_tvac_brute * ratio)
            line_htva_net = quantize_money(line_tvac_net / (Decimal("1.00") + rate))
            total_htva += line_htva_net

            if rate not in vat_buckets:
                vat_buckets[rate] = {"htva": ZERO, "tvac": ZERO}
            vat_buckets[rate]["htva"] += line_htva_net
            vat_buckets[rate]["tvac"] += line_tvac_net

        total_tva = quantize_money(final_tvac - total_htva)

        # Réconciliation de l'arrondi au centime sur la ventilation TVAC (résidu au taux le plus lourd)
        if vat_buckets:
            sum_bucket_tvac = sum((b["tvac"] for b in vat_buckets.values()), ZERO)
            residu = quantize_money(final_tvac - sum_bucket_tvac)
            if residu != ZERO:
                # Allocation déterministe au taux pesant le plus lourd (puis taux le plus élevé)
                target_rate = max(vat_buckets.keys(), key=lambda r: (vat_buckets[r]["tvac"], r))
                vat_buckets[target_rate]["tvac"] += residu

        vat_lines = [
            VatBreakdownLine(
                vat_rate=rate,
                base_ht=b["htva"],
                montant_tva=quantize_money(b["tvac"] - b["htva"]),
                montant_ttc=b["tvac"],
            )
            for rate, b in sorted(vat_buckets.items(), key=lambda x: x[0], reverse=True)
        ]

        return CartTotal(
            total_ht=total_htva,
            total_tva=total_tva,
            total_ttc=final_tvac,
            total_discount_ttc=total_discount,
            vat_breakdown=vat_lines,
        )

    @classmethod
    def compute_financial_receipt(
        cls,
        items: Sequence[Union[CartItem, Dict[str, Any]]],
        payments: Sequence[Tuple[str, Union[Decimal, float, str]]],
        global_discount_percent: Union[Decimal, float, str] = ZERO,
        global_discount_amount: Union[Decimal, float, str] = ZERO,
    ) -> FinancialReceipt:
        """Calcule et produit le FinancialReceipt immuable et fiscalement étanche (NF525).

        Garantit :
        - Invariant TVA : total_htva + total_tva == total_tvac.
        - Isolation de l'arrondi cash belge : ecart_arrondi_cash == 0.00 si paiement sans espèces.
        - Calcul exact du rendu de monnaie et vérification de suffisance du règlement.
        """
        cart_total = cls.compute_cart_totals(
            items=items,
            global_discount_percent=global_discount_percent,
            global_discount_amount=global_discount_amount,
        )

        # Validation et quantification des paiements
        paiements_dec: List[Tuple[str, Decimal]] = []
        for method, amt in payments:
            amt_dec = quantize_money(to_decimal(amt, "payment_amount"))
            if amt_dec <= ZERO:
                raise ValueError(f"Montant de paiement invalide ({amt_dec}) pour le mode {method!r}.")
            paiements_dec.append((method, amt_dec))

        # Analyse des moyens de paiement : Espèces vs Non-espèces
        cash_tendered = sum((amt for m, amt in paiements_dec if is_cash_payment(m)), ZERO)
        non_cash_tendered = sum((amt for m, amt in paiements_dec if not is_cash_payment(m)), ZERO)

        ecart_arrondi_cash = ZERO
        effective_total_due = cart_total.total_ttc

        # RÈGLE D'OR : L'arrondi 5 centimes s'applique UNIQUEMENT s'il y a des espèces
        if cash_tendered > ZERO:
            due_in_cash_raw = max(ZERO, cart_total.total_ttc - non_cash_tendered)
            if due_in_cash_raw > ZERO:
                due_in_cash, ecart_arrondi_cash = apply_belgian_cash_rounding(due_in_cash_raw)
                # Tolérance comptoir : si le client a remis le montant brut exact sans l'arrondi
                manque = due_in_cash - cash_tendered
                if ZERO < manque <= Decimal("0.02") and cash_tendered >= due_in_cash_raw:
                    due_in_cash = quantize_money(cash_tendered)
                    ecart_arrondi_cash = quantize_money(due_in_cash - due_in_cash_raw)
                effective_total_due = quantize_money(non_cash_tendered + due_in_cash)
        else:
            # Sécurité absolue : tout paiement sans espèces doit avoir un écart d'arrondi strictement nul
            ecart_arrondi_cash = ZERO

        total_paid = cash_tendered + non_cash_tendered
        reste_du = max(ZERO, quantize_money(effective_total_due - total_paid))
        if reste_du > ZERO:
            raise ValueError(
                f"Paiement insuffisant : {reste_du} restant dû sur un total effectif de {effective_total_due}."
            )

        # On ne rend que des espèces : un trop-perçu carte/QR/avoir ne produit pas de rendu.
        rendu = min(cash_tendered, max(ZERO, quantize_money(total_paid - effective_total_due)))

        return FinancialReceipt(
            total_tvac=cart_total.total_ttc,
            total_htva=cart_total.total_ht,
            total_tva=cart_total.total_tva,
            remise=cart_total.total_discount_ttc,
            rendu=rendu,
            ecart_arrondi_cash=ecart_arrondi_cash,
            payments=paiements_dec,
            vat_breakdown=cart_total.vat_breakdown,
        )
