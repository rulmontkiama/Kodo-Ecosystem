"""
Hub de rétrocompatibilité pour les Modales Kōdo POS.
Redirige les appels vers le package modulaire views/modals/.
"""
from views.modals.base import ToastNotification, show_toast, BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD
from views.modals.product import ProductEditModal, ProductModal, TailleSelectionModal, RemiseLigneModal, TAILLES_PRET_A_PORTER, COULEURS_PRET_A_PORTER
from views.modals.checkout import CheckoutModal, EncaissementModal, RemiseModal, ChangeReturnModal, PrestationModal, NumpadModal
from views.modals.customer import CustomerModal, ClientModal
from views.modals.config import PinModal
from views.modals.update_modal import UpdateNotificationModal, MandatoryUpdateBanner, MandatoryUpdateOverlay
from views.modals.hold_basket_modal import PaniersEnAttenteModal, CrashRestorationModal
