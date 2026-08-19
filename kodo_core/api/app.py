# -*- coding: utf-8 -*-
"""
Serveur API REST Modulaire - Kōdo POS Core
Point d'entrée de l'application REST API dispatchant les requêtes HTTP
vers les routeurs modulaires (POS, Produits, Clients, Stats, Backups, Système).
"""

import json
from typing import Dict, Any, Tuple, Optional, List, Callable
from urllib.parse import parse_qs, urlparse

from kodo_core.api.routes.pos_routes import handle_pos_request
from kodo_core.api.routes.products_routes import handle_products_request
from kodo_core.api.routes.clients_routes import handle_clients_request
from kodo_core.api.routes.stats_routes import handle_stats_request
from kodo_core.api.routes.backup_routes import handle_backup_request
from kodo_core.api.routes.system_routes import handle_system_request


class KodoAPIApp:
    """
    Application et Dispatcher principal de l'API REST Kōdo POS Core.
    """

    def __init__(self):
        self.route_handlers: List[Callable] = [
            handle_pos_request,
            handle_products_request,
            handle_clients_request,
            handle_stats_request,
            handle_backup_request,
            handle_system_request
        ]

    def handle_request(
        self,
        method: str,
        path: str,
        query: Dict[str, Any],
        headers: Dict[str, str],
        data: Dict[str, Any]
    ) -> Tuple[int, Any, Dict[str, str]]:
        """
        Traite une requête HTTP API et retourne (status_code, content, headers).
        """
        for handler in self.route_handlers:
            res = handler(method, path, query, data)
            if res is not None:
                if len(res) == 3:
                    status_code, content, custom_headers = res
                    return status_code, content, custom_headers or {}
                else:
                    status_code, content = res
                    headers_dict = {'Content-Type': 'application/json; charset=utf-8'}
                    return status_code, content, headers_dict

        return 404, {"error": "Route API non trouvée"}, {'Content-Type': 'application/json; charset=utf-8'}


# Instance singleton globale de l'application API Core
kodo_app = KodoAPIApp()
