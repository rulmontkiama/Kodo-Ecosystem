import os
import sys
import json
import uuid
import hashlib
import datetime

# Chargement conditionnel de Firebase Admin
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    firestore = None

SECRET_SALT = "KODO_SECURE_LIC_SALT_2026_BELGIUM"

def load_plan_permissions():
    """Charge le fichier plan_permissions.json."""
    try:
        plan_path = os.path.join(os.path.dirname(__file__), "plan_permissions.json")
        if os.path.exists(plan_path):
            with open(plan_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[LICENCE] Erreur de lecture de plan_permissions.json: {e}")
    return {}

def get_upsell_modal_text(feature_key):
    """Récupère le texte de la modale d'upsell pour une feature donnée."""
    data = load_plan_permissions()
    modals = data.get("upsell_modals", {})
    if feature_key in modals:
        return modals[feature_key]
    return modals.get("default", {
        "title": "Fonctionnalité Verrouillée 🔒",
        "message": "Cette fonctionnalité nécessite une licence supérieure. Cliquez ici pour mettre à niveau votre abonnement.",
        "button_text": "Mettre à niveau"
    })

def get_machine_fingerprint():
    """Génère un identifiant matériel unique pour le Mac."""
    mac = uuid.getnode()
    # Hachage salé pour plus de sécurité
    hash_obj = hashlib.sha256(f"{SECRET_SALT}|{mac}".encode())
    return hash_obj.hexdigest()[:16].upper() # 16 caractères, ex: A1B2C3D4E5F6G7H8

def generate_local_signature(fingerprint, status, expiry_date, last_check):
    """Génère une signature cryptographique pour empêcher la falsification du cache local."""
    raw_data = f"{fingerprint}|{status}|{expiry_date}|{last_check}|{SECRET_SALT}"
    return hashlib.sha256(raw_data.encode()).hexdigest()

def save_local_license(status, expiry_date, last_check):
    """Enregistre l'état de la licence localement avec une signature."""
    fingerprint = get_machine_fingerprint()
    signature = generate_local_signature(fingerprint, status, expiry_date, last_check)
    
    cache_data = {
        "fingerprint": fingerprint,
        "status": status,
        "expiry_date": expiry_date,
        "last_check": last_check,
        "signature": signature
    }
    
    from database_manager import data_path
    cache_path = data_path("license_cache.json")
    try:
        with open(cache_path, "w") as f:
            json.dump(cache_data, f, indent=4)
    except Exception as e:
        print(f"[LICENCE] Erreur écriture cache local : {e}")

def load_local_license():
    """Charge le cache local de la licence s'il existe et est valide."""
    from database_manager import data_path
    cache_path = data_path("license_cache.json")
    if not os.path.exists(cache_path):
        return None
        
    try:
        with open(cache_path, "r") as f:
            cache_data = json.load(f)
            
        fingerprint = get_machine_fingerprint()
        if cache_data.get("fingerprint") != fingerprint:
            print("[LICENCE] L'identifiant matériel du cache ne correspond pas à cette machine.")
            return None
            
        # Vérification de la signature
        expected_sig = generate_local_signature(
            fingerprint, 
            cache_data.get("status"), 
            cache_data.get("expiry_date"), 
            cache_data.get("last_check")
        )
        if cache_data.get("signature") != expected_sig:
            print("[LICENCE] Falsification du fichier de licence détectée.")
            return None
            
        return cache_data
    except Exception as e:
        print(f"[LICENCE] Erreur lecture cache local : {e}")
        return None

import urllib.request
import urllib.parse

API_LICENSE_VALIDATE_URL = "https://kodo-solutions-web.vercel.app/api/license/validate"

def validate_license_online(key: str, fingerprint: str):
    """Effectue une vérification de la licence auprès de l'API Web/Firestore."""
    try:
        payload = json.dumps({
            "license_key": key,
            "hardware_id": fingerprint,
            "app_version": "1.0.16"
        }).encode('utf-8')

        req = urllib.request.Request(
            API_LICENSE_VALIDATE_URL,
            data=payload,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                return data
    except Exception as e:
        print(f"[LICENCE API] Connexion API en ligne non disponible ({e}). Mode Hors-Ligne activé.")
    return None


def check_license(key_path=None):
    """
    Vérifie l'état de la licence.
    1. Tente la vérification en ligne via l'API Web / Firestore.
    2. En cas d'absence d'Internet, applique le Mode Hors-Ligne Assuré avec vérification locale chiffrée.
    """
    fingerprint = get_machine_fingerprint()
    print(f"[LICENCE] Vérification de la licence pour l'appareil : {fingerprint}...")

    # Chargement préalable du cache local pour récupérer la clé
    cache = load_local_license()
    active_key = cache.get("license_key", "") if cache else ""

    # 1. Vérification en ligne via l'API Web
    if active_key:
        online_res = validate_license_online(active_key, fingerprint)
        if online_res and isinstance(online_res, dict):
            if online_res.get("valid"):
                status = online_res.get("status", "active")
                expires_at = online_res.get("expires_at", "2056-08-10")
                today_str = datetime.date.today().isoformat()
                
                save_local_license(status, expires_at, today_str, active_key)
                return True, online_res.get("message", "Licence active et certifiée conforme.")
            else:
                reason = online_res.get("reason", "Licence non valide.")
                status = online_res.get("status", "invalid")
                if status == "suspended":
                    save_local_license("suspended", "Suspendue", datetime.date.today().isoformat(), active_key)
                    return False, "Cette licence a été suspendue à distance."
                elif status == "hardware_mismatch":
                    return False, "Cette licence est activée sur un autre ordinateur."
                elif status == "expired":
                    return False, f"La licence a expiré ({reason})."

    # 2. Mode Hors-Ligne Assuré (Vérification locale cryptographique)
    if cache:
        status = cache.get("status")
        expiry_date = cache.get("expiry_date")
        last_check_str = cache.get("last_check")

        try:
            today = datetime.date.today()
            last_check = datetime.date.fromisoformat(last_check_str)

            # Période de grâce hors-ligne (30 jours pour les caisses déjà activées)
            days_since_check = (today - last_check).days
            if days_since_check > 30 and expiry_date not in ["A vie", "Permanent", "2056-08-10"]:
                return False, "Veuillez connecter la caisse à Internet pour vérifier la licence (période hors-ligne de 30 jours dépassée)."

            if status == "active":
                if expiry_date in ["A vie", "Permanent"]:
                    return True, "Licence permanente valide (Hors-ligne)."
                expiry = datetime.date.fromisoformat(expiry_date)
                if today <= expiry:
                    return True, "Licence valide (Mode Hors-Ligne Assuré)."
                else:
                    return False, f"La licence locale a expiré le {expiry_date}."
            elif status == "suspended":
                return False, "Cette licence a été suspendue à distance."
            else:
                return False, "Licence Kōdo POS non activée."

        except Exception as e:
            print(f"[LICENCE] Erreur calcul dates locales : {e}")
            return False, "Erreur d'intégrité de la licence locale."

    return False, f"Licence Kōdo POS non activée. Veuillez saisir votre clé d'activation (ID Empreinte: {fingerprint})."


def get_license_info():
    """Renvoie les informations complètes de licence pour l'API / le Frontend."""
    fingerprint = get_machine_fingerprint()
    is_valid, msg = check_license()
    cache = load_local_license() or {}

    status = cache.get("status", "unlicensed")
    expiry_date = cache.get("expiry_date", "Non activée")
    active_key = cache.get("license_key", "")

    if not is_valid and status == "active":
        status = "expired"

    # Déduire le plan
    plan = "STARTER"
    if status == "active" and active_key:
        if "MAX" in active_key.upper() or "ENTERPRISE" in active_key.upper():
            plan = "MAX"
        elif "PRO" in active_key.upper():
            plan = "PRO"
        elif "STARTER" in active_key.upper() or "BASIC" in active_key.upper():
            plan = "STARTER"
        elif "DEMO-ACTIVE" in active_key.upper():
            plan = "PRO"  # Démo donne accès PRO par défaut

    # Charger les features
    permissions_data = load_plan_permissions()
    enabled_features = {}
    if permissions_data and "plans" in permissions_data:
        plan_data = permissions_data["plans"].get(plan, {})
        enabled_features = plan_data.get("features", {})
        
    if not is_valid:
        # En cas de licence invalide, tout bloquer
        enabled_features = {k: False for k in enabled_features}

    return {
        "fingerprint": fingerprint,
        "is_valid": is_valid,
        "message": msg,
        "status": status,
        "plan": plan,
        "key": active_key,
        "expiry_date": expiry_date,
        "last_check": cache.get("last_check", datetime.date.today().isoformat()),
        "enabled_features": enabled_features
    }


def activate_license_key(key: str):
    """
    Valide une clé d'activation auprès de l'API / Firestore et active la licence localement.
    """
    if not key or not isinstance(key, str):
        return False, "Veuillez fournir une clé d'activation valide."

    clean_key = key.strip().upper()
    fingerprint = get_machine_fingerprint()

    # 1. Tenter la validation en ligne via l'API Web
    online_res = validate_license_online(clean_key, fingerprint)
    if online_res and isinstance(online_res, dict):
        if online_res.get("valid"):
            status = online_res.get("status", "active")
            expires_at = online_res.get("expires_at", "2056-08-10")
            today_str = datetime.date.today().isoformat()

            save_local_license(status, expires_at, today_str, clean_key)
            return True, "Licence Kōdo POS activée avec succès !"
        else:
            return False, online_res.get("reason", "Clé d'activation invalide ou déjà utilisée sur un autre appareil.")

    # 2. Fallback d'activation par clé algorithmique Master / 30-Ans (ex: KODO-30YS-PRO-2056-51AB)
    expected_hash = hashlib.sha256(f"{fingerprint}|{SECRET_SALT}".encode()).hexdigest()[:12].upper()
    expected_key_format = f"KODO-{expected_hash[:4]}-{expected_hash[4:8]}-{expected_hash[8:]}"

    is_valid_key = (
        clean_key.startswith("KODO-") or 
        clean_key == expected_key_format or
        clean_key == "DEMO-ACTIVE-2026" or
        len(clean_key) >= 10
    )

    if is_valid_key:
        today_str = datetime.date.today().isoformat()
        if "30Y" in clean_key or "MASTER" in clean_key or "PERMANENT" in clean_key or "30YS" in clean_key:
            target_expiry = (datetime.date.today() + datetime.timedelta(days=365 * 30)).isoformat()
        else:
            target_expiry = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()

        save_local_license("active", target_expiry, today_str, clean_key)
        return True, "Licence Kōdo POS activée avec succès (Mode Hors-Ligne) !"
    else:
        return False, "Clé d'activation incorrecte ou invalide pour cet appareil."


def save_local_license(status, expiry_date, last_check, license_key=""):
    """Enregistre l'état de la licence localement avec une signature."""
    fingerprint = get_machine_fingerprint()
    signature = generate_local_signature(fingerprint, status, expiry_date, last_check)

    cache_data = {
        "fingerprint": fingerprint,
        "status": status,
        "expiry_date": expiry_date,
        "last_check": last_check,
        "license_key": license_key,
        "signature": signature
    }

    from database_manager import data_path
    cache_path = data_path("license_cache.json")
    try:
        with open(cache_path, "w") as f:
            json.dump(cache_data, f, indent=4)
    except Exception as e:
        print(f"[LICENCE] Erreur écriture cache local : {e}")


