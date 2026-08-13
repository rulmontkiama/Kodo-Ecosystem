"""
Modale de gestion des Paniers en Attente (Hold Basket) et Restauration Post-Crash.
"""
import customtkinter as ctk
from decimal import Decimal
from views.modals.base import (
    BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD
)
import database_manager

class PaniersEnAttenteModal(ctk.CTkToplevel):
    def __init__(self, parent, on_restore_callback):
        super().__init__(parent)
        self.parent = parent
        self.on_restore_callback = on_restore_callback

        self.title("Paniers en attente")
        self.geometry("640x520")
        self.resizable(False, False)
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()

        # En-tête
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(20, 10))

        title_lbl = ctk.CTkLabel(
            header,
            text="Paniers en Attente",
            font=ctk.CTkFont(FNT_TITLE, 22, "bold"),
            text_color=TEXT[0]
        )
        title_lbl.pack(side="left")

        btn_close = ctk.CTkButton(
            header,
            text="✕",
            width=36,
            height=36,
            corner_radius=18,
            fg_color=SEC_BG[0],
            text_color=TEXT[0],
            hover_color=LINE[0],
            command=self.destroy
        )
        btn_close.pack(side="right")

        # Scrollable list area
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=24, pady=10)

        self._charger_paniers()

    def _charger_paniers(self):
        for child in self.scroll.winfo_children():
            child.destroy()

        paniers = database_manager.lister_paniers_en_attente()

        if not paniers:
            empty_lbl = ctk.CTkLabel(
                self.scroll,
                text="Aucun panier en attente pour le moment.",
                font=ctk.CTkFont(FNT_BODY, 15),
                text_color=GRY[0]
            )
            empty_lbl.pack(pady=60)
            return

        for item in paniers:
            card = ctk.CTkFrame(self.scroll, fg_color=SEC_BG[0], corner_radius=16)
            card.pack(fill="x", pady=6, padx=4)

            left_box = ctk.CTkFrame(card, fg_color="transparent")
            left_box.pack(side="left", padx=16, pady=12, fill="both", expand=True)

            nb_items = len(item.get("panier", []))
            client = item.get("client_nom") or "Client au comptoir"
            date_str = str(item.get("date_creation", ""))[:16]

            lbl_title = ctk.CTkLabel(
                left_box,
                text=f"Panier #{item['id']} — {client}",
                font=ctk.CTkFont(FNT_BODY, 15, "bold"),
                text_color=TEXT[0],
                anchor="w"
            )
            lbl_title.pack(fill="x")

            lbl_meta = ctk.CTkLabel(
                left_box,
                text=f"{nb_items} article(s) • Créé le {date_str}",
                font=ctk.CTkFont(FNT_BODY, 13),
                text_color=GRY[0],
                anchor="w"
            )
            lbl_meta.pack(fill="x")

            lbl_total = ctk.CTkLabel(
                card,
                text=f"{item['total_tvac']:.2f} €",
                font=ctk.CTkFont(FNT_TITLE, 18, "bold"),
                text_color=ACCENT[0]
            )
            lbl_total.pack(side="left", padx=12)

            btn_box = ctk.CTkFrame(card, fg_color="transparent")
            btn_box.pack(side="right", padx=12, pady=12)

            btn_restore = ctk.CTkButton(
                btn_box,
                text="Restaurer",
                font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                fg_color=GRN[0],
                text_color="#FFFFFF",
                height=36,
                corner_radius=18,
                command=lambda pid=item['id']: self._restaurer(pid)
            )
            btn_restore.pack(side="top", pady=2)

            btn_delete = ctk.CTkButton(
                btn_box,
                text="Supprimer",
                font=ctk.CTkFont(FNT_BODY, 12),
                fg_color="transparent",
                text_color=RED[0],
                hover_color=LINE[0],
                height=28,
                corner_radius=14,
                command=lambda pid=item['id']: self._supprimer(pid)
            )
            btn_delete.pack(side="top", pady=2)

    def _restaurer(self, panier_id):
        res = database_manager.recuperer_panier_en_attente(panier_id)
        if res and self.on_restore_callback:
            self.on_restore_callback(res)
        self.destroy()

    def _supprimer(self, panier_id):
        database_manager.supprimer_panier_en_attente(panier_id)
        self._charger_paniers()
        if hasattr(self.parent, '_update_badge_paniers_attente'):
            self.parent._update_badge_paniers_attente()


class CrashRestorationModal(ctk.CTkToplevel):
    """Modale proposant la restauration immédiate d'un panier interrompu suite à un crash."""
    def __init__(self, parent, session_data, on_confirm, on_ignore):
        super().__init__(parent)
        self.session_data = session_data
        self.on_confirm = on_confirm
        self.on_ignore = on_ignore

        self.title("Reprise après interruption")
        self.geometry("520x340")
        self.resizable(False, False)
        self.configure(fg_color=BG[0])
        self.transient(parent)
        self.grab_set()

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=28, pady=24)

        icon_lbl = ctk.CTkLabel(
            content,
            text="Reprise après interruption",
            font=ctk.CTkFont(FNT_TITLE, 20, "bold"),
            text_color=TEXT[0]
        )
        icon_lbl.pack(anchor="w", pady=(0, 10))

        panier_items = session_data.get("panier", [])
        nb_items = len(panier_items)
        tot_str = session_data.get("total_tvac", "0.00")

        desc_lbl = ctk.CTkLabel(
            content,
            text=(
                f"Un panier en cours non finalisé a été détecté suite à une coupure ou fermeture inattendue.\n\n"
                f"• Articles : {nb_items} produit(s)\n"
                f"• Montant total : {tot_str} €\n\n"
                f"Souhaitez-vous restaurer immédiatement ce panier ?"
            ),
            font=ctk.CTkFont(FNT_BODY, 14),
            text_color=TEXT[0],
            justify="left",
            wraplength=460
        )
        desc_lbl.pack(anchor="w", pady=(0, 20))

        btn_box = ctk.CTkFrame(content, fg_color="transparent")
        btn_box.pack(fill="x", side="bottom")

        btn_yes = ctk.CTkButton(
            btn_box,
            text="Restaurer le panier",
            font=ctk.CTkFont(FNT_BODY, 14, "bold"),
            fg_color=GRN[0],
            text_color="#FFFFFF",
            height=44,
            corner_radius=22,
            command=self._confirm
        )
        btn_yes.pack(side="left", fill="x", expand=True, padx=(0, 8))

        btn_no = ctk.CTkButton(
            btn_box,
            text="Ignorer",
            font=ctk.CTkFont(FNT_BODY, 14),
            fg_color=SEC_BG[0],
            text_color=TEXT[0],
            hover_color=LINE[0],
            height=44,
            corner_radius=22,
            command=self._ignore
        )
        btn_no.pack(side="right", fill="x", expand=True, padx=(8, 0))

    def _confirm(self):
        self.destroy()
        if self.on_confirm:
            self.on_confirm()

    def _ignore(self):
        self.destroy()
        if self.on_ignore:
            self.on_ignore()
