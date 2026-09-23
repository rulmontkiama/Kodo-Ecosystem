"""
Service matériel de caisse : détection d'état imprimante thermique, génération
du flux binaire ESC/POS du ticket, et commande d'ouverture du tiroir-caisse.

Découplé de l'UI : toute interaction OS (subprocess CUPS / win32print) passe
par des points d'entrée injectables pour rester testable avec des flux simulés.
"""
import os
import sys
import subprocess
from decimal import Decimal
from typing import Optional, Sequence

# Référence UNIQUE d'arrondi monétaire du projet (voir son docstring) : le ticket imprimé
# doit afficher exactement le montant que la vente scelle en base.
from kodo_core.domain.sales.models import quantize_money

ESC = b'\x1b'
GS = b'\x1d'

ESC_INIT = ESC + b'@'
ESC_ALIGN_LEFT = ESC + b'a\x00'
ESC_ALIGN_CENTER = ESC + b'a\x01'

ESC_BOLD_ON = ESC + b'E\x01'
ESC_BOLD_OFF = ESC + b'E\x00'

GS_CUT_FUNCTION = GS + b'VB\x00'  # GS V 66 0 : coupure papier

# Impulsion électrique standard pour solénoïde tiroir-caisse RJ11 (pin 2)
ESC_DRAWER_PIN2 = ESC + b'p\x00\x19\xfa'  # ESC p 0 25 250

COL = 42  # Largeur standard ticket thermique 80mm (42 colonnes)


