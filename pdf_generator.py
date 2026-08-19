"""
Façade pdf_generator -> kodo_core.hardware.pdf
Point d'entrée de compatibilité pour la génération de PDF vectoriels via ReportLab.
"""
from kodo_core.hardware.pdf import (
    NumberedCanvas,
    get_param,
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
    "NumberedCanvas",
    "get_param",
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
