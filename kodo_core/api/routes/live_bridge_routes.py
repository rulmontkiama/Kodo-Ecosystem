# -*- coding: utf-8 -*-
"""
Routes API du pont Live Shopping (export stock / import ventes par fichiers JSON) - Kōdo POS Core
Toutes les routes sont sous /api/live-bridge/*.
"""

import io
from typing import Dict, Any, Tuple, Optional

from kodo_core.domain.live.live_bridge import LiveBridge, LiveBridgeError


def _q(query: Dict[str, Any], key: str) -> Optional[str]:
    return query.get(key, [None])[0]


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def handle_live_bridge_request(
    method: str, path: str, query: Dict[str, Any], data: Dict[str, Any]
) -> Optional[Tuple[int, Any]]:
    if not path.startswith("/api/live-bridge/"):
        return None

    try:
        # GET /api/live-bridge/stock : déclinaisons exportables (écran d'export)
        if method == "GET" and path == "/api/live-bridge/stock":
            return 200, {"items": LiveBridge.list_exportable_stock()}

        # POST /api/live-bridge/export
        # Body: { items: [{stock_id, quantite}], session_reference?, boutique? }
        if method == "POST" and path == "/api/live-bridge/export":
            return 200, LiveBridge.export_stock(
                data.get("items"), data.get("session_reference"), data.get("boutique"))

        # POST /api/live-bridge/import/preview   Body: { content, default_payment? }
        if method == "POST" and path == "/api/live-bridge/import/preview":
            return 200, LiveBridge.preview_import(
                data.get("content"), data.get("default_payment") or "especes")

        # POST /api/live-bridge/import/apply   Body: { content, default_payment?, cashier_name? }
        if method == "POST" and path == "/api/live-bridge/import/apply":
            return 200, LiveBridge.apply_import(
                data.get("content"), data.get("default_payment") or "especes",
                (data.get("cashier_name") or "Live Shopping").strip() or "Live Shopping")

        # GET /api/live-bridge/imports : historique des imports
        if method == "GET" and path == "/api/live-bridge/imports":
            return 200, {"imports": LiveBridge.list_imports()}

        # GET /api/live-bridge/bordereau?order_id=X | import_id=X : PDF des bordereaux de livraison
        if method == "GET" and path == "/api/live-bridge/bordereau":
            orders = LiveBridge.get_delivery_orders(
                order_id=_int_or_none(_q(query, "order_id")), import_id=_int_or_none(_q(query, "import_id")))
            if not orders:
                return 404, {"error": "Aucune commande en livraison pour ce bordereau."}
            from kodo_core.hardware.pdf import generer_bordereaux_livraison_pdf
            buffer = io.BytesIO()
            generer_bordereaux_livraison_pdf(orders, buffer)
            return 200, buffer.getvalue(), {
                "Content-Type": "application/pdf",
                "Content-Disposition": 'inline; filename="bordereaux-livraison.pdf"',
            }
    except LiveBridgeError as e:
        return 400, {"error": str(e), "details": e.details}
    except Exception as e:
        return 500, {"error": str(e)}

    return None
