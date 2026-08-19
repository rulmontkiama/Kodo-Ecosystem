# -*- coding: utf-8 -*-
"""
Routes API Catalogue Produit & Gestion des Stocks - Kōdo POS Core
"""

from typing import Dict, Any, Tuple, Optional
from kodo_core.domain.catalog.inventory_manager import InventoryManager


def handle_products_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any]) -> Optional[Tuple[int, Any]]:
    """
    Gestionnaire de requêtes pour le catalogue, catégories, marques et stocks.
    """

    # 1. Liste des produits
    if method == "GET" and path == "/api/products":
        cat = query.get("category", [None])[0]
        brand = query.get("brand", [None])[0]
        search = query.get("search", [None])[0]
        products = InventoryManager.get_all_products(category=cat, brand=brand, search=search)
        return 200, products

    # 2. Ajout / Édition Produit
    elif method == "POST" and path == "/api/products":
        res = InventoryManager.save_product(data)
        return 200, {"success": True, "productId": res["product_id"]}

    # 3. Suppression Produit
    elif method == "DELETE" and path == "/api/products":
        prod_ids = query.get('id', [])
        if not prod_ids and 'id' in data:
            prod_ids = [str(data['id'])]
        if not prod_ids:
            return 400, {"error": "ID produit manquant"}

        InventoryManager.delete_product(int(prod_ids[0]))
        return 200, {"success": True}

    # 4. Liste des catégories
    elif method == "GET" and path == "/api/categories":
        cats = InventoryManager.get_categories()
        return 200, cats

    # 5. Ajouter Catégorie
    elif method == "POST" and path == "/api/categories":
        name = data.get('name') or data.get('nom')
        if name:
            InventoryManager.add_category(name)
        return 200, {"success": True}

    # 6. Supprimer Catégorie
    elif method == "DELETE" and path == "/api/categories":
        names = query.get('name', [])
        if not names and 'name' in data:
            names = [str(data['name'])]
        if not names:
            return 400, {"error": "Nom catégorie manquant"}

        InventoryManager.delete_category(names[0])
        return 200, {"success": True}

    # 7. Liste des marques
    elif method == "GET" and path == "/api/brands":
        brands = InventoryManager.get_brands()
        return 200, brands

    # 8. Ajouter Marque
    elif method == "POST" and path == "/api/brands":
        name = data.get('name') or data.get('nom')
        if name:
            InventoryManager.add_brand(name)
        return 200, {"success": True}

    # 9. Alertes Stock Bas
    elif method == "GET" and path == "/api/stock/alerts":
        alerts = InventoryManager.get_low_stock_alerts()
        return 200, alerts

    return None
