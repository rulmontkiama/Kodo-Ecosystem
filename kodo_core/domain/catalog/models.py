# -*- coding: utf-8 -*-
"""
Modèles métier purs du catalogue / stock - Kōdo POS Core.
Aucune dépendance UI ni BDD.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class StockMovementType(str, Enum):
    VENTE = "VENTE"
    RETOUR = "RETOUR"
    INVENTAIRE = "INVENTAIRE"
    AJUSTEMENT = "AJUSTEMENT"


@dataclass
class StockMovement:
    """Mouvement de stock tracé pour audit (table stock_movements)."""

    product_id: str
    type_mouvement: StockMovementType
    delta: int
    stock_final: int
    date_heure: str
    variation_id: Optional[str] = None
    motif: Optional[str] = None
    id: Optional[int] = None


@dataclass
class StockAlert:
    """Article dont le stock actuel est descendu au niveau ou sous le seuil d'alerte."""

    product_id: str
    stock_actuel: int
    stock_alerte: int
    variation_id: Optional[str] = None
