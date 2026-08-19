"""
Façade mince pour shopify_sync déléguant au module kodo_core.sync.shopify.
"""

from kodo_core.sync.shopify import (
    ShopifySyncThread,
    ShopifySync,
    import_shopify_catalog,
)

__all__ = [
    "ShopifySyncThread",
    "ShopifySync",
    "import_shopify_catalog",
]
