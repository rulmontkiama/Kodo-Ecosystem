# -*- coding: utf-8 -*-
"""
Routes API Live Shopping - Kōdo POS Core
Gère les sessions live, la file d'attente, les acheteurs et l'encaissement.
"""

from typing import Dict, Any, Tuple, Optional
from kodo_core.domain.live.live_manager import LiveManager


def handle_live_request(
    method: str, path: str, query: Dict[str, Any], data: Dict[str, Any], headers: Optional[Dict[str, str]] = None
) -> Optional[Tuple[int, Any]]:
    """
    Gestionnaire de requêtes pour le module Live Shopping.
    Toutes les routes sont sous /api/live/* et /api/live/admin/*.
    Retourne (status_code, response_data) ou None si la route ne correspond pas.
    """

    # -------------------------------------------------------------------------
    # 1. SESSION ACTIVE (Spectateur & Admin)
    # GET /api/live/session
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/live/session":
        session = LiveManager.get_active_session()
        if session:
            return 200, {"session": session, "active": True}
        return 200, {"session": None, "active": False}

    # -------------------------------------------------------------------------
    # 2. LISTE DES SESSIONS (Admin)
    # GET /api/live/sessions
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/live/sessions":
        sessions = LiveManager.get_all_sessions()
        return 200, sessions

    # -------------------------------------------------------------------------
    # 3. CRÉER / METTRE À JOUR UNE SESSION (Admin)
    # POST /api/live/session
    # Body: { titre, produit_vedette_id, action?, statut?, notes? }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/session":
        try:
            result = LiveManager.create_or_update_session(data)
            return 200, result
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 4. METTRE À JOUR LE PRODUIT VEDETTE EN DIRECT (Admin)
    # POST /api/live/session/featured
    # Body: { session_id, product_id }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/session/featured":
        session_id = data.get("session_id")
        product_id = data.get("product_id")
        if not session_id:
            return 400, {"error": "session_id requis"}
        try:
            ok = LiveManager.set_featured_product(int(session_id), int(product_id) if product_id else None)
            return 200, {"success": ok}
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 5. CATALOGUE LIVE (Spectateur & Admin)
    # GET /api/live/catalog?session_id=X
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/live/catalog":
        session_id = query.get("session_id", [None])[0]
        try:
            catalog = LiveManager.get_live_catalog(
                session_id=int(session_id) if session_id else None
            )
            return 200, catalog
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 6. INSCRIPTION / MISE À JOUR ACHETEUR LIVE (Spectateur)
    # POST /api/live/register
    # Body: { nom, prenom, telephone, email, pseudo_social,
    #          mode_reception, adresse_rue, code_postal, ville, pays,
    #          taille_haut, taille_bas, pointure, notes }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/register":
        try:
            result = LiveManager.register_buyer(data)
            return 200, result
        except ValueError as ve:
            return 400, {"error": str(ve)}
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 7. SOUMETTRE UNE RÉSERVATION (Spectateur)
    # POST /api/live/claim
    # Body: { session_id, buyer_id, product_id, taille, quantite, client_id? }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/claim":
        required = ["session_id", "buyer_id", "product_id", "taille"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return 400, {"error": f"Champs requis manquants : {', '.join(missing)}"}
        try:
            result = LiveManager.submit_claim(data)
            if result.get("success"):
                return 200, result
            return 400, result
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 8. MES RÉSERVATIONS (Spectateur)
    # GET /api/live/my-claims?buyer_id=X&session_id=Y
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/live/my-claims":
        buyer_id = query.get("buyer_id", [None])[0]
        session_id = query.get("session_id", [None])[0]
        if not buyer_id or not session_id:
            return 400, {"error": "buyer_id et session_id requis"}
        try:
            claims = LiveManager.get_my_claims(int(buyer_id), int(session_id))
            return 200, claims
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 9. TOUTES LES CLAIMS (Admin - File d'attente)
    # GET /api/live/admin/claims?session_id=X&statut_attribution=Y&statut_paiement=Z
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/live/admin/claims":
        session_id = query.get("session_id", [None])[0]
        if not session_id:
            return 400, {"error": "session_id requis"}
        filters = {}
        if query.get("statut_attribution", [None])[0]:
            filters["statut_attribution"] = query["statut_attribution"][0]
        if query.get("statut_paiement", [None])[0]:
            filters["statut_paiement"] = query["statut_paiement"][0]
        try:
            claims = LiveManager.get_claims(int(session_id), filters=filters)
            return 200, claims
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 10. METTRE À JOUR LE STATUT D'UNE CLAIM (Admin)
    # POST /api/live/admin/claims/update
    # Body: { claim_id, statut_paiement?, statut_attribution?, statut_commande? }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/admin/claims/update":
        claim_id = data.get("claim_id")
        if not claim_id:
            return 400, {"error": "claim_id requis"}
        try:
            result = LiveManager.update_claim_status(int(claim_id), data)
            return 200, result
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 11. ENCAISSER UNE CLAIM DANS KŌDO POS (Admin - 1-clic)
    # POST /api/live/admin/claims/checkout
    # Body: { claim_id, paymentMethod?, cashierName? }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/admin/claims/checkout":
        claim_id = data.get("claim_id")
        if not claim_id:
            return 400, {"error": "claim_id requis"}
        try:
            result = LiveManager.checkout_claim(int(claim_id), data)
            if result.get("success"):
                return 200, result
            return 400, result
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 12. LISTE DES ACHETEURS (Admin)
    # GET /api/live/admin/buyers?session_id=X
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/live/admin/buyers":
        session_id = query.get("session_id", [None])[0]
        try:
            buyers = LiveManager.get_buyers(
                session_id=int(session_id) if session_id else None
            )
            return 200, buyers
        except Exception as e:
            return 500, {"error": str(e)}

    # -------------------------------------------------------------------------
    # 13. GÉNÉRER UN MESSAGE RÉCAP (Admin)
    # POST /api/live/admin/message
    # Body: { buyer_id, session_id }
    # -------------------------------------------------------------------------
    if method == "POST" and path == "/api/live/admin/message":
        buyer_id = data.get("buyer_id")
        session_id = data.get("session_id")
        if not buyer_id or not session_id:
            return 400, {"error": "buyer_id et session_id requis"}
        try:
            message = LiveManager.generate_summary_message(int(buyer_id), int(session_id))
            return 200, {"message": message}
        except Exception as e:
            return 500, {"error": str(e)}

    return None
