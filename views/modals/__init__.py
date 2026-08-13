"""
Package de Modales pour Kōdo POS Core.
"""
from views.modals.base import ToastNotification, show_toast, BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN
from views.modals.product import ProductEditModal, ProductModal, TailleSelectionModal, EtiquetteCodeBarreModal, RechercheProduitCaisseModal, RemiseLigneModal, generate_ean13_code, TAILLES_PRET_A_PORTER, COULEURS_PRET_A_PORTER
from views.modals.checkout import CheckoutModal, EncaissementModal, RemiseModal, ChangeReturnModal, PrestationModal, NumpadModal
from views.modals.customer import CustomerModal, ClientModal
from views.modals.config import PinModal
from views.modals.update_modal import UpdateNotificationModal, MandatoryUpdateBanner, MandatoryUpdateOverlay
from views.modals.hold_basket_modal import PaniersEnAttenteModal, CrashRestorationModal
from views.modals.z_caisse_modal import ZDeCaisseModal, ClotureModal, DepenseCaisseModal, ComptaReportingModal

from views.modals.category import GestionCategoriesModal, get_prepopulated_categories, GestionMarquesModal, get_prepopulated_marques

__all__ = [
    "ToastNotification",
    "show_toast",
    "ProductEditModal",
    "ProductModal",
    "TailleSelectionModal",
    "EtiquetteCodeBarreModal",
    "RechercheProduitCaisseModal",
    "generate_ean13_code",
    "GestionCategoriesModal",
    "get_prepopulated_categories",
    "GestionMarquesModal",
    "get_prepopulated_marques",
    "CheckoutModal",
    "EncaissementModal",
    "RemiseModal",
    "ChangeReturnModal",
    "PrestationModal",
    "NumpadModal",
    "CustomerModal",
    "ClientModal",
    "PinModal",
    "UpdateNotificationModal",
    "MandatoryUpdateBanner",
    "MandatoryUpdateOverlay",
    "PaniersEnAttenteModal",
    "CrashRestorationModal",
    "ZDeCaisseModal",
    "ClotureModal",
    "DepenseCaisseModal",
    "ComptaReportingModal",
    "TAILLES_PRET_A_PORTER",
    "COULEURS_PRET_A_PORTER",
    "BG", "SEC_BG", "ACCENT", "TEXT", "GRY", "LINE", "RED", "GRN"
]

