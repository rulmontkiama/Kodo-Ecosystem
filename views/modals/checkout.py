"""
Modales d'Encaissement, de Réductions et de Modes de Paiement.
"""
import customtkinter as ctk
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification

class CheckoutModal(ctk.CTkToplevel):
    """Modale principale d'encaissement et de choix de règlement."""
    
    def __init__(self, parent, total_tvac, on_payment_complete=None):
        super().__init__(parent)
        self.total_tvac = total_tvac
        self.on_payment_complete = on_payment_complete
        
        self.title("Encaissement")
        self.geometry("500x550")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        
        self.transient(parent)
        self.grab_set()

        # Titre & Montant
        self.lbl_title = ctk.CTkLabel(self, text="Règlement de la Vente", font=ctk.CTkFont(FNT_TITLE, 22, "bold"))
        self.lbl_title.pack(pady=(20, 5))

        self.lbl_total = ctk.CTkLabel(
            self, 
            text=f"{self.total_tvac:.2f} €", 
            font=ctk.CTkFont(FNT_TITLE, 36, "bold"), 
            text_color=ACCENT
        )
        self.lbl_total.pack(pady=10)

        # Choix du moyen de paiement
        pay_frame = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        pay_frame.pack(fill="both", expand=True, padx=25, pady=15)

        btn_esp = ctk.CTkButton(pay_frame, text="Espèces", font=ctk.CTkFont(FNT_BODY, 16, "bold"), height=50, fg_color=GRN, command=lambda: self._complete("Espèces"))
        btn_esp.pack(fill="x", padx=20, pady=10)

        btn_cb = ctk.CTkButton(pay_frame, text="Carte Bancaire (Bancontact / Visa)", font=ctk.CTkFont(FNT_BODY, 16, "bold"), height=50, fg_color=ACCENT, command=lambda: self._complete("Carte"))
        btn_cb.pack(fill="x", padx=20, pady=10)

        btn_mix = ctk.CTkButton(pay_frame, text="Virement / Mixte", font=ctk.CTkFont(FNT_BODY, 14), height=40, fg_color=GRY, command=lambda: self._complete("Mixte"))
        btn_mix.pack(fill="x", padx=20, pady=5)

        # Bouton fermer
        btn_cancel = ctk.CTkButton(self, text="Annuler", fg_color="transparent", text_color=RED, command=self.destroy)
        btn_cancel.pack(pady=10)

    def _complete(self, method):
        if self.on_payment_complete:
            self.on_payment_complete(method)
        self.destroy()

from decimal import Decimal

class EncaissementModal(ctk.CTkToplevel):
    def __init__(self, parent, net, on_complete, methode_defaut=None, panier_items=None):
        super().__init__(parent)
        self.net = net
        self.on_complete = on_complete
        self.title("Encaissement")
        self.geometry("520x580")
        self.configure(fg_color=BG[0])
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        lbl_t = ctk.CTkLabel(self, text="Encaissement", font=ctk.CTkFont(FNT_TITLE, 22, "bold"), text_color=TEXT[0])
        lbl_t.pack(pady=(20, 5))

        lbl_m = ctk.CTkLabel(self, text=f"{self.net:.2f} €", font=ctk.CTkFont(FNT_TITLE, 36, "bold"), text_color=ACCENT[0])
        lbl_m.pack(pady=10)

        f = ctk.CTkFrame(self, fg_color=SEC_BG[0], corner_radius=16)
        f.pack(fill="both", expand=True, padx=24, pady=15)

        btn_esp = ctk.CTkButton(f, text="Espèces", font=ctk.CTkFont(FNT_BODY, 16, "bold"), height=48, fg_color=GRN[0], text_color="#FFF", command=lambda: self._pay("Espèces"))
        btn_esp.pack(fill="x", padx=20, pady=8)

        btn_cb = ctk.CTkButton(f, text="Bancontact / Carte", font=ctk.CTkFont(FNT_BODY, 16, "bold"), height=48, fg_color=ACCENT[0], text_color="#FFF", command=lambda: self._pay("Bancontact"))
        btn_cb.pack(fill="x", padx=20, pady=8)

        btn_qr = ctk.CTkButton(f, text="QR Code", font=ctk.CTkFont(FNT_BODY, 16, "bold"), height=48, fg_color=TEXT[0], text_color="#FFF", command=lambda: self._pay("QR_Code"))
        btn_qr.pack(fill="x", padx=20, pady=8)

        btn_cancel = ctk.CTkButton(self, text="Annuler", fg_color="transparent", text_color=RED[0], command=self.destroy)
        btn_cancel.pack(pady=10)

    def _pay(self, methode):
        self.destroy()
        if self.on_complete:
            self.on_complete([(methode, self.net)], Decimal("0.00"), False)

