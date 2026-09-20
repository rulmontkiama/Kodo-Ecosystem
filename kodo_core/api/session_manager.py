# -*- coding: utf-8 -*-
"""
kodo_core.api.session_manager — Gestionnaire de sessions et jetons d'authentification légers.
Signe et valide les jetons de session via HMAC-SHA256 pour sécuriser les appels API REST.
"""

import os
import time
import base64
import hmac
import hashlib
from typing import Dict, Any, Optional

from kodo_core.config import ShopConfig


def _get_session_secret() -> bytes:
    """Récupère ou génère la clé secrète pour la signature des jetons de session."""
    secret = ShopConfig.get_secret_key()
    salt = ShopConfig.get_salt()
    return hashlib.sha256(f"{secret}|{salt}|SESSION_SIGNING_KEY".encode('utf-8')).digest()


def create_session_token(user_id: str, user_name: str, role: str, ttl_seconds: int = 28800) -> str:
    """
    Génère un jeton de session signé HMAC-SHA256 valide pendant ttl_seconds (8h par défaut).
    Format : base64(payload_utf8).signature_hex
    """
    exp_timestamp = int(time.time()) + ttl_seconds
    # Nettoyage des délimiteurs
    clean_id = str(user_id).replace('|', '_')
    clean_name = str(user_name).replace('|', '_')
    clean_role = str(role).replace('|', '_')
    payload = f"{clean_id}|{clean_name}|{clean_role}|{exp_timestamp}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii')
    
    secret = _get_session_secret()
    signature = hmac.new(secret, payload_b64.encode('ascii'), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Vérifie la signature et l'expiration d'un jeton de session.
    Retourne le dictionnaire utilisateur si valide, None sinon.
    """
    if not token or not isinstance(token, str) or '.' not in token:
        return None
    
    parts = token.split('.', 1)
    if len(parts) != 2:
        return None
    
    payload_b64, signature = parts
    secret = _get_session_secret()
    expected_sig = hmac.new(secret, payload_b64.encode('ascii'), hashlib.sha256).hexdigest()
    
    if not hmac.compare_digest(signature, expected_sig):
        return None
    
    try:
        payload = base64.urlsafe_b64decode(payload_b64.encode('ascii')).decode('utf-8')
        user_id, user_name, role, exp_str = payload.split('|')
        exp_timestamp = int(exp_str)
        if time.time() > exp_timestamp:
            return None  # Expiré
        
        return {
            "id": user_id,
            "name": user_name,
            "role": role,
            "exp": exp_timestamp
        }
    except Exception:
        return None


def extract_token(headers: Optional[Dict[str, str]]) -> Optional[str]:
    """Extrait le jeton de session depuis les en-têtes HTTP (Authorization ou X-Session-Token)."""
    if not headers or not isinstance(headers, dict):
        return None
    
    # Recherche insensible à la casse
    headers_lower = {k.lower(): v for k, v in headers.items()}
    
    auth_header = headers_lower.get('authorization')
    if auth_header and auth_header.startswith('Bearer '):
        return auth_header[7:].strip()
    
    return headers_lower.get('x-session-token') or headers_lower.get('x-auth-token')


def get_current_user(headers: Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Extrait et valide l'utilisateur courant depuis les en-têtes de la requête."""
    token = extract_token(headers)
    if not token:
        return None
    return verify_session_token(token)
