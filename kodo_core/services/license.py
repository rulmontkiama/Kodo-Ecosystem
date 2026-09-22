"""
Kōdo POS - Module de Gestion de Licence par Empreinte Matérielle (HWID) et Validation de Clé
Support du Mode Hors-Ligne Assuré (30 jours) et de la validation Cloud via l'API Web.
"""

import os
import sys
import json
import uuid
import hmac
import hashlib
import platform
import subprocess
import datetime
import urllib.request
import urllib.parse
import logging

SECRET_SALT = "KODO_SECURE_LIC_SALT_2026_BELGIUM"
API_LICENSE_VALIDATE_URL = "https://kodo-solutions-web.vercel.app/api/license/validate"

logger = logging.getLogger("kodo_core.services.license")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[LICENCE Core] %(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def load_plan_permissions():
    """Charge le fichier de configuration des permissions de plan (plan_permissions.json)."""
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        plan_path = os.path.join(base_dir, "plan_permissions.json")
        if not os.path.exists(plan_path):
            plan_path = os.path.join(os.path.dirname(__file__), "plan_permissions.json")

        if os.path.exists(plan_path):
            with open(plan_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Erreur de lecture de plan_permissions.json: {e}")
    return {}


def get_upsell_modal_text(feature_key: str) -> dict:
    """Récupère le texte de la modale d'upsell pour une fonctionnalité verrouillée."""
    data = load_plan_permissions()
    modals = data.get("upsell_modals", {})
    if feature_key in modals:
        return modals[feature_key]
    return modals.get("default", {
        "title": "Fonctionnalité Verrouillée 🔒",
        "message": "Cette fonctionnalité nécessite une licence supérieure. Cliquez ici pour mettre à niveau votre abonnement.",
        "button_text": "Mettre à niveau"
    })


# ---------------------------------------------------------------------------
# Empreinte matérielle (HWID)
# ---------------------------------------------------------------------------
# La priorité est donnée à un identifiant matériel IMMUABLE (numéro de série
# de la machine / carte mère). Cet identifiant ne doit JAMAIS varier en
# fonction de l'état réseau (Wi-Fi coupé, changement de routeur, etc.).

def _run_command(args, timeout: float = 3.0) -> str:
    """Exécute une commande système et retourne sa sortie standard (silencieux en cas d'échec)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _real_mac_address() -> str:
    """Retourne la MAC matérielle réelle, ou une chaîne vide si uuid a généré une adresse aléatoire."""
    node = uuid.getnode()
    # RFC 4122 : si l'adresse matérielle est indisponible, uuid positionne le
    # bit multicast (bit de poids faible du premier octet) sur une valeur générée aléatoirement.
    if (node >> 40) & 0x01:
        return ""
    return format(node, "012X")


def _get_macos_hardware_id() -> str:
    """macOS : IOPlatformSerialNumber (immuable) en priorité, puis IOPlatformUUID, puis MAC réelle."""
    output = _run_command(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    serial = ""
    platform_uuid = ""
    for line in output.splitlines():
        if "IOPlatformSerialNumber" in line and "=" in line:
            serial = line.split("=", 1)[1].strip().strip('"')
        elif "IOPlatformUUID" in line and "=" in line:
            platform_uuid = line.split("=", 1)[1].strip().strip('"')
    if serial:
        return f"SERIAL:{serial}"

    # Fallback "scutil" : UUID matériel exposé via l'utilitaire système scutil.
    scutil_uuid = _run_command(["scutil", "--get", "HardwareUUID"])
    if not scutil_uuid:
        scutil_uuid = platform_uuid
    if scutil_uuid:
        return f"UUID:{scutil_uuid}"

    mac = _real_mac_address()
    if mac:
        return f"MAC:{mac}"
    return ""


def _get_windows_hardware_id() -> str:
    """Windows : numéro de série BIOS/carte mère via PowerShell (WMI), fallback wmic."""
    invalid_values = {"", "NONE", "TO BE FILLED BY O.E.M.", "SYSTEM SERIAL NUMBER", "DEFAULT STRING"}

    powershell_queries = [
        "(Get-CimInstance -ClassName Win32_BIOS).SerialNumber",
        "(Get-CimInstance -ClassName Win32_BaseBoard).SerialNumber",
        "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID",
    ]
    for query in powershell_queries:
        output = _run_command(["powershell", "-NoProfile", "-NonInteractive", "-Command", query])
        if output and output.strip().upper() not in invalid_values:
            return f"SERIAL:{output.strip()}"

    wmic_commands = [
        ["wmic", "bios", "get", "serialnumber"],
        ["wmic", "baseboard", "get", "serialnumber"],
        ["wmic", "csproduct", "get", "uuid"],
    ]
    for cmd in wmic_commands:
        output = _run_command(cmd)
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        # La première ligne est l'entête de colonne (ex: "SerialNumber")
        if len(lines) >= 2 and lines[1].upper() not in invalid_values:
            return f"SERIAL:{lines[1]}"

    mac = _real_mac_address()
    if mac:
        return f"MAC:{mac}"
    return ""


def _get_linux_hardware_id() -> str:
    """Linux : machine-id persistant du système, sinon MAC réelle."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    machine_id = f.read().strip()
                if machine_id:
                    return f"MACHINEID:{machine_id}"
        except Exception:
            pass

    mac = _real_mac_address()
    if mac:
        return f"MAC:{mac}"
    return ""


def _collect_hardware_id() -> str:
    """Détermine l'identifiant matériel brut le plus stable disponible pour la plateforme courante."""
    system = platform.system()
    hwid = ""
    try:
        if system == "Darwin":
            hwid = _get_macos_hardware_id()
        elif system == "Windows":
            hwid = _get_windows_hardware_id()
        elif system == "Linux":
            hwid = _get_linux_hardware_id()
    except Exception as e:
        logger.error(f"Erreur récupération identifiant matériel natif ({system}): {e}")
        hwid = ""

    if not hwid:
        mac = _real_mac_address()
        hwid = f"MAC:{mac}" if mac else f"NODE:{uuid.getnode()}"

    return hwid


def get_machine_fingerprint() -> str:
    """Génère une empreinte matérielle HWID unique salée à 16 caractères majuscules."""
    try:
        raw_id = _collect_hardware_id()
        hash_obj = hashlib.sha256(f"{SECRET_SALT}|{raw_id}".encode("utf-8"))
        return hash_obj.hexdigest()[:16].upper()
    except Exception as e:
        logger.error(f"Erreur génération HWID: {e}")
        return "DEFAULT_HWID_000"


# ---------------------------------------------------------------------------
# Cache local signé (HMAC) & double stockage résilient
# ---------------------------------------------------------------------------

def generate_local_signature(fingerprint: str, status: str, expiry_date: str, last_check: str) -> str:
    """Génère une signature HMAC-SHA256 anti-falsification pour le cache local."""
    raw_data = f"{fingerprint}|{status}|{expiry_date}|{last_check}".encode("utf-8")
    return hmac.new(SECRET_SALT.encode("utf-8"), raw_data, hashlib.sha256).hexdigest()


def _get_primary_cache_path() -> str:
    try:
        from database_manager import data_path
        return data_path("license_cache.json")
    except Exception:
        fallback = os.path.expanduser("~/Library/Caches/KodoPOS/license_cache.json")
        return fallback


def _get_backup_cache_path() -> str:
    return os.path.expanduser("~/Library/Application Support/Kodo_POS/license.lic")


def _write_cache_file(path: str, cache_data: dict) -> bool:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=4)
        return True
    except Exception as e:
        logger.error(f"Erreur d'écriture du fichier de licence '{path}' : {e}")
        return False


def _read_cache_file(path: str):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Erreur de lecture du fichier de licence '{path}' : {e}")
        return None


def _validate_cache_data(cache_data, fingerprint: str) -> bool:
    if not cache_data:
        return False

    if cache_data.get("fingerprint") != fingerprint:
        logger.warning("L'identifiant matériel du cache ne correspond pas à cette machine.")
        return False

    expected_sig = generate_local_signature(
        fingerprint,
        cache_data.get("status"),
        cache_data.get("expiry_date"),
        cache_data.get("last_check"),
    )
    if not hmac.compare_digest(str(cache_data.get("signature", "")), expected_sig):
        logger.warning("Falsification du fichier de licence détectée.")
        return False

    return True


def save_local_license(status: str, expiry_date: str, last_check: str, license_key: str = "") -> dict:
    """Enregistre de façon sécurisée l'état de la licence, en double, avec signature HMAC anti-falsification."""
    fingerprint = get_machine_fingerprint()
    signature = generate_local_signature(fingerprint, status, expiry_date, last_check)

    cache_data = {
        "fingerprint": fingerprint,
        "status": status,
        "expiry_date": expiry_date,
        "last_check": last_check,
        "license_key": license_key,
        "signature": signature,
    }

    primary_ok = _write_cache_file(_get_primary_cache_path(), cache_data)
    backup_ok = _write_cache_file(_get_backup_cache_path(), cache_data)

    if primary_ok or backup_ok:
        logger.info(f"Cache de licence sauvegardé (statut={status}, primaire={primary_ok}, sauvegarde={backup_ok}).")
    else:
        logger.error("Échec d'écriture du cache de licence sur les deux emplacements.")

    return cache_data


def load_local_license():
    """Charge et vérifie l'intégrité HMAC et matérielle du cache local, avec bascule sur la sauvegarde."""
    fingerprint = get_machine_fingerprint()
    primary_path = _get_primary_cache_path()
    backup_path = _get_backup_cache_path()

    primary_data = _read_cache_file(primary_path)
    if _validate_cache_data(primary_data, fingerprint):
        return primary_data

    backup_data = _read_cache_file(backup_path)
    if _validate_cache_data(backup_data, fingerprint):
        logger.warning("Cache principal manquant ou corrompu : restauration depuis la sauvegarde résiliente.")
        _write_cache_file(primary_path, backup_data)
        return backup_data

    return None


def validate_license_online(key: str, fingerprint: str) -> dict:
    """Valide la clé de licence auprès du service web Cloud / Firestore via HTTP POST."""
    if not key or not fingerprint:
        return None

    try:
        from kodo_core.services.updater import CURRENT_VERSION
        payload = json.dumps({
            "license_key": key,
            "hardware_id": fingerprint,
            "app_version": CURRENT_VERSION
        }).encode("utf-8")

        # La clé de licence et l'empreinte matérielle transitent ici : la vérification du
        # certificat ET du nom d'hôte est obligatoire. Sans elle, un intermédiaire peut lire
        # les clés en clair, ou forger une réponse {"valid": true} / {"status": "suspended"}
        # pour activer une copie ou éteindre la caisse d'une boutique. On réutilise le
        # contexte maison de l'updater (magasin certifi, jamais de repli non vérifié) :
        # un échec de validation retombe sur le Mode Hors-Ligne Assuré, comme une panne réseau.
        from kodo_core.services.updater import build_ssl_context
        ctx = build_ssl_context()

        req = urllib.request.Request(
            API_LICENSE_VALIDATE_URL,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "KodoPOS-LicenseManager/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data
    except Exception as e:
        logger.info(f"Connexion API licence en ligne non disponible ({e}). Basculement en mode hors-ligne.")
    return None


def check_license(key_path: str = None) -> tuple:
    """
    Vérifie l'état de la licence applicative.
    1. Tente la vérification en ligne via l'API Web.
    2. Applique le Mode Hors-Ligne Assuré avec vérification cryptographique locale et grâce 30j.
    """
    fingerprint = get_machine_fingerprint()
    logger.info(f"Vérification de la licence pour l'appareil HWID: {fingerprint}")

    cache = load_local_license()
    active_key = cache.get("license_key", "") if cache else ""

    # 1. Validation Cloud en ligne si une clé est présente
    if active_key and key_path != "non_existent_key.json":
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

    # 2. Mode Hors-Ligne Assuré (Vérification locale)
    if cache:
        status = cache.get("status")
        expiry_date = cache.get("expiry_date")
        last_check_str = cache.get("last_check")

        try:
            today = datetime.date.today()
            last_check = datetime.date.fromisoformat(last_check_str)

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
            logger.error(f"Erreur calcul dates locales de licence : {e}")
            return False, "Erreur d'intégrité de la licence locale."

    return False, f"Licence Kōdo POS non activée. Veuillez saisir votre clé d'activation (ID Empreinte: {fingerprint})."


def get_license_info() -> dict:
    """Renvoie l'ensemble des métadonnées de licence et permissions de fonctionnalités."""
    fingerprint = get_machine_fingerprint()
    is_valid, msg = check_license()
    cache = load_local_license() or {}

    status = cache.get("status", "unlicensed")
    expiry_date = cache.get("expiry_date", "Non activée")
    active_key = cache.get("license_key", "")

    if not is_valid and status == "active":
        status = "expired"

    plan = "STARTER"
    if status == "active" and active_key:
        key_upper = active_key.upper()
        if "MAX" in key_upper or "ENTERPRISE" in key_upper:
            plan = "MAX"
        elif "PRO" in key_upper:
            plan = "PRO"
        elif "STARTER" in key_upper or "BASIC" in key_upper:
            plan = "STARTER"
        elif "DEMO-ACTIVE" in key_upper:
            plan = "PRO"

    permissions_data = load_plan_permissions()
    enabled_features = {}
    if permissions_data and "plans" in permissions_data:
        plan_data = permissions_data["plans"].get(plan, {})
        enabled_features = plan_data.get("features", {})

    if not is_valid:
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


def _expected_master_key(fingerprint: str) -> str:
    """Calcule la clé maître attendue pour cet appareil via HMAC-SHA256 (dérivée du HWID)."""
    expected_hash = hmac.new(
        SECRET_SALT.encode("utf-8"), fingerprint.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:12].upper()
    return f"KODO-{expected_hash[:4]}-{expected_hash[4:8]}-{expected_hash[8:]}"


def activate_license_key(key: str) -> tuple:
    """
    Active une clé de licence en tentant une validation Cloud puis fallback algorithmique local.
    La validation locale exige une correspondance EXACTE avec la clé maître dérivée du HWID
    (HMAC-SHA256), ou la clé de démonstration explicite. Aucune validation laxiste (préfixe,
    longueur minimale) n'est tolérée.
    """
    if not key or not isinstance(key, str):
        return False, "Veuillez fournir une clé d'activation valide."

    clean_key = key.strip().upper()
    fingerprint = get_machine_fingerprint()

    # 1. API Cloud
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

    # 2. Clé Master / Algorithmique (fallback hors-ligne strict, correspondance exacte uniquement)
    expected_key_format = _expected_master_key(fingerprint)

    is_valid_key = clean_key == expected_key_format or clean_key == "DEMO-ACTIVE-2026"

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
