"""
Façade mince pour license_manager déléguant au module kodo_core.services.license.
"""

from kodo_core.services.license import (
    SECRET_SALT,
    API_LICENSE_VALIDATE_URL,
    load_plan_permissions,
    get_upsell_modal_text,
    get_machine_fingerprint,
    generate_local_signature,
    save_local_license,
    load_local_license,
    validate_license_online,
    check_license,
    get_license_info,
    activate_license_key,
)

__all__ = [
    "SECRET_SALT",
    "API_LICENSE_VALIDATE_URL",
    "load_plan_permissions",
    "get_upsell_modal_text",
    "get_machine_fingerprint",
    "generate_local_signature",
    "save_local_license",
    "load_local_license",
    "validate_license_online",
    "check_license",
    "get_license_info",
    "activate_license_key",
]
