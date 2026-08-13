import customtkinter as ctk
import datetime, os
from decimal import Decimal
from database_manager import get_connection
import export_manager

# Palette Kōdo POS Redesign (Stats)
C_BG      = "#F4F6F8"  # Fond principal Gris épuré
C_SEC_BG  = "#FFFFFF"  # Cartes et conteneurs Blanc pur
C_ACCENT  = "#FF6B6B"  # Nouveau Corail Red Kōdo POS
C_TEXT    = "#212529"  # Charcoal foncé
C_GRY     = "#868E96"  # Gris secondaire
C_LINE    = "#DEE2E6"  # Bordure
C_RED     = "#FF6B6B"  # Rouge corail
C_GRN     = "#28C76F"  # Vert émeraude

FNT_TITLE = "Inter"
FNT_BODY  = "Inter"
RAD = 20

import tkinter as tk

class WeeklySalesChart(ctk.CTkFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        ctk.CTkLabel(self, text="ACTIVITÉ DES 7 DERNIERS JOURS", font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=C_GRY, anchor="w").pack(fill="x", padx=24, pady=(5, 5))
        
        self.canvas = tk.Canvas(self, bg=C_SEC_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=20, pady=(5, 5))
        
        self.canvas.bind("<Configure>", lambda e: self.draw())
        
    def draw(self):
        self.canvas.delete("all")
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 50 or h < 50:
            return
            
        data = _get_sales_last_7_days()
        if not data:
            self.canvas.create_text(w/2, h/2, text="Aucune donnée d'activité", fill=C_GRY, font=(FNT_BODY, 12))
            return
            
        max_val = max([val for lbl, val in data])
        if max_val <= 0:
            max_val = 100.0
            
        margin_left = 35
        margin_right = 15
        margin_top = 20
        margin_bottom = 20
        
        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom
        
        # Grid lines
        nb_grid_lines = 4
        for i in range(nb_grid_lines):
            y_ratio = i / (nb_grid_lines - 1)
            y_pos = margin_top + chart_h * y_ratio
            self.canvas.create_line(margin_left, y_pos, w - margin_right, y_pos, fill=C_LINE, width=0.5)
            val_lbl = max_val * (1.0 - y_ratio)
            self.canvas.create_text(margin_left - 8, y_pos, text=f"{int(val_lbl)}", fill=C_GRY, font=(FNT_BODY, 8), anchor="e")
            
        # Barres
        nb_bars = len(data)
        bar_gap = 14
        bar_w = (chart_w - (nb_bars - 1) * bar_gap) / nb_bars
        
        for idx, (lbl, val) in enumerate(data):
            x1 = margin_left + idx * (bar_w + bar_gap)
            x2 = x1 + bar_w
            
            val_ratio = val / max_val
            bar_h = chart_h * val_ratio
            y2 = h - margin_bottom
            y1 = y2 - bar_h
            
            # Dessiner la barre
            radius = min(bar_w / 2, 8)
            if bar_h > radius * 2:
                self.canvas.create_rectangle(x1, y1 + radius, x2, y2, fill=C_ACCENT, outline="", width=0)
                self.canvas.create_oval(x1, y1, x2, y1 + radius * 2, fill=C_ACCENT, outline="", width=0)
            elif bar_h > 0:
                self.canvas.create_rectangle(x1, y1, x2, y2, fill=C_ACCENT, outline="", width=0)
                
            self.canvas.create_text((x1 + x2)/2, h - margin_bottom + 10, text=lbl, fill=C_GRY, font=(FNT_BODY, 9, "bold"))
            
            if val > 0:
                self.canvas.create_text((x1 + x2)/2, y1 - 8, text=f"{val:.0f} €", fill=C_TEXT, font=(FNT_BODY, 8, "bold"))


def build(parent):
    # Le conteneur parent (app principale) a déjà le fond C_BG. 
    # Pour éviter la double bordure, on utilise transparent ou C_BG
    frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
    frame.grid_rowconfigure(1, weight=1)
    frame.grid_columnconfigure(0, weight=1)

    # Header Apple Style (Large Title)
    hdr = ctk.CTkFrame(frame, fg_color="transparent")
    hdr.grid(row=0, column=0, sticky="ew", padx=40, pady=(40, 20))
    hdr.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(hdr, text="Tableau de Bord", font=ctk.CTkFont(FNT_TITLE, 34, "bold"),
                 text_color=C_TEXT).grid(row=0, column=0, sticky="w")

    now = datetime.datetime.now()

    def _do_export_comptable():
        from views.modals import ComptaReportingModal
        ComptaReportingModal(parent)

    right_hdr = ctk.CTkFrame(hdr, fg_color="transparent")
    right_hdr.grid(row=0, column=1, sticky="e")

    info = f"{now.strftime('%A %d %B %Y').capitalize()}   |   Session : {parent.vendeur_actif['nom']}"
    ctk.CTkLabel(right_hdr, text=info, font=ctk.CTkFont(FNT_BODY, 13), text_color=C_GRY).pack(side="left", padx=(0, 15))

    btn_exp = ctk.CTkButton(right_hdr, text="Export Comptable", height=38,
                            font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                            fg_color=C_SEC_BG, text_color=C_TEXT, corner_radius=19,
                            command=_do_export_comptable)
    btn_exp.pack(side="right")

    body = ctk.CTkFrame(frame, fg_color="transparent")
    body.grid(row=1, column=0, sticky="nsew", padx=40, pady=(0, 40))
    body.grid_rowconfigure(1, weight=1)
    body.grid_columnconfigure(0, weight=1)

    # ── KPI row — 5 cartes ───────────────────────────────────
    kpi_row = ctk.CTkFrame(body, fg_color="transparent")
    kpi_row.grid(row=0, column=0, sticky="ew", pady=(0, 30))
    for i in range(5):
        kpi_row.grid_columnconfigure(i, weight=1)

    kpi_widgets = {}

    def kpi_card(parent_w, col, key, label, value, accent=False, warn=False):
        bg = "#FFF5F5" if accent else ("#FFFAF0" if warn else C_SEC_BG)
        txt_col = C_ACCENT if accent else (C_RED if warn else C_TEXT)
        card = ctk.CTkFrame(parent_w, fg_color=bg, corner_radius=RAD)
        card.grid(row=0, column=col, sticky="nsew", padx=(0, 16) if col < 4 else 0)
        lbl_w = ctk.CTkLabel(card, text=label.upper(), font=ctk.CTkFont(FNT_BODY, 10, "bold"), text_color=C_GRY)
        lbl_w.pack(pady=(25, 5))
        val_w = ctk.CTkLabel(card, text=value, font=ctk.CTkFont(FNT_TITLE, 24, "bold"), text_color=txt_col)
        val_w.pack(pady=(0, 25))
        kpi_widgets[key] = (lbl_w, val_w)

    def on_period_changed(val):
        p_map = {"Aujourd'hui": "jour", "Ce Mois": "mois", "Cette Année": "annee", "Tout": "tout"}
        p_code = p_map.get(val, "jour")
        
        ca, nb_ventes, nb_articles, alertes = _get_kpis(p_code)
        dep = _get_depenses_periode(p_code)

        lbl_suffix_map = {"jour": "DU JOUR", "mois": "DU MOIS", "annee": "DE L'ANNÉE", "tout": "CUMULÉ"}
        suf = lbl_suffix_map[p_code]

        if "ca" in kpi_widgets:
            kpi_widgets["ca"][0].configure(text=f"CHIFFRE D'AFFAIRES ({suf})")
            kpi_widgets["ca"][1].configure(text=f"{ca} €")
        if "ventes" in kpi_widgets:
            kpi_widgets["ventes"][0].configure(text=f"VENTES ({suf})")
            kpi_widgets["ventes"][1].configure(text=str(nb_ventes))
        if "articles" in kpi_widgets:
            kpi_widgets["articles"][0].configure(text=f"ARTICLES VENDUS ({suf})")
            kpi_widgets["articles"][1].configure(text=str(nb_articles))
        if "depenses" in kpi_widgets:
            kpi_widgets["depenses"][0].configure(text=f"SORTIES CAISSE ({suf})")
            kpi_widgets["depenses"][1].configure(text=f"{dep:.2f} €")

    seg_period = ctk.CTkSegmentedButton(
        right_hdr, 
        values=["Aujourd'hui", "Ce Mois", "Cette Année", "Tout"],
        command=on_period_changed,
        font=ctk.CTkFont(FNT_BODY, 11, "bold"),
        selected_color=C_ACCENT,
        unselected_color=C_SEC_BG
    )
    seg_period.set("Aujourd'hui")
    seg_period.pack(side="left", padx=(0, 15))

    ca, nb_ventes, nb_articles, alertes = _get_kpis("jour")
    depenses_jour = _get_depenses_periode("jour")

    kpi_card(kpi_row, 0, "ca", "Chiffre d'Affaires (Du Jour)", f"{ca} €", accent=True)
    kpi_card(kpi_row, 1, "ventes", "Ventes (Du Jour)", str(nb_ventes))
    kpi_card(kpi_row, 2, "articles", "Articles Vendus (Du Jour)", str(nb_articles))
    kpi_card(kpi_row, 3, "depenses", "Sorties Caisse (Du Jour)", f"{depenses_jour:.2f} €", warn=(depenses_jour > 0))

    # ── Bottom split ─────────────────────────────────────────
    split = ctk.CTkFrame(body, fg_color="transparent")
    split.grid(row=1, column=0, sticky="nsew")
    split.grid_rowconfigure(0, weight=1)
    split.grid_columnconfigure(0, weight=7)
    split.grid_columnconfigure(1, weight=3)

    # ── Left: ventes récentes ─────────────────────────────────
    left = ctk.CTkFrame(split, fg_color=C_SEC_BG, corner_radius=RAD, border_width=0)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 20))
    left.grid_rowconfigure(2, weight=1)
    left.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(left, text="VENTES RÉCENTES", font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                 text_color=C_TEXT, anchor="w").grid(row=0, column=0, padx=24, pady=(24, 6), sticky="w")

    chart = WeeklySalesChart(left, height=220)
    chart.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 15))

    scroll = ctk.CTkScrollableFrame(left, fg_color="transparent", corner_radius=24)
    scroll.grid(row=2, column=0, sticky="nsew", padx=0, pady=0)
    scroll.grid_columnconfigure((0, 1, 2, 3), weight=1)

    ventes = _get_ventes_recentes()
    if not ventes:
        ctk.CTkLabel(scroll, text="Aucune vente aujourd'hui.",
                     font=ctk.CTkFont(FNT_BODY, 13), text_color=C_GRY
                     ).grid(row=0, column=0, columnspan=4, pady=40)
    for idx, (heure, ticket, methode, total) in enumerate(ventes):
        row_f = ctk.CTkFrame(scroll, fg_color="transparent")
        row_f.grid(row=idx * 2, column=0, columnspan=4, sticky="ew")
        row_f.grid_columnconfigure((0, 1, 2, 3), weight=1)
        for ci, val in enumerate([heure, ticket, methode, f"{total} €"]):
            ctk.CTkLabel(row_f, text=str(val), font=ctk.CTkFont(FNT_BODY, 12),
                         text_color=C_ACCENT if ci == 3 else C_TEXT, anchor="w"
                         ).grid(row=0, column=ci, padx=24, pady=12, sticky="w")
        ctk.CTkFrame(scroll, height=1, fg_color=C_LINE
                     ).grid(row=idx * 2 + 1, column=0, columnspan=4, sticky="ew", padx=10)

    # ── Right panel ──────────────────────────────
    right_f = ctk.CTkScrollableFrame(split, fg_color="transparent")
    right_f.grid(row=0, column=1, sticky="nsew")
    right_f.grid_columnconfigure(0, weight=1)

    # Section 1 : Répartition paiements
    pay_card = ctk.CTkFrame(right_f, fg_color=C_SEC_BG, corner_radius=RAD)
    pay_card.grid(row=0, column=0, sticky="nsew", pady=(0, 16))
    ctk.CTkLabel(pay_card, text="RÉPARTITION PAIEMENTS",
                 font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=C_GRY).pack(pady=(20, 10))
    pay_data = _get_repartition_paiements()
    for m, val in pay_data.items():
        lbl = m if m != "QR_Code" else "QR Code"
        r = ctk.CTkFrame(pay_card, fg_color="transparent")
        r.pack(fill="x", padx=24, pady=6)
        ctk.CTkLabel(r, text=lbl, font=ctk.CTkFont(FNT_BODY, 12), text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(r, text=f"{val:.2f} €", font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                     text_color=C_ACCENT).pack(side="right")
    ctk.CTkFrame(pay_card, height=8, fg_color="transparent").pack()

    # Section 2 : Dépenses du jour
    dep_card = ctk.CTkFrame(right_f, fg_color=C_SEC_BG, corner_radius=RAD, border_width=0)
    dep_card.grid(row=1, column=0, sticky="nsew")
    dep_card.grid_columnconfigure(0, weight=1)

    dep_hdr = ctk.CTkFrame(dep_card, fg_color="transparent")
    dep_hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
    ctk.CTkLabel(dep_hdr, text="DÉPENSES DU JOUR",
                 font=ctk.CTkFont(FNT_BODY, 12, "bold"), text_color=C_TEXT).pack(side="left")

    depenses_data = _get_depenses_details()
    if not depenses_data:
        ctk.CTkLabel(dep_card, text="Aucune dépense aujourd'hui.",
                     font=ctk.CTkFont(FNT_BODY, 12), text_color=C_GRY
                     ).grid(row=1, column=0, padx=24, pady=(0, 16))
    else:
        for i, (libelle, montant, moyen) in enumerate(depenses_data):
            dep_row = ctk.CTkFrame(dep_card, fg_color="transparent")
            dep_row.grid(row=i + 1, column=0, sticky="ew", padx=20, pady=3)
            ctk.CTkLabel(dep_row, text=libelle, font=ctk.CTkFont(FNT_BODY, 11),
                         text_color=C_TEXT, anchor="w").pack(side="left")
            ctk.CTkLabel(dep_row, text=f"−{float(montant):.2f} €",
                         font=ctk.CTkFont(FNT_BODY, 11, "bold"),
                         text_color=C_RED).pack(side="right")
        ctk.CTkFrame(dep_card, height=10, fg_color="transparent"
                     ).grid(row=len(depenses_data) + 1, column=0)

    # Section 3 : Rapports Comptables (PDF)
    rep_card = ctk.CTkFrame(right_f, fg_color=C_SEC_BG, corner_radius=RAD)
    rep_card.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
    
    ctk.CTkLabel(rep_card, text="RAPPORTS COMPTABLES (PDF)",
                 font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=C_GRY).pack(pady=(20, 10))

    type_var = ctk.StringVar(value="Mois")
    
    def _on_type_change(val):
        entry_val.delete(0, "end")
        if val == "Jour":
            entry_val.insert(0, datetime.date.today().isoformat())
        elif val == "Mois":
            entry_val.insert(0, datetime.date.today().strftime("%Y-%m"))
        elif val == "Année":
            entry_val.insert(0, datetime.date.today().strftime("%Y"))

    segmented_type = ctk.CTkSegmentedButton(rep_card, values=["Jour", "Mois", "Année"],
                                             variable=type_var, command=_on_type_change,
                                             font=ctk.CTkFont(FNT_BODY, 11, "bold"),
                                             selected_color=C_ACCENT, unselected_color=C_BG)
    segmented_type.pack(fill="x", padx=24, pady=5)

    ctk.CTkLabel(rep_card, text="Période (AAAA-MM-JJ, AAAA-MM ou AAAA)",
                 font=ctk.CTkFont(FNT_BODY, 11, "bold"), text_color=C_TEXT).pack(anchor="w", padx=24, pady=(10, 2))

    entry_val = ctk.CTkEntry(rep_card, placeholder_text="Ex: 2026-07-27",
                             font=ctk.CTkFont(FNT_BODY, 13), height=38,
                             fg_color=C_BG, border_width=0, corner_radius=10)
    entry_val.pack(fill="x", padx=24, pady=5)
    entry_val.insert(0, datetime.date.today().strftime("%Y-%m"))

    def _telecharger_rapport():
        t = type_var.get().lower().replace("année", "annee")
        v = entry_val.get().strip()
        if not v:
            if hasattr(parent, "_st"): parent._st("Période invalide.", C_RED)
            return
        
        default_name = f"rapport_recettes_{t}_{v}.pdf"
        
        from tkinter import filedialog
        file_path = filedialog.asksaveasfilename(parent=parent,
                                                 defaultextension=".pdf",
                                                 filetypes=[("Documents PDF", "*.pdf")],
                                                 initialfile=default_name,
                                                 title="Enregistrer le rapport comptable")
        if file_path:
            try:
                import pdf_generator
                pdf_generator.generer_rapport_pdf(t, v, file_path)
                if hasattr(parent, "_show_toast"):
                    parent._show_toast("Rapport PDF généré avec succès !", C_GRN)
                elif hasattr(parent, "_st"):
                    parent._st("Rapport PDF généré avec succès !", C_GRN)
            except Exception as e:
                if hasattr(parent, "_st"):
                    parent._st(f"Erreur génération PDF : {e}", C_RED)

    btn_dl = ctk.CTkButton(rep_card, text="Générer Rapport (PDF)", height=38,
                            font=ctk.CTkFont(FNT_BODY, 12, "bold"),
                            fg_color=C_ACCENT, text_color="#FFFFFF", corner_radius=19,
                            command=_telecharger_rapport)
    btn_dl.pack(fill="x", padx=24, pady=(15, 20))

    # ── Footer : Export & Clôture ─────────────────────────────
    export_f = ctk.CTkFrame(frame, height=80, fg_color="transparent", border_width=0)
    export_f.grid(row=2, column=0, sticky="ew")
    export_f.grid_propagate(False)

    inner_exp = ctk.CTkFrame(export_f, fg_color="transparent")
    inner_exp.pack(expand=True)

    def _ouvrir_compta():
        from views.modals import ComptaReportingModal
        ComptaReportingModal(parent)

    def _ouvrir_cloture():
        from views.modals import ClotureModal
        ClotureModal(parent)

    def _ouvrir_depense():
        from views.modals import DepenseCaisseModal
        vendeur_nom = parent.vendeur_actif['nom'] if parent.vendeur_actif else 'Inconnu'
        def _on_depense_saved():
            # Rafraîchir le dashboard après ajout d'une dépense
            parent.afficher_stats()
        DepenseCaisseModal(parent, vendeur_nom=vendeur_nom, callback=_on_depense_saved)

    ctk.CTkButton(inner_exp, text="+ Dépense", height=48, width=130,
                  font=ctk.CTkFont(FNT_BODY, 13, "bold"),
                  fg_color="transparent", text_color=C_TEXT, border_width=2, border_color=C_TEXT, corner_radius=24, command=_ouvrir_depense).pack(side="left", padx=10)

    ctk.CTkButton(inner_exp, text="Module Comptable Pro", height=48, width=220,
                  font=ctk.CTkFont(FNT_BODY, 14, "bold"),
                  fg_color=C_TEXT, text_color=C_BG, corner_radius=24, command=_ouvrir_compta).pack(side="left", padx=10)

    ctk.CTkButton(inner_exp, text="Clôture Jour (Z)", height=48, width=200,
                  font=ctk.CTkFont(FNT_BODY, 14, "bold"),
                  fg_color=C_ACCENT, text_color=C_BG, corner_radius=24, command=_ouvrir_cloture).pack(side="left", padx=10)

    return frame


# ── Requêtes SQL ─────────────────────────────────────────────

def _get_kpis(periode="jour"):
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        
        where_clause = "WHERE date(t.date_heure) = date('now', 'localtime')"
        if periode == "mois":
            where_clause = "WHERE strftime('%Y-%m', t.date_heure) = strftime('%Y-%m', 'now', 'localtime')"
        elif periode == "annee":
            where_clause = "WHERE strftime('%Y', t.date_heure) = strftime('%Y', 'now', 'localtime')"
        elif periode == "tout":
            where_clause = ""

        # CA et Nombre de tickets directement sur Tickets (sans jointure pour éviter la duplication)
        c.execute(f"SELECT COALESCE(SUM(t.total_tvac), 0.0), COUNT(t.id) FROM Tickets t {where_clause}")
        ca_row = c.fetchone()
        ca = ca_row[0] if ca_row else 0.0
        nb = ca_row[1] if ca_row else 0

        # Nombre total d'articles vendus via jointure sur Ventes_Details
        c.execute(f"SELECT COALESCE(SUM(vd.quantite), 0) FROM Ventes_Details vd JOIN Tickets t ON t.id = vd.id_ticket {where_clause}")
        art_row = c.fetchone()
        art = art_row[0] if art_row else 0
        
        c.execute("SELECT COUNT(*) FROM Stocks WHERE quantite_actuelle <= seuil_alerte")
        alr = c.fetchone()[0]
        
        return str(Decimal(str(ca or 0)).quantize(Decimal("0.01"))), nb or 0, art or 0, alr or 0
    except Exception as e:
        print(f"[STATS] Erreur KPIs: {e}")
        return "0.00", 0, 0, 0
    finally:
        if conn:
            conn.close()


def _get_depenses_periode(periode="jour"):
    """Total des dépenses selon la période."""
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        if periode == "jour":
            c.execute("SELECT COALESCE(SUM(montant), 0) FROM Depenses_Caisse WHERE date(date_heure)=date('now', 'localtime')")
        elif periode == "mois":
            c.execute("SELECT COALESCE(SUM(montant), 0) FROM Depenses_Caisse WHERE strftime('%Y-%m', date_heure)=strftime('%Y-%m', 'now', 'localtime')")
        elif periode == "annee":
            c.execute("SELECT COALESCE(SUM(montant), 0) FROM Depenses_Caisse WHERE strftime('%Y', date_heure)=strftime('%Y', 'now', 'localtime')")
        else:
            c.execute("SELECT COALESCE(SUM(montant), 0) FROM Depenses_Caisse")
        total = c.fetchone()[0] or 0.0
        return float(total)
    except Exception as e:
        print(f"[STATS] Erreur dépenses: {e}")
        return 0.0
    finally:
        if conn:
            conn.close()


def _get_depenses_jour():
    return _get_depenses_periode("jour")


def _get_depenses_details():
    """Détail des dépenses de la journée."""
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        today = datetime.date.today().isoformat()
        c.execute("""SELECT libelle, montant, moyen_paiement FROM Depenses_Caisse
                     WHERE date(date_heure)=? ORDER BY date_heure DESC""", (today,))
        rows = c.fetchall()
        return rows
    except Exception as e:
        print(f"[STATS] Erreur détails dépenses: {e}")
        return []
    finally:
        if conn:
            conn.close()


def _get_ventes_recentes():
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("""SELECT strftime('%H:%M', date_heure), numero_ticket,
                            methode_paiement, total_tvac
                     FROM Tickets ORDER BY date_heure DESC LIMIT 20""")
        rows = c.fetchall()
        return rows
    except Exception as e:
        print(f"[STATS] Erreur ventes récentes: {e}")
        return []
    finally:
        if conn:
            conn.close()


def _get_repartition_paiements():
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        today = datetime.date.today().isoformat()
        c.execute("""SELECT methode_paiement, SUM(total_tvac) FROM Tickets
                     WHERE date(date_heure)=? GROUP BY methode_paiement""", (today,))
        rows = c.fetchall()
        res = {"Bancontact": 0.0, "Espèces": 0.0, "QR Code": 0.0, "Shopify": 0.0}
        for m, val in rows:
            key = "QR Code" if m == "QR_Code" else m
            if key in res:
                res[key] = float(val)
        return res
    except Exception as e:
        print(f"[STATS] Erreur répartition paiements: {e}")
        return {}
    finally:
        if conn:
            conn.close()


def _get_alertes_stock():
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        c.execute("""SELECT p.nom, s.taille, s.quantite_actuelle
                     FROM Stocks s JOIN Produits p ON p.id=s.id_produit
                     WHERE s.quantite_actuelle <= s.seuil_alerte
                     ORDER BY s.quantite_actuelle ASC""")
        rows = c.fetchall()
        return rows
    except Exception as e:
        print(f"[STATS] Erreur alertes stock: {e}")
        return []
    finally:
        if conn:
            conn.close()


def _get_sales_last_7_days():
    import datetime
    conn = None
    try:
        conn = get_connection()
        c = conn.cursor()
        data = []
        for i in range(6, -1, -1):
            d = (datetime.date.today() - datetime.timedelta(days=i))
            d_str = d.isoformat()
            c.execute("SELECT COALESCE(SUM(total_tvac), 0.0) FROM Tickets WHERE date(date_heure)=?", (d_str,))
            val = float(c.fetchone()[0])
            day_lbl = d.strftime("%a").capitalize()[:3]
            # Convertir en français pour harmoniser
            fr_days = {"Mon": "Lun", "Tue": "Mar", "Wed": "Mer", "Thu": "Jeu", "Fri": "Ven", "Sat": "Sam", "Sun": "Dim"}
            day_lbl = fr_days.get(day_lbl, day_lbl)
            data.append((day_lbl, val))
        return data
    except Exception as e:
        print(f"[STATS CHART] Erreur : {e}")
        return []
    finally:
        if conn:
            conn.close()
