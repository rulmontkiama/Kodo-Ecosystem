# -*- coding: utf-8 -*-
"""
Routes API Client & Fidélité (CRM) - Kōdo POS Core
"""

from typing import Dict, Any, Tuple, Optional
from kodo_core.domain.customers.crm import CRMManager


def handle_clients_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any]) -> Optional[Tuple[int, Any]]:
    """
    Gestionnaire de requêtes pour les clients et la fidélité.
    """

    # 1. Liste des clients
    if method == "GET" and path == "/api/clients":
        search = query.get("search", [None])[0]
        clients = CRMManager.get_all_customers(search=search)
        return 200, clients

    # 2. Ajout / Édition Client
    elif method == "POST" and path == "/api/clients":
        res = CRMManager.save_customer(data)
        return 200, {"success": True, "clientId": str(res["client_id"])}

    # 3. Suppression Client
    elif method == "DELETE" and path == "/api/clients":
        c_ids = query.get('id', [])
        if not c_ids and 'id' in data:
            c_ids = [str(data['id'])]
        if not c_ids:
            return 400, {"error": "ID client manquant"}

        CRMManager.delete_customer(int(c_ids[0]))
        return 200, {"success": True}

    # 4. Historique des achats d'un client
    elif method == "GET" and path.startswith("/api/clients/") and path.endswith("/history"):
        parts = path.strip("/").split("/")
        if len(parts) >= 3:
            try:
                client_id = int(parts[1])
                history = CRMManager.get_customer_purchase_history(client_id)
                return 200, history
            except ValueError:
                pass
        return 400, {"error": "ID client invalide"}

    # 5. Conversion / Échange de points de fidélité
    elif method == "POST" and path == "/api/clients/points/redeem":
        client_id = data.get("client_id") or data.get("clientId")
        points = int(data.get("points", 0))
        if not client_id or points <= 0:
            return 400, {"error": "ID client et nombre de points valides requis"}

        res = CRMManager.redeem_points_for_discount(int(client_id), points)
        if res["success"]:
            return 200, res
        return 400, res

    return None
