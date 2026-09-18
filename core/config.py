"""
core.config - Alias de compatibilité vers kodo_core.config.

Cet ancien module dupliquait entièrement ShopConfig/ShopProfile, ce qui a
causé une divergence (get_db_path ignorant KODO_DB_PATH ici mais pas dans
kodo_core.config) et un incident réel d'écriture en base de production.
kodo_core.config est désormais l'unique source de vérité ; ce module ne
fait que la réexporter pour ne pas casser les imports existants
(`from core.config import ShopConfig`).
"""

from kodo_core.config import ShopConfig, ShopProfile

__all__ = ["ShopConfig", "ShopProfile"]
