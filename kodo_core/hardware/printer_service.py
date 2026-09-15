"""
Service matériel de caisse : détection d'état imprimante thermique, génération
du flux binaire ESC/POS du ticket, et commande d'ouverture du tiroir-caisse.

Découplé de l'UI : toute interaction OS (subprocess CUPS / win32print) passe
par des points d'entrée injectables pour rester testable avec des flux simulés.
"""
import sys
import subprocess
from decimal import Decimal
from typing import Optional, Sequence

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


def _quantize(amount: Decimal) -> str:
    return f"{amount:.2f}"


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

    shop_name = ticket_data.get("shop_name", "Kōdo POS")
    payload += ESC_BOLD_ON
    payload += (_center(shop_name) + "\n").encode("ascii", errors="replace")
    payload += ESC_BOLD_OFF

    numero = ticket_data.get("numero", "")
    payload += (f"Ticket #{numero}\n".encode("ascii", errors="replace"))
    payload += ("-" * COL + "\n").encode("ascii")

    payload += ESC_ALIGN_LEFT
    for item in ticket_data.get("items", []):
        qty = item.get("qty", 1)
        label = str(item.get("label", ""))[:30]
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
    payload += (f"Hash: {fiscal_hash}\n").encode("ascii", errors="replace")

    payload += _generate_barcode(str(numero))

    payload += b"\n\n\n"
    payload += GS_CUT_FUNCTION

    return bytes(payload)


def _generate_barcode(data: str) -> bytes:
    """Code-barres CODE128 (GS k 73) du numéro de ticket."""
    if not data:
        return b""
    encoded = data.encode("ascii", errors="ignore")[:255]
    header = GS + b'k' + bytes([73]) + bytes([len(encoded)])
    return header + encoded


def open_cash_drawer_sequence() -> bytes:
    """
    Retourne la séquence d'impulsion électrique standard ESC/POS
    (ESC p 0 25 250) pour déclencher le solénoïde du tiroir-caisse (RJ11).
    """
    return ESC_INIT + ESC_DRAWER_PIN2
