# -*- coding: utf-8 -*-
"""Tests unitaires du module de licence blindé (kodo_core.services.license)."""

import datetime
import hmac
import hashlib
import json
import os

import pytest

from kodo_core.services import license as license_module


@pytest.fixture
def temp_cache_paths(tmp_path, monkeypatch):
    """Redirige le stockage double (primaire + sauvegarde) vers des fichiers temporaires isolés."""
    primary = tmp_path / "primary" / "license_cache.json"
    backup = tmp_path / "backup" / "license.lic"

    monkeypatch.setattr(license_module, "_get_primary_cache_path", lambda: str(primary))
    monkeypatch.setattr(license_module, "_get_backup_cache_path", lambda: str(backup))

    return primary, backup


@pytest.fixture
def fixed_fingerprint(monkeypatch):
    """Fige l'empreinte matérielle pour rendre les tests déterministes."""
    monkeypatch.setattr(license_module, "get_machine_fingerprint", lambda: "FIXEDHWID1234567")
    return "FIXEDHWID1234567"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Empêche tout appel réseau réel pendant les tests unitaires."""
    monkeypatch.setattr(license_module, "validate_license_online", lambda key, fingerprint: None)


# ---------------------------------------------------------------------------
# HWID stable
# ---------------------------------------------------------------------------

def test_hwid_is_deterministic_for_same_hardware_id(monkeypatch):
    monkeypatch.setattr(license_module, "_collect_hardware_id", lambda: "SERIAL:ABC123")
    fp1 = license_module.get_machine_fingerprint()
    fp2 = license_module.get_machine_fingerprint()
    assert fp1 == fp2
    assert len(fp1) == 16


def test_hwid_unaffected_by_real_mac_change_when_serial_available(monkeypatch):
    """Le numéro de série prime : une variation de la MAC réseau ne doit rien changer."""
    monkeypatch.setattr(license_module, "_get_macos_hardware_id", lambda: "SERIAL:C02XG2JGJGH7")
    monkeypatch.setattr(license_module, "platform", license_module.platform)
    monkeypatch.setattr(license_module.platform, "system", lambda: "Darwin")

    monkeypatch.setattr(license_module, "_real_mac_address", lambda: "AA:BB:CC:DD:EE:FF")
    fp_wifi_on = license_module.get_machine_fingerprint()

    monkeypatch.setattr(license_module, "_real_mac_address", lambda: "")
    fp_wifi_off = license_module.get_machine_fingerprint()

    assert fp_wifi_on == fp_wifi_off


def test_macos_hardware_id_prefers_ioplatform_serial(monkeypatch):
    ioreg_output = (
        '    | | "IOPlatformUUID" = "11111111-2222-3333-4444-555555555555"\n'
        '    | | "IOPlatformSerialNumber" = "C02XG2JGJGH7"\n'
    )
    monkeypatch.setattr(license_module, "_run_command", lambda args, timeout=3.0: ioreg_output if args[0] == "ioreg" else "")
    hwid = license_module._get_macos_hardware_id()
    assert hwid == "SERIAL:C02XG2JGJGH7"


def test_macos_hardware_id_falls_back_to_uuid_then_mac(monkeypatch):
    def fake_run(args, timeout=3.0):
        if args[0] == "ioreg":
            return ""
        if args[0] == "scutil":
            return ""
        return ""

    monkeypatch.setattr(license_module, "_run_command", fake_run)
    monkeypatch.setattr(license_module, "_real_mac_address", lambda: "001122334455")
    hwid = license_module._get_macos_hardware_id()
    assert hwid == "MAC:001122334455"


def test_windows_hardware_id_uses_bios_serial(monkeypatch):
    def fake_run(args, timeout=3.0):
        if args[0] == "powershell":
            return "WIN-SERIAL-999"
        return ""

    monkeypatch.setattr(license_module, "_run_command", fake_run)
    hwid = license_module._get_windows_hardware_id()
    assert hwid == "SERIAL:WIN-SERIAL-999"


# ---------------------------------------------------------------------------
# Falsification & signature HMAC
# ---------------------------------------------------------------------------

def test_signature_is_hmac_sha256_and_changes_with_salt(fixed_fingerprint):
    sig = license_module.generate_local_signature(fixed_fingerprint, "active", "2030-01-01", "2026-01-01")
    expected = hmac.new(
        license_module.SECRET_SALT.encode("utf-8"),
        f"{fixed_fingerprint}|active|2030-01-01|2026-01-01".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert sig == expected


def test_tampering_with_cache_file_is_detected_and_rejected(temp_cache_paths, fixed_fingerprint):
    primary, backup = temp_cache_paths
    license_module.save_local_license("active", "2030-01-01", "2026-01-01", "SOME-KEY")

    with open(primary, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["status"] = "active"
    data["expiry_date"] = "2099-01-01"  # falsification de la date d'expiration
    with open(primary, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # La sauvegarde de secours est intacte : elle doit permettre l'auto-réparation.
    loaded = license_module.load_local_license()
    assert loaded is not None
    assert loaded["expiry_date"] == "2030-01-01"

    with open(primary, "r", encoding="utf-8") as f:
        restored = json.load(f)
    assert restored["expiry_date"] == "2030-01-01"


def test_tampering_with_both_copies_is_rejected(temp_cache_paths, fixed_fingerprint):
    primary, backup = temp_cache_paths
    license_module.save_local_license("active", "2030-01-01", "2026-01-01", "SOME-KEY")

    for path in (primary, backup):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["status"] = "active"
        data["expiry_date"] = "2099-01-01"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    assert license_module.load_local_license() is None


def test_cache_from_different_hardware_is_rejected(temp_cache_paths, fixed_fingerprint, monkeypatch):
    license_module.save_local_license("active", "2030-01-01", "2026-01-01", "SOME-KEY")
    # Une machine différente ne doit jamais pouvoir réutiliser ce cache.
    monkeypatch.setattr(license_module, "get_machine_fingerprint", lambda: "OTHERHWID0000000")
    assert license_module.load_local_license() is None


# ---------------------------------------------------------------------------
# Double stockage résilient
# ---------------------------------------------------------------------------

def test_save_writes_to_both_primary_and_backup_locations(temp_cache_paths, fixed_fingerprint):
    primary, backup = temp_cache_paths
    license_module.save_local_license("active", "2030-01-01", "2026-01-01", "SOME-KEY")
    assert primary.exists()
    assert backup.exists()


def test_load_recovers_from_backup_when_primary_missing(temp_cache_paths, fixed_fingerprint):
    primary, backup = temp_cache_paths
    license_module.save_local_license("active", "2030-01-01", "2026-01-01", "SOME-KEY")
    os.remove(primary)

    loaded = license_module.load_local_license()
    assert loaded is not None
    assert loaded["status"] == "active"
    # Auto-réparation : le fichier primaire doit être régénéré.
    assert primary.exists()


# ---------------------------------------------------------------------------
# Activation locale (fallback hors-ligne strict) & en ligne
# ---------------------------------------------------------------------------

def test_weak_validation_no_longer_accepted(temp_cache_paths, fixed_fingerprint):
    ok, msg = license_module.activate_license_key("KODO-ANYTHING-1234567890")
    assert ok is False


def test_arbitrary_long_key_is_rejected(temp_cache_paths, fixed_fingerprint):
    ok, msg = license_module.activate_license_key("THIS-IS-A-LONG-RANDOM-STRING")
    assert ok is False


def test_exact_master_key_for_this_hardware_is_accepted(temp_cache_paths, fixed_fingerprint):
    expected_key = license_module._expected_master_key(fixed_fingerprint)
    ok, msg = license_module.activate_license_key(expected_key)
    assert ok is True

    cache = license_module.load_local_license()
    assert cache["status"] == "active"
    assert cache["license_key"] == expected_key


def test_demo_key_is_accepted(temp_cache_paths, fixed_fingerprint):
    ok, msg = license_module.activate_license_key("demo-active-2026")
    assert ok is True


def test_master_key_from_other_hardware_is_rejected(temp_cache_paths, fixed_fingerprint):
    other_key = license_module._expected_master_key("OTHERHWID0000000")
    ok, msg = license_module.activate_license_key(other_key)
    assert ok is False


def test_online_activation_succeeds_and_persists_cache(temp_cache_paths, fixed_fingerprint, monkeypatch):
    monkeypatch.setattr(
        license_module,
        "validate_license_online",
        lambda key, fingerprint: {"valid": True, "status": "active", "expires_at": "2027-01-01"},
    )
    ok, msg = license_module.activate_license_key("CLOUD-KEY-XYZ")
    assert ok is True

    cache = license_module.load_local_license()
    assert cache["status"] == "active"
    assert cache["expiry_date"] == "2027-01-01"
    assert cache["license_key"] == "CLOUD-KEY-XYZ"


def test_online_check_license_revalidates_and_refreshes_cache(temp_cache_paths, fixed_fingerprint, monkeypatch):
    license_module.save_local_license("active", "2027-01-01", "2020-01-01", "CLOUD-KEY-XYZ")

    monkeypatch.setattr(
        license_module,
        "validate_license_online",
        lambda key, fingerprint: {"valid": True, "status": "active", "expires_at": "2028-06-01", "message": "OK"},
    )

    is_valid, msg = license_module.check_license()
    assert is_valid is True

    cache = license_module.load_local_license()
    assert cache["expiry_date"] == "2028-06-01"
    assert cache["last_check"] == datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# Mode hors-ligne assuré (30 jours) & licences permanentes
# ---------------------------------------------------------------------------

def test_offline_grace_period_valid_within_30_days(temp_cache_paths, fixed_fingerprint):
    last_check = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    expiry = (datetime.date.today() + datetime.timedelta(days=100)).isoformat()
    license_module.save_local_license("active", expiry, last_check, "SOME-KEY")

    is_valid, msg = license_module.check_license()
    assert is_valid is True


def test_offline_grace_period_expired_beyond_30_days(temp_cache_paths, fixed_fingerprint):
    last_check = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
    expiry = (datetime.date.today() + datetime.timedelta(days=100)).isoformat()
    license_module.save_local_license("active", expiry, last_check, "SOME-KEY")

    is_valid, msg = license_module.check_license()
    assert is_valid is False


def test_permanent_license_bypasses_30_day_offline_limit(temp_cache_paths, fixed_fingerprint):
    last_check = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    license_module.save_local_license("active", "Permanent", last_check, "MASTER-PERMANENT-KEY")

    is_valid, msg = license_module.check_license()
    assert is_valid is True
