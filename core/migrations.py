"""
core.migrations - Alias de rétrocompatibilité vers kodo_core.db.migrations.
"""

from kodo_core.db.migrations import MigrationError, MigrationManager, initialiser_db

__all__ = ["MigrationError", "MigrationManager", "initialiser_db"]
