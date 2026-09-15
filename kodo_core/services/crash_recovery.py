# -*- coding: utf-8 -*-
"""
Service de Crash Recovery du panier - Kōdo POS Core.
Persiste un snapshot JSON du panier en cours dans un fichier de session local,
avec écriture atomique (.tmp + os.replace) pour survivre à une coupure de courant.
Aucune dépendance UI ni BDD.
"""

import json
import os
from typing import Optional

from kodo_core.config import ShopConfig
from kodo_core.domain.sales.models import Cart

DEFAULT_SESSION_FILENAME = "panier_session.json"


class CrashRecoveryService:
    """Gère la persistance et la restauration d'une session panier non finalisée."""

    def __init__(self, session_path: Optional[str] = None):
        self.session_path = session_path or os.path.join(
            ShopConfig.get_sessions_dir(), DEFAULT_SESSION_FILENAME
        )

    def save_snapshot(self, cart: Cart) -> None:
        """Sauvegarde un snapshot atomique du panier (écriture .tmp puis os.replace)."""
        directory = os.path.dirname(self.session_path) or "."
        os.makedirs(directory, exist_ok=True)

        tmp_path = f"{self.session_path}.tmp"
        payload = json.dumps(cart.to_dict(), ensure_ascii=False, indent=2)

        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, self.session_path)

    def has_pending_recovery(self) -> bool:
        """Détecte si une session non finalisée existe sur le disque."""
        return os.path.isfile(self.session_path)

    def restore_cart_session(self) -> Cart:
        """Recharge le panier (articles, remises) depuis le snapshot de session."""
        with open(self.session_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Cart.from_dict(data)

    def clear_session(self) -> None:
        """Supprime le fichier de snapshot (vente validée ou panier vidé manuellement)."""
        try:
            os.remove(self.session_path)
        except FileNotFoundError:
            pass
