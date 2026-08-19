"""
Services - Update Checker (Façade vers kodo_core.services.updater).
"""

from kodo_core.services.updater import (
    CURRENT_VERSION,
    UPDATE_ENDPOINTS as VERSION_URLS,
    BROWSER_USER_AGENT,
    DEFAULT_HEADERS as HEADERS,
    parse_version,
    get_installed_version,
    check_for_updates_sync,
    get_target_dist_dir,
    apply_remote_update_sync,
)

__all__ = [
    "CURRENT_VERSION",
    "VERSION_URLS",
    "BROWSER_USER_AGENT",
    "HEADERS",
    "parse_version",
    "get_installed_version",
    "check_for_updates_sync",
    "get_target_dist_dir",
    "apply_remote_update_sync",
]
