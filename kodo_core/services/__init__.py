"""
Kōdo POS Core Services Module
Services du noyau (Gestion de licence HWID, Auto-updater).
"""

from .license import (
    get_machine_fingerprint,
    generate_local_signature,
    save_local_license,
    load_local_license,
    validate_license_online,
    check_license,
    get_license_info,
    activate_license_key,
    load_plan_permissions,
    get_upsell_modal_text,
)

from .updater import (
    parse_version,
    get_installed_version,
    check_for_updates_sync,
    apply_remote_update_sync,
    AppUpdateEngine,
    UpdateError,
)

__all__ = [
    "get_machine_fingerprint",
    "generate_local_signature",
    "save_local_license",
    "load_local_license",
    "validate_license_online",
    "check_license",
    "get_license_info",
    "activate_license_key",
    "load_plan_permissions",
    "get_upsell_modal_text",
    "parse_version",
    "get_installed_version",
    "check_for_updates_sync",
    "apply_remote_update_sync",
    "AppUpdateEngine",
    "UpdateError",
]
