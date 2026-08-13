"""
Modale de Clôture de Caisse Quotidienne (Z de Caisse NF525).
"""
import customtkinter as ctk
from decimal import Decimal
from views.modals.base import BG, SEC_BG, ACCENT, TEXT, GRY, LINE, RED, GRN, FNT_TITLE, FNT_BODY, RAD, ToastNotification
import database_manager

class ZDeCaisseModal(ctk.CTkToplevel):
    """Modale de rapport et validation de la clôture de caisse Z avec chaînage cryptographique SHA-256."""

    def __init__(self, parent, caisse_id="POS-01", vendeur="Admin", on_complete=None):
        super().__init__(parent)
        self.caisse_id = caisse_id
        self.vendeur = vendeur
        self.on_complete = on_complete

        self.title("Clôture de Caisse (Z de Caisse)")
        self.geometry("540x620")
        self.configure(fg_color=BG[0])
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        # Calculer le bilan non clôturé
        self.bilan = database_manager.generer_bilan_z_journalier(caisse_id=self.caisse_id)

        # En-tête
        lbl_title = ctk.CTkLabel(self, text="Clôture de Caisse (Z de Caisse)", font=ctk.CTkFont(FNT_TITLE, 20, "bold"), text_color=TEXT[0])
        lbl_title.pack(pady=(20, 5))

        lbl_sub = ctk.CTkLabel(self, text=f"Bilan officiel certifié NF525 — {self.caisse_id}", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY[0])
        lbl_sub.pack(pady=(0, 15))

        # Carte de synthèse
        card = ctk.CTkFrame(self, fg_color=SEC_BG[0], corner_radius=RAD)
        card.pack(fill="both", expand=True, padx=25, pady=5)

        self._row(card, "Tickets enregistrés :", f"{self.bilan['nb_tickets']} ticket(s)")
        self._row(card, "Total Ventes (TTC) :", f"{self.bilan['total_tvac']:.2f} €", is_bold=True, color=ACCENT[0])
        self._row(card, "Total Hors TVA (HTVA) :", f"{self.bilan['total_htva']:.2f} €")
        self._row(card, "Total TVA (21%) :", f"{self.bilan['total_tva']:.2f} €")
        self._row(card, "Total Remises :", f"−{self.bilan['total_remises']:.2f} €")

        div = ctk.CTkFrame(card, height=1, fg_color=LINE[0])
        div.pack(fill="x", padx=15, pady=10)

        self._row(card, "Total Carte Bancaire :", f"{self.bilan['total_carte']:.2f} €")
        self._row(card, "Total Espèces (Théorique) :", f"{self.bilan['total_especes']:.2f} €", is_bold=True)

        # Champ de saisie du fond de caisse réel
        ctk.CTkLabel(card, text="Fond de caisse réel compté (€) :", font=ctk.CTkFont(FNT_BODY, 13, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(10, 2))
        self.entry_reel = ctk.CTkEntry(card, placeholder_text=f"ex: {self.bilan['total_especes']:.2f}", height=40, font=ctk.CTkFont(FNT_BODY, 14))
        self.entry_reel.pack(fill="x", padx=15, pady=(0, 15))

        # Boutons d'action
        btn_box = ctk.CTkFrame(self, fg_color="transparent")
        btn_box.pack(fill="x", padx=25, pady=15)

        btn_cancel = ctk.CTkButton(btn_box, text="Annuler", fg_color=GRY[0], height=44, command=self.destroy)
        btn_cancel.pack(side="left", padx=5)

        btn_validate = ctk.CTkButton(btn_box, text="Clôturer la Caisse (Z)", fg_color=GRN[0], text_color="#FFFFFF", height=44, font=ctk.CTkFont(FNT_BODY, 14, "bold"), command=self._valider_cloture)
        btn_validate.pack(side="right", fill="x", expand=True, padx=5)

    def _row(self, parent, label, val, is_bold=False, color=None):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", padx=15, pady=4)
        c = color if color else TEXT[0]
        f = ctk.CTkFont(FNT_BODY, 13, "bold" if is_bold else "normal")
        ctk.CTkLabel(r, text=label, font=f, text_color=TEXT[0]).pack(side="left")
        ctk.CTkLabel(r, text=val, font=f, text_color=c).pack(side="right")

    def _valider_cloture(self):
        try:
            val_str = self.entry_reel.get().strip().replace(",", ".")
            fond_reel = Decimal(val_str) if val_str else self.bilan["total_especes"]
        except Exception:
            fond_reel = self.bilan["total_especes"]

        res = database_manager.enregistrer_cloture_caisse(
            caisse_id=self.caisse_id,
            fond_caisse_reel=fond_reel,
            vendeur=self.vendeur
        )

        ToastNotification(self.master, f"Clôture Z effectuée avec succès ! Hash: {res['current_hash'][:12]}...", type="success")
        if self.on_complete:
            self.on_complete(res)
        self.destroy()


ClotureModal = ZDeCaisseModal


class DepenseCaisseModal(ctk.CTkToplevel):
    """Modale de saisie d'une dépense / sortie de caisse."""

    def __init__(self, parent, vendeur_nom="Admin", callback=None):
        super().__init__(parent)
        self.vendeur_nom = vendeur_nom
        self.callback = callback

        self.title("Nouvelle Dépense / Sortie de Caisse")
        self.geometry("460x420")
        self.configure(fg_color=BG[0])
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text="Sortie de Caisse / Dépense", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT[0]).pack(pady=(20, 5))
        ctk.CTkLabel(self, text="Enregistrez une sortie d'espèces ou paiement fournisseur", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY[0]).pack(pady=(0, 15))

        card = ctk.CTkFrame(self, fg_color=SEC_BG[0], corner_radius=RAD)
        card.pack(fill="both", expand=True, padx=25, pady=5)

        ctk.CTkLabel(card, text="Motif / Libellé de la dépense :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(15, 2))
        self.entry_motif = ctk.CTkEntry(card, placeholder_text="ex: Achat fournitures, Coursier...", height=38, font=ctk.CTkFont(FNT_BODY, 13))
        self.entry_motif.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(card, text="Montant (€) :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(5, 2))
        self.entry_montant = ctk.CTkEntry(card, placeholder_text="0.00", height=38, font=ctk.CTkFont(FNT_BODY, 14, "bold"))
        self.entry_montant.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(card, text="Moyen de paiement :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(5, 2))
        self.combo_moyen = ctk.CTkOptionMenu(card, values=["Espèces", "Carte Bancaire", "Virement"], height=38, font=ctk.CTkFont(FNT_BODY, 13))
        self.combo_moyen.set("Espèces")
        self.combo_moyen.pack(fill="x", padx=15, pady=(0, 15))

        btn_box = ctk.CTkFrame(self, fg_color="transparent")
        btn_box.pack(fill="x", padx=25, pady=15)

        btn_cancel = ctk.CTkButton(btn_box, text="Annuler", fg_color=GRY[0], height=42, command=self.destroy)
        btn_cancel.pack(side="left", padx=5)

        btn_val = ctk.CTkButton(btn_box, text="Enregistrer Dépense", fg_color=RED[0], text_color="#FFFFFF", height=42, font=ctk.CTkFont(FNT_BODY, 13, "bold"), command=self._enregistrer)
        btn_val.pack(side="right", fill="x", expand=True, padx=5)

    def _enregistrer(self):
        motif = self.entry_motif.get().strip()
        montant_str = self.entry_montant.get().strip().replace(",", ".")

        if not motif:
            ToastNotification(self.master, "Veuillez saisir le motif de la dépense", type="warning")
            return

        try:
            montant = float(montant_str)
            if montant <= 0:
                raise ValueError()
        except Exception:
            ToastNotification(self.master, "Montant invalide", type="error")
            return

        moyen = self.combo_moyen.get()

        conn = None
        try:
            conn = database_manager.get_connection()
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS Depenses_Caisse (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_heure TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    libelle TEXT NOT NULL,
                    montant DECIMAL NOT NULL,
                    moyen_paiement TEXT DEFAULT 'Espèces',
                    vendeur TEXT
                )
            """)
            c.execute("""
                INSERT INTO Depenses_Caisse (libelle, montant, moyen_paiement, vendeur)
                VALUES (?, ?, ?, ?)
            """, (motif, montant, moyen, self.vendeur_nom))
            conn.commit()
            ToastNotification(self.master, f"Dépense de {montant:.2f} € enregistrée", type="success")
            if self.callback:
                self.callback()
            self.destroy()
        except Exception as e:
            ToastNotification(self.master, f"Erreur enregistrement : {e}", type="error")
        finally:
            if conn:
                conn.close()


class ComptaReportingModal(ctk.CTkToplevel):
    """Modale de génération et d'export du reporting comptable."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Module Comptable Pro — Exports")
        self.geometry("500x480")
        self.configure(fg_color=BG[0])
        self.resizable(False, False)

        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text="Module Comptable & Reporting", font=ctk.CTkFont(FNT_TITLE, 18, "bold"), text_color=TEXT[0]).pack(pady=(20, 5))
        ctk.CTkLabel(self, text="Exportations conformes pour votre expert-comptable", font=ctk.CTkFont(FNT_BODY, 12), text_color=GRY[0]).pack(pady=(0, 15))

        card = ctk.CTkFrame(self, fg_color=SEC_BG[0], corner_radius=RAD)
        card.pack(fill="both", expand=True, padx=25, pady=5)

        import datetime
        now = datetime.datetime.now()

        ctk.CTkLabel(card, text="Mois :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(15, 2))
        self.combo_mois = ctk.CTkOptionMenu(card, values=[f"{m:02d}" for m in range(1, 13)], height=38)
        self.combo_mois.set(f"{now.month:02d}")
        self.combo_mois.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(card, text="Année :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(5, 2))
        self.combo_annee = ctk.CTkOptionMenu(card, values=[str(a) for a in range(now.year - 2, now.year + 2)], height=38)
        self.combo_annee.set(str(now.year))
        self.combo_annee.pack(fill="x", padx=15, pady=(0, 10))

        ctk.CTkLabel(card, text="Format d'export :", font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=TEXT[0]).pack(anchor="w", padx=15, pady=(5, 2))
        self.combo_format = ctk.CTkOptionMenu(
            card,
            values=[
                "Rapport PDF Journalier (.pdf)",
                "Rapport PDF Mensuel (.pdf)",
                "Excel (.xlsx)",
                "CSV (.csv)",
                "WinBooks (CSV)"
            ],
            height=38
        )
        self.combo_format.set("Rapport PDF Journalier (.pdf)")
        self.combo_format.pack(fill="x", padx=15, pady=(0, 15))

        btn_box = ctk.CTkFrame(self, fg_color="transparent")
        btn_box.pack(fill="x", padx=25, pady=15)

        btn_cancel = ctk.CTkButton(btn_box, text="Fermer", fg_color=GRY[0], height=44, command=self.destroy)
        btn_cancel.pack(side="left", padx=5)

        btn_exp = ctk.CTkButton(btn_box, text="Générer & Ouvrir", fg_color=ACCENT[0], text_color="#FFFFFF", height=44, font=ctk.CTkFont(FNT_BODY, 13, "bold"), command=self._do_export)
        btn_exp.pack(side="right", fill="x", expand=True, padx=5)

    def _do_export(self):
        try:
            import os, subprocess, shutil, datetime
            from tkinter import filedialog
            import export_manager
            import pdf_generator

            mois = int(self.combo_mois.get())
            annee = int(self.combo_annee.get())
            fmt_sel = self.combo_format.get()
            today_str = datetime.date.today().isoformat()
            desktop_dir = os.path.expanduser("~/Desktop")

            if "PDF Journalier" in fmt_sel:
                ext = ".pdf"
                file_types = [("Fichiers PDF", "*.pdf")]
                default_name = f"Rapport_Journalier_{today_str}.pdf"
            elif "PDF Mensuel" in fmt_sel:
                ext = ".pdf"
                file_types = [("Fichiers PDF", "*.pdf")]
                default_name = f"Rapport_Mensuel_{annee:04d}-{mois:02d}.pdf"
            elif "WinBooks" in fmt_sel or "CSV" in fmt_sel:
                ext = ".csv"
                file_types = [("Fichiers CSV", "*.csv")]
                default_name = f"Export_Comptable_{annee:04d}_{mois:02d}.csv"
            else:
                ext = ".xlsx"
                file_types = [("Fichiers Excel", "*.xlsx")]
                default_name = f"Export_Comptable_{annee:04d}_{mois:02d}.xlsx"

            chosen_path = filedialog.asksaveasfilename(
                parent=self,
                title="Enregistrer le fichier comptable",
                initialdir=desktop_dir,
                initialfile=default_name,
                defaultextension=ext,
                filetypes=file_types
            )

            save_dest = chosen_path if chosen_path else os.path.join(desktop_dir, default_name)

            if "PDF Journalier" in fmt_sel:
                pdf_generator.generer_rapport_pdf("jour", today_str, save_dest)
                target_path = save_dest
            elif "PDF Mensuel" in fmt_sel:
                pdf_generator.generer_rapport_pdf("mois", f"{annee:04d}-{mois:02d}", save_dest)
                target_path = save_dest
            elif "WinBooks" in fmt_sel:
                generated = export_manager.export_winbooks_csv(mois=mois, annee=annee)
                shutil.copy2(generated, save_dest)
                target_path = save_dest
            elif "CSV" in fmt_sel:
                generated = export_manager.export_comptable_mensuel(mois, annee, format_type="csv")
                shutil.copy2(generated, save_dest)
                target_path = save_dest
            else:
                generated = export_manager.export_comptable_mensuel(mois, annee, format_type="excel")
                shutil.copy2(generated, save_dest)
                target_path = save_dest

            if target_path and os.path.exists(target_path):
                try:
                    subprocess.Popen(["open", target_path])
                except Exception:
                    pass

                ToastNotification(self.master, f"Fichier téléchargé : {os.path.basename(target_path)}", type="success")

            self.destroy()
        except Exception as e:
            ToastNotification(self.master, f"Erreur d'exportation : {e}", type="error")

