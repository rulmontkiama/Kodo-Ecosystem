"""
Kōdo POS - SKU Shopify d'une déclinaison (taille) d'un produit local.

Règle UNIQUE, partagée par l'export du catalogue au format CSV Shopify (`export_manager`) et par
la synchronisation (`kodo_core.sync.shopify`). Avant, l'export écrivait un SKU par taille
(`BI038-30`, `BI038-32`…) et ne posait le code-barres que sur la première taille, tandis que la
synchro cherchait la variante Shopify avec le code-barres du PRODUIT : toute vente, quelle que
soit la taille, retirait la pièce de la première taille en ligne.

Module sans dépendance (ni base, ni réseau) : il peut être importé de partout.
"""

import re

# Libellés désignant l'absence de déclinaison (comparaison en minuscules, espaces retirées).
TAILLES_SANS_DECLINAISON = ("", "unique", "taille unique", "tu", "default title", "__no_size__")


def libelle_taille(taille) -> str:
    """Libellé de taille tel qu'exporté (« Taille Unique » quand la ligne de stock n'en porte pas)."""
    return (str(taille) if taille is not None else "").strip() or "Taille Unique"


def est_sans_taille(tailles) -> bool:
    """
    Vrai si le produit n'est pas décliné : UNE seule ligne de stock, au libellé neutre.
    `tailles` : les libellés des lignes de stock du produit, dans l'ordre de `Stocks.id`.
    """
    tailles = list(tailles)
    if len(tailles) != 1:
        return False
    return (str(tailles[0]) if tailles[0] is not None else "").strip().lower() in TAILLES_SANS_DECLINAISON


def sku_variante(code_barre, produit_id, taille, index: int, sans_taille: bool) -> str:
    """
    SKU Shopify de la ligne de stock n° `index` (0 = la plus ancienne) d'un produit.

    - produit non décliné : le code-barres, sinon `KODO-<id>` ;
    - produit décliné : `<code-barres>-<taille>`, sinon `KODO-<id>-<taille>` ; la taille est
      réduite à [A-Za-z0-9_-] et remplacée par `V<n>` si rien n'en reste.
    """
    code = str(code_barre).strip() if code_barre else ""
    if sans_taille:
        return code if code else f"KODO-{produit_id}"
    suffixe = re.sub(r"[^A-Za-z0-9_-]", "", libelle_taille(taille))
    if not suffixe:
        suffixe = f"V{index + 1}"
    return f"{code}-{suffixe}" if code else f"KODO-{produit_id}-{suffixe}"


def skus_produit(code_barre, produit_id, tailles) -> list:
    """SKU de chaque ligne de stock du produit, dans l'ordre des `tailles` (ordre de `Stocks.id`)."""
    tailles = list(tailles)
    sans_taille = est_sans_taille(tailles)
    return [sku_variante(code_barre, produit_id, t, i, sans_taille) for i, t in enumerate(tailles)]


def prefixes_possibles(sku) -> list:
    """
    Codes produit dont `sku` pourrait être un SKU de déclinaison, du plus long au plus court.
    `BI038-XS-S` → [`BI038-XS`, `BI038`] : une taille peut elle-même contenir un tiret.
    """
    sku = str(sku or "").strip()
    return [sku[:i] for i in range(len(sku) - 1, 0, -1) if sku[i] == "-"]
