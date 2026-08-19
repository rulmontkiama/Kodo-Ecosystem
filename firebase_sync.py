"""
Façade mince pour firebase_sync déléguant au module kodo_core.sync.firebase.
"""

from kodo_core.sync.firebase import (
    FirebaseSyncThread,
    FirebaseSync,
    start_sync_thread,
)

__all__ = [
    "FirebaseSyncThread",
    "FirebaseSync",
    "start_sync_thread",
]
