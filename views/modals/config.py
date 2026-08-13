"""
Modales de Sécurité, Saisie de PIN et Configuration Enseigne.
"""
import customtkinter as ctk
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification
from database_manager import hash_pin

class PinModal(ctk.CTkToplevel):
    """Modale sécurisée de saisie de PIN pour authentification vendeur / admin."""
    
    def __init__(self, parent, title_text="Authentification Vendeur", on_success_callback=None):
        super().__init__(parent)
        self.on_success_callback = on_success_callback
        
        self.title("Sécurité PIN")
        self.geometry("350x420")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text=title_text, font=ctk.CTkFont(FNT_TITLE, 18, "bold")).pack(pady=20)

        self.entry_pin = ctk.CTkEntry(self, show="•", font=ctk.CTkFont(FNT_TITLE, 24, "bold"), justify="center", width=200, height=45)
        self.entry_pin.pack(pady=10)

        # Clavier Numérique
        pad = ctk.CTkFrame(self, fg_color="transparent")
        pad.pack(pady=10)

        for i in range(1, 10):
            row = (i - 1) // 3
            col = (i - 1) % 3
            btn = ctk.CTkButton(pad, text=str(i), width=60, height=45, font=ctk.CTkFont(FNT_TITLE, 16, "bold"), command=lambda x=str(i): self._add_digit(x))
            btn.grid(row=row, column=col, padx=5, pady=5)

        btn_clr = ctk.CTkButton(pad, text="C", width=60, height=45, fg_color=RED, command=lambda: self.entry_pin.delete(0, 'end'))
        btn_clr.grid(row=3, column=0, padx=5, pady=5)

        btn_zero = ctk.CTkButton(pad, text="0", width=60, height=45, font=ctk.CTkFont(FNT_TITLE, 16, "bold"), command=lambda: self._add_digit("0"))
        btn_zero.grid(row=3, column=1, padx=5, pady=5)

        btn_ok = ctk.CTkButton(pad, text="OK", width=60, height=45, fg_color=GRN, command=self._validate)
        btn_ok.grid(row=3, column=2, padx=5, pady=5)

    def _add_digit(self, char):
        if len(self.entry_pin.get()) < 6:
            self.entry_pin.insert('end', char)

    def _validate(self):
        pin = self.entry_pin.get().strip()
        if not pin:
            return
        hashed = hash_pin(pin)
        if self.on_success_callback:
            self.on_success_callback(hashed)
        self.destroy()
