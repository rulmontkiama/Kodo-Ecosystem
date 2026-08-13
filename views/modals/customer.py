"""
Modales de gestion des Clients, Fiches Profils et Recherche.
"""
import customtkinter as ctk
import sqlite3
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification
from database_manager import get_connection

class CustomerModal(ctk.CTkToplevel):
    """Modale de création / modification de fiche client."""
    
    def __init__(self, parent, customer_id=None, on_save_callback=None):
        super().__init__(parent)
        self.customer_id = customer_id
        self.on_save_callback = on_save_callback
        
        self.title("Fiche Client")
        self.geometry("500x550")
        self.configure(fg_color=BG)
        self.resizable(False, False)
        
        self.transient(parent)
        self.grab_set()

        self.lbl_title = ctk.CTkLabel(self, text="Fiche Client", font=ctk.CTkFont(FNT_TITLE, 20, "bold"))
        self.lbl_title.pack(pady=15)

        form = ctk.CTkFrame(self, fg_color=SEC_BG, corner_radius=RAD)
        form.pack(fill="both", expand=True, padx=20, pady=10)

        self.entry_nom = self._field(form, "Nom complet :", "ex: Jean Dupont")
        self.entry_email = self._field(form, "Email :", "jean.dupont@email.com")
        self.entry_phone = self._field(form, "Téléphone :", "+32 470 12 34 56")
        self.entry_taille_h = self._field(form, "Taille Haut (Prêt-à-porter) :", "M / L / 40")
        self.entry_taille_b = self._field(form, "Taille Bas (Prêt-à-porter) :", "32 / 42")

        btn_box = ctk.CTkFrame(self, fg_color="transparent")
        btn_box.pack(fill="x", padx=20, pady=15)
        
        ctk.CTkButton(btn_box, text="Annuler", fg_color=GRY, command=self.destroy).pack(side="left", padx=10)
        ctk.CTkButton(btn_box, text="Sauvegarder Client", fg_color=ACCENT, command=self._save).pack(side="right", padx=10)

    def _field(self, parent, label, ph):
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(FNT_BODY, 12, "bold")).pack(anchor="w", padx=15, pady=(8, 2))
        e = ctk.CTkEntry(parent, placeholder_text=ph, height=36)
        e.pack(fill="x", padx=15, pady=(0, 5))
        return e

    def _save(self):
        nom = self.entry_nom.get().strip()
        email = self.entry_email.get().strip()
        if not nom:
            ToastNotification(self, "Le nom est obligatoire", type="error")
            return

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Clients (nom, email, taille_haut, taille_bas) VALUES (?, ?, ?, ?)", 
                       (nom, email, self.entry_taille_h.get().strip(), self.entry_taille_b.get().strip()))
        conn.commit()
        conn.close()
        
        if self.on_save_callback:
            self.on_save_callback()
        self.destroy()

class ClientModal(ctk.CTkToplevel):
    """Modale de sélection / liaison rapide d'un client."""
    def __init__(self, parent, on_select_callback):
        super().__init__(parent)
        self.on_select_callback = on_select_callback
        self.title("Sélection Client")
        self.geometry("450x480")
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text="Sélectionner un Client", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT[0]).pack(pady=15)

        self.e_search = ctk.CTkEntry(self, placeholder_text="Rechercher par nom ou email...", height=40)
        self.e_search.pack(padx=20, pady=5, fill="x")
        self.e_search.bind("<KeyRelease>", lambda e: self._search())

        self.scroll = ctk.CTkScrollableFrame(self, fg_color=SEC_BG[0])
        self.scroll.pack(fill="both", expand=True, padx=20, pady=10)

        self._search()

    def _search(self):
        for c in self.scroll.winfo_children(): c.destroy()
        q = self.e_search.get().strip()
        conn = get_connection()
        c = conn.cursor()
        if q:
            c.execute("SELECT id, nom, email FROM Clients WHERE nom LIKE ? OR email LIKE ? LIMIT 10", (f"%{q}%", f"%{q}%"))
        else:
            c.execute("SELECT id, nom, email FROM Clients ORDER BY id DESC LIMIT 10")
        rows = c.fetchall()
        conn.close()

        for cid, nom, email in rows:
            btn = ctk.CTkButton(self.scroll, text=f"{nom} ({email or 'Pas d\'email'})", fg_color="transparent", text_color=TEXT[0], hover_color=LINE[0], anchor="w", command=lambda i=cid, n=nom: self._select(i, n))
            btn.pack(fill="x", pady=2)

    def _select(self, cid, nom):
        if self.on_select_callback:
            self.on_select_callback(cid, nom)
        self.destroy()
