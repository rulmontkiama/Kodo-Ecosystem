"""
audit_trail - Alias de rétrocompatibilité vers kodo_core.db.audit_trail.
"""

from kodo_core.db.audit_trail import (
    verifier_chainage,
    verify_database_integrity,
    audit_complet,
    calculer_hash_cloture,
    calculer_hash_transaction,
    signer_ticket,
    signer_ledger,
    signer_rapport_z,
    record_audit_event,
    compute_sha256
)

if __name__ == "__main__":
    audit_complet()