class RemiseModal(ctk.CTkToplevel):
    def __init__(self, parent, total, callback):
        super().__init__(parent)
        self.total = Decimal(str(total or 0))
        self.callback = callback
        self.title("Remise sur le Ticket")
        self.geometry("440x440")
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()

        self.calculated_remise = Decimal("0.00")

        # Titre
        ctk.CTkLabel(self, text="🏷️ Remise sur le Ticket", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT[0]).pack(pady=(18, 5))

        # Preview Total
        info_frame = ctk.CTkFrame(self, fg_color=SEC_BG[0], corner_radius=12)
        info_frame.pack(fill="x", padx=20, pady=5)

        ctk.CTkLabel(info_frame, text=f"Total Panier Brut : {self.total:.2f} €", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY[0]).pack(pady=(8, 2))

        self.lbl_preview = ctk.CTkLabel(info_frame, text=f"Nouveau Total : {self.total:.2f} €", font=ctk.CTkFont(FNT_BODY, 16, "bold"), text_color=GRN[0])
        self.lbl_preview.pack(pady=(0, 8))

        # Boutons d'accès rapide en %
        pct_frame = ctk.CTkFrame(self, fg_color="transparent")
        pct_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(pct_frame, text="Remises rapides :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", pady=(0, 4))
        btns_box = ctk.CTkFrame(pct_frame, fg_color="transparent")
        btns_box.pack(fill="x")

        for pct in [5, 10, 15, 20, 30, 50]:
            btn = ctk.CTkButton(
                btns_box,
                text=f"-{pct}%",
                width=55,
                height=32,
                fg_color=SEC_BG[0],
                text_color=TEXT[0],
                hover_color="#E2E8F0",
                corner_radius=8,
                command=lambda p=pct: self._apply_pct(p)
            )
            btn.pack(side="left", padx=2, expand=True)

        # Mode de saisie personnalisée (% et €)
        custom_frame = ctk.CTkFrame(self, fg_color="transparent")
        custom_frame.pack(fill="x", padx=20, pady=10)

        # % personnalisé
        pct_row = ctk.CTkFrame(custom_frame, fg_color="transparent")
        pct_row.pack(fill="x", pady=3)
        ctk.CTkLabel(pct_row, text="Remise en % :", width=120, anchor="w", font=ctk.CTkFont(FNT_BODY, 12), text_color=TEXT[0]).pack(side="left")
        self.entry_pct = ctk.CTkEntry(pct_row, placeholder_text="ex: 10", height=32, corner_radius=8)
        self.entry_pct.pack(side="left", fill="x", expand=True, padx=(0, 6))
        btn_calc_pct = ctk.CTkButton(pct_row, text="Appliquer %", width=90, height=32, fg_color=ACCENT[0], command=self._calc_custom_pct)
        btn_calc_pct.pack(side="right")

        # € personnalisé
        val_row = ctk.CTkFrame(custom_frame, fg_color="transparent")
        val_row.pack(fill="x", pady=3)
        ctk.CTkLabel(val_row, text="Remise en € :", width=120, anchor="w", font=ctk.CTkFont(FNT_BODY, 12), text_color=TEXT[0]).pack(side="left")
        self.entry_val = ctk.CTkEntry(val_row, placeholder_text="ex: 5.00", height=32, corner_radius=8)
        self.entry_val.pack(side="left", fill="x", expand=True, padx=(0, 6))
        btn_calc_val = ctk.CTkButton(val_row, text="Appliquer €", width=90, height=32, fg_color=ACCENT[0], command=self._calc_custom_val)
        btn_calc_val.pack(side="right")

        # Actions
        btn_box = ctk.CTkFrame(self, fg_color="transparent")
        btn_box.pack(fill="x", padx=20, pady=15)

        btn_reset = ctk.CTkButton(btn_box, text="Réinitialiser", fg_color=GRY[0], width=100, height=36, corner_radius=10, command=self._reset_remise)
        btn_reset.pack(side="left")

        btn_val = ctk.CTkButton(btn_box, text="Valider", fg_color=GRN[0], hover_color="#2E7D32", height=36, corner_radius=10, command=self._valider)
        btn_val.pack(side="right", fill="x", expand=True, padx=(10, 0))

    def _apply_pct(self, pct):
        rem = (self.total * Decimal(str(pct)) / Decimal("100")).quantize(Decimal("0.01"))
        self.calculated_remise = min(self.total, rem)
        net = self.total - self.calculated_remise
        self.lbl_preview.configure(text=f"Nouveau Total : {net:.2f} € (-{pct}%)")

    def _calc_custom_pct(self):
        raw = self.entry_pct.get().strip().replace(",", ".")
        try:
            val = float(raw)
            if val < 0 or val > 100:
                return
            pct_dec = Decimal(str(val))
            rem = (self.total * pct_dec / Decimal("100")).quantize(Decimal("0.01"))
            self.calculated_remise = min(self.total, rem)
            net = self.total - self.calculated_remise
            self.lbl_preview.configure(text=f"Nouveau Total : {net:.2f} € (-{val:g}%)")
        except ValueError:
            pass

    def _calc_custom_val(self):
        raw = self.entry_val.get().strip().replace(",", ".")
        try:
            val = Decimal(raw)
            if val < Decimal("0.00"):
                return
            self.calculated_remise = min(self.total, val.quantize(Decimal("0.01")))
            net = self.total - self.calculated_remise
            self.lbl_preview.configure(text=f"Nouveau Total : {net:.2f} € (-{self.calculated_remise:.2f}€)")
        except:
            pass

    def _reset_remise(self):
        self.calculated_remise = Decimal("0.00")
        self.lbl_preview.configure(text=f"Nouveau Total : {self.total:.2f} € (Aucune)")

    def _valider(self):
        if self.callback:
            self.callback(self.calculated_remise)
        self.destroy()

