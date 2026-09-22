"""
Façade ticket_printer -> kodo_core.hardware.printer
Point d'entrée de compatibilité pour le système d'impression thermique.
"""
from kodo_core.hardware.printer import (
    COL,
    ESCPOSThermalPrinter,
    get_resource_path,
    strip_accents,
    sanitize_escpos_text,
    _center,
    _right,
    _separator,
    generer_ticket,
    generer_ticket_takeaway,
    generer_ticket_promo,
    generer_image_ticket,
    pil_to_escpos_raster,
    imprimer_ticket,
    imprimer_ticket_caisse,
    imprimer_ticket_test,
    generer_ticket_test,
    get_ticket_logo_path,
    ouvrir_tiroir_caisse,
)
from kodo_core.hardware.print_worker import (
    PrintWorker,
    PrintJob,
    PrinterCircuitBreaker,
    get_print_worker,
)

__all__ = [
    "COL",
    "ESCPOSThermalPrinter",
    "get_resource_path",
    "get_ticket_logo_path",
    "strip_accents",
    "sanitize_escpos_text",
    "_center",
    "_right",
    "_separator",
    "generer_ticket",
    "generer_ticket_takeaway",
    "generer_ticket_promo",
    "generer_ticket_test",
    "generer_image_ticket",
    "pil_to_escpos_raster",
    "imprimer_ticket",
    "imprimer_ticket_caisse",
    "imprimer_ticket_test",
    "ouvrir_tiroir_caisse",
    "PrintWorker",
    "PrintJob",
    "PrinterCircuitBreaker",
    "get_print_worker",
]