def env_cups() -> dict:
    """
    Environnement à passer à tout appel `lpstat`/`lp` dont on relit la sortie.

    CUPS traduit ses messages : sur un Mac en français `lpstat -p` répond
    « l'imprimante Kodo est inactive », où le code cherchait « idle » / « enabled ».
    Une imprimante parfaitement prête était donc rapportée « unknown » — donc
    indisponible à l'écran de caisse — pour la seule raison que le système n'était
    pas en anglais. On fige la langue des messages plutôt que d'énumérer les
    traductions : `lpstat` reste le même programme, seule sa langue est imposée.
    `LANGUAGE` est vidé car GNU gettext lui donne la priorité sur `LC_ALL`.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["LANGUAGE"] = ""
    return env


def check_printer_status(printer_name: str) -> tuple[bool, str]:
    """
    Teste l'état d'une imprimante thermique via CUPS (macOS/Linux) ou le
    spooler (Windows).

    Retourne (is_ready, status) avec status dans
    {"ready", "busy", "offline", "unknown"}.
    """
    if not printer_name:
        return False, "unknown"

    if sys.platform == "win32":
        return _check_printer_status_windows(printer_name)
    return _check_printer_status_cups(printer_name)


def _check_printer_status_cups(printer_name: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["lpstat", "-p", printer_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            env=env_cups(),
        )
    except Exception:
        return False, "unknown"

    if result.returncode != 0:
        return False, "offline"

    output = result.stdout.decode(errors="ignore").lower()

    if "disabled" in output:
        return False, "offline"
    if "now printing" in output or "processing" in output:
        return False, "busy"
    if "idle" in output or "enabled" in output:
        return True, "ready"
    return False, "unknown"


def _check_printer_status_windows(printer_name: str) -> tuple[bool, str]:
    try:
        import win32print
    except ImportError:
        return False, "unknown"

    try:
        handle = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(handle, 2)
        finally:
            win32print.ClosePrinter(handle)
    except Exception:
        return False, "offline"

    status = info.get("Status", 0)
    if status == 0:
        return True, "ready"
    if status & (win32print.PRINTER_STATUS_OFFLINE | win32print.PRINTER_STATUS_ERROR | win32print.PRINTER_STATUS_NOT_AVAILABLE):
        return False, "offline"
    if status & (win32print.PRINTER_STATUS_BUSY | win32print.PRINTER_STATUS_PRINTING):
        return False, "busy"
    return False, "unknown"


def _center(text: str, width: int = COL) -> str:
    return text.center(width)[:width]


def _right(label: str, value: str, width: int = COL) -> str:
    line = f"{label}{value}"
    padding = max(width - len(label) - len(value), 1)
    return f"{label}{' ' * padding}{value}"


def _quantize(amount) -> str:
    """Arrondit ROUND_HALF_UP puis formate le montant imprimé sur le ticket.

    Cette fonction ne faisait que formater (`f"{amount:.2f}"`), ce qui applique l'arrondi
    BANQUIER de Python et non ROUND_HALF_UP : 8.345 s'imprimait 8.34 et 1234.565 s'imprimait
    1234.56, alors que la vente scellée en base retenait 8.35 et 1234.57. Le document remis à
    la cliente pouvait donc afficher un centime de moins que le montant réellement encaissé.
    """
    return f"{quantize_money(amount):.2f}"


def _clean_str(text: str) -> str:
    """Purge les caractères de contrôle pour prévenir toute injection de commandes ESC/POS."""
    if not text:
        return ""
    return "".join(c for c in str(text) if c in ("\n", "\t") or (32 <= ord(c) < 127))


def generate_esc_pos_receipt(ticket_data: dict, fiscal_hash: str) -> bytes:
    """
    Génère le flux binaire ESC/POS complet d'un ticket de caisse :
    en-tête, tableau articles, totaux, ventilation TVA, hash fiscal scellé,
    code-barres du ticket et coupure papier (GS V 66 0).

    ticket_data attend les clés : shop_name, numero, items (liste de dicts
    avec qty/label/total), total_ttc, vat_breakdown (liste de dicts avec
    rate/base/amount).
    """
    payload = bytearray()
    payload += ESC_INIT
    payload += ESC_ALIGN_CENTER

    shop_name = _clean_str(ticket_data.get("shop_name", "Kōdo POS"))
    payload += ESC_BOLD_ON
    payload += (_center(shop_name) + "\n").encode("ascii", errors="replace")
    payload += ESC_BOLD_OFF

    numero = _clean_str(ticket_data.get("numero", ""))
    payload += (f"Ticket #{numero}\n".encode("ascii", errors="replace"))
    payload += ("-" * COL + "\n").encode("ascii")

    payload += ESC_ALIGN_LEFT
    for item in ticket_data.get("items", []):
        qty = item.get("qty", 1)
        label = _clean_str(str(item.get("label", "")))[:30]
        total = _quantize(Decimal(str(item.get("total", "0"))))
        line = f"{qty:<4}{label:<28}{total:>10}"
        payload += (line[:COL] + "\n").encode("ascii", errors="replace")

    payload += ("-" * COL + "\n").encode("ascii")

    for vat in ticket_data.get("vat_breakdown", []):
        rate = vat.get("rate", "0")
        base = _quantize(Decimal(str(vat.get("base", "0"))))
        amount = _quantize(Decimal(str(vat.get("amount", "0"))))
        line = _right(f"TVA {rate}% (base {base})", f"{amount}")
        payload += (line[:COL] + "\n").encode("ascii", errors="replace")

    total_ttc = _quantize(Decimal(str(ticket_data.get("total_ttc", "0"))))
    payload += ESC_BOLD_ON
    payload += (_right("TOTAL TTC", total_ttc) + "\n").encode("ascii", errors="replace")
    payload += ESC_BOLD_OFF

    payload += ("-" * COL + "\n").encode("ascii")
    payload += ESC_ALIGN_CENTER
    safe_hash = _clean_str(fiscal_hash)
    payload += (f"Hash: {safe_hash}\n").encode("ascii", errors="replace")

    payload += _generate_barcode(str(numero))

    payload += b"\n\n\n"
    payload += GS_CUT_FUNCTION

    return bytes(payload)


def _generate_barcode(data: str) -> bytes:
    """Code-barres CODE128 (GS k 73) du numéro de ticket avec sous-ensemble {B."""
    if not data:
        return b""
    encoded = data.encode("ascii", errors="ignore")[:250]
    payload = b"{B" + encoded
    header = GS + b'k' + bytes([73, len(payload)])
    return header + payload


def open_cash_drawer_sequence() -> bytes:
    """
    Retourne la séquence d'impulsion électrique standard ESC/POS
    (ESC p 0 25 250) pour déclencher le solénoïde du tiroir-caisse (RJ11).
    """
    return ESC_INIT + ESC_DRAWER_PIN2