class ChangeReturnModal(ctk.CTkToplevel):
    def __init__(self, parent, rendu):
        super().__init__(parent)
        self.rendu = rendu
        self.title("Rendu Monnaie")
        self.geometry("380x250")
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text="Rendu Monnaie", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT[0]).pack(pady=(20, 5))
        ctk.CTkLabel(self, text=f"{self.rendu:.2f} €", font=ctk.CTkFont(FNT_TITLE, 36, "bold"), text_color=GRN[0]).pack(pady=15)
        ctk.CTkButton(self, text="OK", height=40, fg_color=TEXT[0], text_color="#FFF", command=self.destroy).pack(pady=10)

class PrestationModal(ctk.CTkToplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.title("Ajouter une Prestation")
        self.geometry("420x360")
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text="Prestation / Service hors-stock", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT[0]).pack(pady=15)

        self.e_nom = ctk.CTkEntry(self, placeholder_text="Nom du service", height=40)
        self.e_nom.pack(padx=24, pady=6, fill="x")

        self.e_prix = ctk.CTkEntry(self, placeholder_text="Prix TTC (€)", height=40)
        self.e_prix.pack(padx=24, pady=6, fill="x")

        btn = ctk.CTkButton(self, text="Ajouter au Panier", font=ctk.CTkFont(FNT_BODY, 14, "bold"), height=44, fg_color=GRN[0], text_color="#FFF", command=self._add)
        btn.pack(padx=24, pady=15, fill="x")

    def _add(self):
        try:
            nom = self.e_nom.get().strip() or "Service"
            prix = Decimal(self.e_prix.get().replace(",", "."))
            if self.callback:
                self.callback(nom, prix, Decimal("0.21"))
        except: pass
        self.destroy()

class NumpadModal(ctk.CTkToplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.title("Saisie")
        self.geometry("300x350")
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()
        ctk.CTkLabel(self, text="Saisie numérique", font=ctk.CTkFont(FNT_TITLE, 16, "bold"), text_color=TEXT[0]).pack(pady=10)
        btn = ctk.CTkButton(self, text="Fermer", command=self.destroy)
        btn.pack(pady=20)
