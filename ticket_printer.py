"""
Façade ticket_printer -> kodo_core.hardware.printer
Point d'entrée de compatibilité pour le système d'impression thermique.
"""
from kodo_core.hardware.printer import (
    COL,
    ESCPOSThermalPrinter,
    get_resource_path,
    strip_accents,
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
    ouvrir_tiroir_caisse,
)

__all__ = [
    "COL",
    "ESCPOSThermalPrinter",
    "get_resource_path",
    "strip_accents",
    "_center",
    "_right",
    "_separator",
    "generer_ticket",
    "generer_ticket_takeaway",
    "generer_ticket_promo",
    "generer_image_ticket",
    "pil_to_escpos_raster",
    "imprimer_ticket",
    "imprimer_ticket_caisse",
    "ouvrir_tiroir_caisse",
]
