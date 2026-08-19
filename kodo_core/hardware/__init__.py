"""
Module matériel et impression Kōdo POS (Thermal ESC/POS & PDF Vectoriels).
"""
from .printer import (
    COL,
    ESCPOSThermalPrinter,
    get_resource_path,
    strip_accents,
    generer_ticket,
    generer_ticket_takeaway,
    generer_ticket_promo,
    generer_image_ticket,
    pil_to_escpos_raster,
    imprimer_ticket,
    imprimer_ticket_caisse,
    ouvrir_tiroir_caisse,
)

from .pdf import (
    NumberedCanvas,
    generer_rapport_pdf,
    generer_etiquettes_pdf,
    generer_facture_pdf,
    generer_recu_pdf,
    generate_barcode_drawing,
    C_PRIMARY,
    C_CORAL,
    C_SECONDARY,
    C_LIGHT_BG,
    C_WHITE,
)

__all__ = [
    "COL",
    "ESCPOSThermalPrinter",
    "get_resource_path",
    "strip_accents",
    "generer_ticket",
    "generer_ticket_takeaway",
    "generer_ticket_promo",
    "generer_image_ticket",
    "pil_to_escpos_raster",
    "imprimer_ticket",
    "imprimer_ticket_caisse",
    "ouvrir_tiroir_caisse",
    "NumberedCanvas",
    "generer_rapport_pdf",
    "generer_etiquettes_pdf",
    "generer_facture_pdf",
    "generer_recu_pdf",
    "generate_barcode_drawing",
    "C_PRIMARY",
    "C_CORAL",
    "C_SECONDARY",
    "C_LIGHT_BG",
    "C_WHITE",
]
