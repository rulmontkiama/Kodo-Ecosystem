import sqlite3
import os
import datetime
from decimal import Decimal
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from database_manager import get_connection

# Palette de couleurs Kōdo POS (DA Apple Chic / Minimaliste)
C_PRIMARY = colors.HexColor("#1D1D1F")   # Noir Apple
C_CORAL   = colors.HexColor("#FF7F7F")   # Coral Kōdo Accent
C_SECONDARY = colors.HexColor("#86868B") # Gris Apple
C_LIGHT_BG = colors.HexColor("#F5F5F7")  # Gris perle arrière-plan
C_WHITE    = colors.HexColor("#FFFFFF")

class NumberedCanvas(canvas.Canvas):
    """Canvas personnalisé pour ajouter les numéros de page et un bas de page professionnel."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_footer(num_pages)
            super().showPage()
        super().save()

    def draw_footer(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(C_SECONDARY)
        
        # Ligne de séparation
        self.setStrokeColor(colors.HexColor("#E5E5EA"))
        self.setLineWidth(0.5)
        self.line(1.5 * cm, 1.2 * cm, A4[0] - 1.5 * cm, 1.2 * cm)
        
        # Informations bas de page
        self.drawString(1.5 * cm, 0.8 * cm, "Rapport de comptabilité officiel — Kōdo POS")
        page_text = f"Page {self._pageNumber} sur {page_count}"
        self.drawRightString(A4[0] - 1.5 * cm, 0.8 * cm, page_text)
        self.restoreState()


def get_param(c, key, default=""):
    c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (key,))
    row = c.fetchone()
    return row[0] if row else default


def generer_rapport_pdf(type_rapport, date_val, save_path):
    """
    Génère un rapport de recettes PDF professionnel.
    - type_rapport: "jour", "mois", "annee"
    - date_val: "YYYY-MM-DD", "YYYY-MM", "YYYY"
    """
    # 1. Calcul de l'intervalle temporel pour exploiter les index
    if type_rapport == "jour":
        start_date = f"{date_val} 00:00:00"
        end_date = f"{date_val} 23:59:59"
        titre_periode = f"du {datetime.datetime.strptime(date_val, '%Y-%m-%d').strftime('%d/%m/%Y')}"
    elif type_rapport == "mois":
        start_date = f"{date_val}-01 00:00:00"
        y, m = map(int, date_val.split("-"))
        if m == 12:
            y_next, m_next = y + 1, 1
        else:
            y_next, m_next = y, m + 1
        end_date = f"{y_next:04d}-{m_next:02d}-01 00:00:00"
        titre_periode = f"de {datetime.datetime(y, m, 1).strftime('%B %Y')}"
    elif type_rapport == "annee":
        start_date = f"{date_val}-01-01 00:00:00"
        end_date = f"{int(date_val)+1:04d}-01-01 00:00:00"
        titre_periode = f"de l'année {date_val}"
    else:
        raise ValueError("Type de rapport invalide.")

    # 2. Récupération des données depuis la base SQLite
    conn = get_connection()
    c = conn.cursor()

    # Infos boutique
    shop_name = get_param(c, "shop_name", "L'ADRESSE B")
    shop_subtitle = get_param(c, "shop_subtitle", "Boutique de Mode")
    shop_address = get_param(c, "shop_address", "")
    shop_vat = get_param(c, "shop_vat", "")

    # Totaux CA
    if type_rapport == "jour":
        c.execute("""
            SELECT 
                COALESCE(SUM(total_tvac), 0.0),
                COALESCE(SUM(total_htva), 0.0),
                COALESCE(SUM(total_tva), 0.0),
                COUNT(id)
            FROM Tickets
            WHERE date_heure >= ? AND date_heure <= ?
        """, (start_date, end_date))
    else:
        c.execute("""
            SELECT 
                COALESCE(SUM(total_tvac), 0.0),
                COALESCE(SUM(total_htva), 0.0),
                COALESCE(SUM(total_tva), 0.0),
                COUNT(id)
            FROM Tickets
            WHERE date_heure >= ? AND date_heure < ?
        """, (start_date, end_date))
    ca_tvac, ca_htva, ca_tva, nb_tickets = c.fetchone()
    
    ca_tvac = Decimal(str(ca_tvac))
    ca_htva = Decimal(str(ca_htva))
    ca_tva = Decimal(str(ca_tva))

    # Répartition par mode de paiement
    if type_rapport == "jour":
        c.execute("""
            SELECT methode_paiement, SUM(total_tvac)
            FROM Tickets
            WHERE date_heure >= ? AND date_heure <= ?
            GROUP BY methode_paiement
        """, (start_date, end_date))
    else:
        c.execute("""
            SELECT methode_paiement, SUM(total_tvac)
            FROM Tickets
            WHERE date_heure >= ? AND date_heure < ?
            GROUP BY methode_paiement
        """, (start_date, end_date))
    paiements_data = c.fetchall()

    # Dépenses de caisse associées (Détail)
    if type_rapport == "jour":
        c.execute("""
            SELECT date_heure, libelle, montant, moyen_paiement
            FROM Depenses_Caisse
            WHERE date_heure >= ? AND date_heure <= ?
            ORDER BY date_heure ASC
        """, (start_date, end_date))
    else:
        c.execute("""
            SELECT date_heure, libelle, montant, moyen_paiement
            FROM Depenses_Caisse
            WHERE date_heure >= ? AND date_heure < ?
            ORDER BY date_heure ASC
        """, (start_date, end_date))
    depenses_list = c.fetchall()
    total_depenses = sum(Decimal(str(r[2])) for r in depenses_list) if depenses_list else Decimal("0.00")

    # Liste des transactions / tickets sur la période
    if type_rapport == "jour":
        c.execute("""
            SELECT date_heure, numero_ticket, methode_paiement, total_tvac
            FROM Tickets
            WHERE date_heure >= ? AND date_heure <= ?
            ORDER BY date_heure ASC
        """, (start_date, end_date))
    else:
        c.execute("""
            SELECT date_heure, numero_ticket, methode_paiement, total_tvac
            FROM Tickets
            WHERE date_heure >= ? AND date_heure < ?
            ORDER BY date_heure ASC
        """, (start_date, end_date))
    tickets_list = c.fetchall()

    # Liste détaillée de TOUS les articles vendus
    if type_rapport == "jour":
        c.execute("""
            SELECT 
                t.numero_ticket,
                t.date_heure,
                COALESCE(p.code_barre, 'N/A') AS ref_code,
                COALESCE(p.nom, 'Article inconnu') AS designation,
                COALESCE(s.taille, '') AS taille,
                vd.quantite,
                vd.prix_unitaire_tvac,
                (vd.quantite * vd.prix_unitaire_tvac) AS total_tvac
            FROM Ventes_Details vd
            JOIN Tickets t ON vd.id_ticket = t.id
            LEFT JOIN Stocks s ON vd.id_stock = s.id
            LEFT JOIN Produits p ON s.id_produit = p.id
            WHERE t.date_heure >= ? AND t.date_heure <= ?
            ORDER BY t.date_heure ASC, t.numero_ticket ASC
        """, (start_date, end_date))
    else:
        c.execute("""
            SELECT 
                t.numero_ticket,
                t.date_heure,
                COALESCE(p.code_barre, 'N/A') AS ref_code,
                COALESCE(p.nom, 'Article inconnu') AS designation,
                COALESCE(s.taille, '') AS taille,
                vd.quantite,
                vd.prix_unitaire_tvac,
                (vd.quantite * vd.prix_unitaire_tvac) AS total_tvac
            FROM Ventes_Details vd
            JOIN Tickets t ON vd.id_ticket = t.id
            LEFT JOIN Stocks s ON vd.id_stock = s.id
            LEFT JOIN Produits p ON s.id_produit = p.id
            WHERE t.date_heure >= ? AND t.date_heure < ?
            ORDER BY t.date_heure ASC, t.numero_ticket ASC
        """, (start_date, end_date))
    articles_list = c.fetchall()

    # Synthèse cumulée des ventes par produit
    if type_rapport == "jour":
        c.execute("""
            SELECT 
                COALESCE(p.code_barre, 'N/A'),
                COALESCE(p.nom, 'Article inconnu'),
                COALESCE(s.taille, '-'),
                SUM(vd.quantite) AS qte_totale,
                SUM(vd.quantite * vd.prix_unitaire_tvac) AS ca_total
            FROM Ventes_Details vd
            JOIN Tickets t ON vd.id_ticket = t.id
            LEFT JOIN Stocks s ON vd.id_stock = s.id
            LEFT JOIN Produits p ON s.id_produit = p.id
            WHERE t.date_heure >= ? AND t.date_heure <= ?
            GROUP BY p.id, s.taille
            ORDER BY qte_totale DESC
        """, (start_date, end_date))
    else:
        c.execute("""
            SELECT 
                COALESCE(p.code_barre, 'N/A'),
                COALESCE(p.nom, 'Article inconnu'),
                COALESCE(s.taille, '-'),
                SUM(vd.quantite) AS qte_totale,
                SUM(vd.quantite * vd.prix_unitaire_tvac) AS ca_total
            FROM Ventes_Details vd
            JOIN Tickets t ON vd.id_ticket = t.id
            LEFT JOIN Stocks s ON vd.id_stock = s.id
            LEFT JOIN Produits p ON s.id_produit = p.id
            WHERE t.date_heure >= ? AND t.date_heure < ?
            GROUP BY p.id, s.taille
            ORDER BY qte_totale DESC
        """, (start_date, end_date))
    produits_cumul_list = c.fetchall()

    conn.close()

    # 3. Création du document PDF avec ReportLab
    doc = SimpleDocTemplate(
        save_path,
        pagesize=A4,
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm
    )

    styles = getSampleStyleSheet()
    
    # Définition de styles personnalisés conformes à la DA
    style_shop_title = ParagraphStyle(
        'ShopTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=C_PRIMARY
    )
    style_shop_sub = ParagraphStyle(
        'ShopSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=C_SECONDARY
    )
    style_report_title = ParagraphStyle(
        'ReportTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=C_PRIMARY,
        spaceAfter=4
    )
    style_report_sub = ParagraphStyle(
        'ReportSub',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=11,
        leading=15,
        textColor=C_SECONDARY,
        spaceAfter=15
    )
    style_h2 = ParagraphStyle(
        'Heading2_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=13,
        leading=17,
        textColor=C_PRIMARY,
        spaceBefore=12,
        spaceAfter=6
    )
    style_kpi_num = ParagraphStyle(
        'KPINum',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=19,
        textColor=C_CORAL,
        alignment=1 # Centré
    )
    style_kpi_label = ParagraphStyle(
        'KPILabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=C_PRIMARY,
        alignment=1 # Centré
    )
    style_cell_text = ParagraphStyle(
        'CellText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=C_PRIMARY
    )
    style_cell_text_bold = ParagraphStyle(
        'CellTextBold',
        parent=style_cell_text,
        fontName='Helvetica-Bold'
    )
    style_cell_right = ParagraphStyle(
        'CellRight',
        parent=style_cell_text,
        alignment=2 # Droite
    )
    style_cell_right_bold = ParagraphStyle(
        'CellRightBold',
        parent=style_cell_text_bold,
        alignment=2 # Droite
    )

    story = []

    # ── EN-TÊTE DU RAPPORT ──────────────────────────────────
    header_data = [
        [
            Paragraph(f"<b>{shop_name}</b><br/>{shop_subtitle}", style_shop_title if len(shop_name) < 15 else style_shop_sub),
            Paragraph(f"Adresse : {shop_address}<br/>TVA : {shop_vat}", style_shop_sub)
        ]
    ]
    header_table = Table(header_data, colWidths=[9*cm, 9*cm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(header_table)
    
    # Ligne de séparation supérieure
    sep_table = Table([[""]], colWidths=[18*cm], rowHeights=[1])
    sep_table.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA")),
        ('BOTTOMPADDING', (0,0), (-1,-1), 15),
    ]))
    story.append(sep_table)
    story.append(Spacer(1, 0.4*cm))

    # Titre principal du rapport
    story.append(Paragraph(f"Rapport Financier & Ventes Détaillées", style_report_title))
    story.append(Paragraph(f"Période {titre_periode} &bull; Généré le {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}", style_report_sub))

    # ── CARTES KPI (Blocs statistiques de synthèse) ────────
    kpi_card_style = TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), C_LIGHT_BG),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('INNERGRID', (0,0), (-1,-1), 0.5, C_WHITE),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA")),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ])

    kpi_data = [
        [
            Paragraph("RECETTES TVAC", style_kpi_label),
            Paragraph("REVENUS HTVA", style_kpi_label),
            Paragraph("TVA COLLECTÉE", style_kpi_label),
            Paragraph("DÉPENSES CAISSE", style_kpi_label)
        ],
        [
            Paragraph(f"<b>{ca_tvac:,.2f} €</b>".replace(",", " "), style_kpi_num),
            Paragraph(f"<b>{ca_htva:,.2f} €</b>".replace(",", " "), style_kpi_num),
            Paragraph(f"<b>{ca_tva:,.2f} €</b>".replace(",", " "), style_kpi_num),
            Paragraph(f"<b>{total_depenses:,.2f} €</b>".replace(",", " "), style_kpi_num)
        ]
    ]
    
    kpi_table = Table(kpi_data, colWidths=[4.5*cm, 4.5*cm, 4.5*cm, 4.5*cm])
    kpi_table.setStyle(kpi_card_style)
    story.append(kpi_table)
    story.append(Spacer(1, 0.6*cm))

    # ── RÉPARTITION DES RECETTES ─────────────────────────────
    story.append(Paragraph("Répartition par Mode de Règlement", style_h2))
    
    pay_rows = [
        [Paragraph("<b>Mode de Paiement</b>", style_cell_text_bold), Paragraph("<b>Montant Encaissé (€)</b>", style_cell_right_bold)]
    ]
    
    total_verif = Decimal("0.00")
    for m, val in paiements_data:
        val_dec = Decimal(str(val))
        total_verif += val_dec
        lbl = "Paiement Mobile / QR" if m == "QR_Code" else ("Carte / Bancontact" if m == "Bancontact" else m)
        pay_rows.append([
            Paragraph(lbl, style_cell_text),
            Paragraph(f"{val_dec:,.2f}".replace(",", " "), style_cell_right)
        ])
    
    pay_rows.append([
        Paragraph("<b>TOTAL DES ENCAISSEMENTS</b>", style_cell_text_bold),
        Paragraph(f"<b>{total_verif:,.2f} €</b>".replace(",", " "), style_cell_right_bold)
    ])

    pay_table = Table(pay_rows, colWidths=[10*cm, 8*cm])
    pay_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), C_LIGHT_BG),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA")),
        ('BACKGROUND', (0,-1), (-1,-1), C_LIGHT_BG),
    ]))
    story.append(pay_table)
    story.append(Spacer(1, 0.6*cm))

    # ── JOURNAL DES DÉPENSES DE CAISSE ──────────────────────
    if depenses_list:
        story.append(Paragraph(f"Journal des Dépenses de Caisse ({len(depenses_list)} sortie(s))", style_h2))
        dep_rows = [
            [
                Paragraph("<b>Date / Heure</b>", style_cell_text_bold),
                Paragraph("<b>Motif / Libellé</b>", style_cell_text_bold),
                Paragraph("<b>Mode</b>", style_cell_text_bold),
                Paragraph("<b>Montant (€)</b>", style_cell_right_bold)
            ]
        ]
        for dh, lib, mont, moy in depenses_list:
            dh_str = str(dh)
            dh_fmt = dh_str[:16] if len(dh_str) >= 16 else dh_str
            dep_rows.append([
                Paragraph(dh_fmt, style_cell_text),
                Paragraph(lib, style_cell_text),
                Paragraph(moy, style_cell_text),
                Paragraph(f"−{Decimal(str(mont)):,.2f}".replace(",", " "), style_cell_right)
            ])
        dep_rows.append([
            Paragraph("<b>TOTAL DÉPENSES</b>", style_cell_text_bold),
            Paragraph("", style_cell_text),
            Paragraph("", style_cell_text),
            Paragraph(f"<b>−{total_depenses:,.2f} €</b>".replace(",", " "), style_cell_right_bold)
        ])
        dep_table = Table(dep_rows, colWidths=[3.5*cm, 7.5*cm, 3.5*cm, 3.5*cm])
        dep_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), C_LIGHT_BG),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E1E1E6")),
            ('BACKGROUND', (0,-1), (-1,-1), C_LIGHT_BG),
        ]))
        story.append(dep_table)
        story.append(Spacer(1, 0.6*cm))

    # ── DÉTAIL COMPLET DES ARTICLES VENDUS (TICKET PAR TICKET) ───
    if articles_list:
        story.append(Paragraph(f"Détail Complet des Articles Vendus ({len(articles_list)} ligne(s))", style_h2))
        art_rows = [
            [
                Paragraph("<b>N° Ticket</b>", style_cell_text_bold),
                Paragraph("<b>Heure</b>", style_cell_text_bold),
                Paragraph("<b>Référence / EAN</b>", style_cell_text_bold),
                Paragraph("<b>Désignation & Variante</b>", style_cell_text_bold),
                Paragraph("<b>Qté</b>", style_cell_right_bold),
                Paragraph("<b>P.U. (€)</b>", style_cell_right_bold),
                Paragraph("<b>Total TVAC (€)</b>", style_cell_right_bold)
            ]
        ]
        tot_qte = 0
        tot_ca_articles = Decimal("0.00")
        for num_tck, dh, ref, nom, taille, qte, pu, tot in articles_list:
            dh_str = str(dh)
            if isinstance(dh, datetime.datetime):
                h_str = dh.strftime("%H:%M")
            elif len(dh_str) > 10:
                try: h_str = datetime.datetime.strptime(dh_str.split(".")[0], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
                except: h_str = dh_str
            else: h_str = dh_str

            desig_str = f"{nom} (Taille: {taille})" if taille else nom
            qte_int = int(qte)
            tot_dec = Decimal(str(tot))
            tot_qte += qte_int
            tot_ca_articles += tot_dec

            art_rows.append([
                Paragraph(num_tck, style_cell_text),
                Paragraph(h_str, style_cell_text),
                Paragraph(ref, style_cell_text),
                Paragraph(desig_str, style_cell_text),
                Paragraph(str(qte_int), style_cell_right),
                Paragraph(f"{Decimal(str(pu)):,.2f}".replace(",", " "), style_cell_right),
                Paragraph(f"{tot_dec:,.2f}".replace(",", " "), style_cell_right)
            ])

        art_rows.append([
            Paragraph("<b>TOTAL ARTICLES VENDUS</b>", style_cell_text_bold),
            Paragraph("", style_cell_text),
            Paragraph("", style_cell_text),
            Paragraph("", style_cell_text),
            Paragraph(f"<b>{tot_qte}</b>", style_cell_right_bold),
            Paragraph("", style_cell_right),
            Paragraph(f"<b>{tot_ca_articles:,.2f} €</b>".replace(",", " "), style_cell_right_bold)
        ])

        art_table = Table(art_rows, colWidths=[2.8*cm, 1.5*cm, 2.7*cm, 5.5*cm, 1.2*cm, 2.1*cm, 2.2*cm])
        art_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), C_LIGHT_BG),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E1E1E6")),
            ('BACKGROUND', (0,-1), (-1,-1), C_LIGHT_BG),
        ]))
        story.append(art_table)
        story.append(Spacer(1, 0.6*cm))

    # ── SYNTHÈSE CUMULÉE PAR ARTICLE ───────────────────────
    if produits_cumul_list:
        story.append(Paragraph(f"Récapitulatif Cumulé des Ventes par Produit", style_h2))
        prod_rows = [
            [
                Paragraph("<b>Référence / EAN</b>", style_cell_text_bold),
                Paragraph("<b>Désignation Produit</b>", style_cell_text_bold),
                Paragraph("<b>Variante / Taille</b>", style_cell_text_bold),
                Paragraph("<b>Quantité Totale</b>", style_cell_right_bold),
                Paragraph("<b>CA Généré TVAC (€)</b>", style_cell_right_bold)
            ]
        ]
        tot_qte_cumul = 0
        tot_ca_cumul = Decimal("0.00")
        for ref, nom, taille, qte_sum, ca_sum in produits_cumul_list:
            q_val = int(qte_sum)
            ca_val = Decimal(str(ca_sum))
            tot_qte_cumul += q_val
            tot_ca_cumul += ca_val
            prod_rows.append([
                Paragraph(ref, style_cell_text),
                Paragraph(nom, style_cell_text),
                Paragraph(taille, style_cell_text),
                Paragraph(str(q_val), style_cell_right),
                Paragraph(f"{ca_val:,.2f}".replace(",", " "), style_cell_right)
            ])

        prod_rows.append([
            Paragraph("<b>TOTAL CUMULÉ</b>", style_cell_text_bold),
            Paragraph("", style_cell_text),
            Paragraph("", style_cell_text),
            Paragraph(f"<b>{tot_qte_cumul}</b>", style_cell_right_bold),
            Paragraph(f"<b>{tot_ca_cumul:,.2f} €</b>".replace(",", " "), style_cell_right_bold)
        ])

        prod_table = Table(prod_rows, colWidths=[3.5*cm, 7.5*cm, 2.5*cm, 2.0*cm, 2.5*cm])
        prod_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), C_LIGHT_BG),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E1E1E6")),
            ('BACKGROUND', (0,-1), (-1,-1), C_LIGHT_BG),
        ]))
        story.append(prod_table)
        story.append(Spacer(1, 0.6*cm))

    # ── JOURNAL DES TRANSACTIONS SYNTHÉTIQUE ─────────────────
    story.append(Paragraph(f"Journal des Tickets de Caisse ({len(tickets_list)} ticket(s))", style_h2))
    
    tck_rows = [
        [
            Paragraph("<b>Heure</b>", style_cell_text_bold),
            Paragraph("<b>N° Ticket</b>", style_cell_text_bold),
            Paragraph("<b>Mode de Règlement</b>", style_cell_text_bold),
            Paragraph("<b>Montant TVAC (€)</b>", style_cell_right_bold)
        ]
    ]

    for date_h, num, method, total in tickets_list:
        lbl_m = "Mobile / QR" if method == "QR_Code" else ("Carte" if method == "Bancontact" else method)
        date_h_str = str(date_h)
        if isinstance(date_h, datetime.datetime):
            h_str = date_h.strftime("%H:%M")
        elif len(date_h_str) > 10:
            try:
                h_str = datetime.datetime.strptime(date_h_str.split(".")[0], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
            except Exception:
                h_str = date_h_str
        else:
            h_str = date_h_str
        tck_rows.append([
            Paragraph(h_str, style_cell_text),
            Paragraph(num, style_cell_text),
            Paragraph(lbl_m, style_cell_text),
            Paragraph(f"{Decimal(str(total)):,.2f}".replace(",", " "), style_cell_right)
        ])

    tck_table = Table(tck_rows, colWidths=[3*cm, 5*cm, 5*cm, 5*cm])
    tck_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), C_LIGHT_BG),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E1E1E6")),
    ]))
    
    story.append(tck_table)

    # Construire le PDF
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Rapport PDF généré : {save_path}")
    return save_path



def generer_etiquettes_pdf(nom, code_barre, taille, prix, prix_solde, qte, output_path):
    """
    Génère un PDF d'étiquettes de prix de prêt-à-porter avec code-barres.
    - Format de page : 6cm x 3.5cm (étiquettes adhésives standard).
    - Chaque étiquette occupe une page.
    """
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics.barcode.code128 import Code128BarcodeWidget
    from reportlab.lib import colors

    # Définir des marges minimales de 2mm pour maximiser l'espace utile
    doc = SimpleDocTemplate(
        output_path,
        pagesize=(6 * cm, 3.5 * cm),
        leftMargin=0.2 * cm,
        rightMargin=0.2 * cm,
        topMargin=0.2 * cm,
        bottomMargin=0.2 * cm
    )

    styles = getSampleStyleSheet()
    
    style_shop = ParagraphStyle(
        'EtiquetteShop',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=9,
        alignment=1, # Centré
        textColor=colors.HexColor("#1D1D1F")
    )
    
    style_name = ParagraphStyle(
        'EtiquetteNom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7,
        leading=8,
        alignment=1, # Centré
        textColor=colors.HexColor("#1D1D1F")
    )
    
    style_price = ParagraphStyle(
        'EtiquettePrix',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=10,
        alignment=1, # Centré
        textColor=colors.HexColor("#FF7F7F") if prix_solde else colors.HexColor("#1D1D1F")
    )

    story = []
    barcode_text = code_barre or "000000000000"
    
    for page_idx in range(qte):
        story.append(Paragraph("L'ADRESSE B", style_shop))
        story.append(Spacer(1, 2))
        
        taille_suffix = f" ({taille})" if taille and taille != "—" else ""
        nom_complet = f"{nom}{taille_suffix}"
        if len(nom_complet) > 35:
            nom_complet = nom_complet[:32] + "..."
        story.append(Paragraph(nom_complet, style_name))
        story.append(Spacer(1, 3))
        
        try:
            barcode = Code128BarcodeWidget(barcode_text, barHeight=22, barWidth=0.8, humanReadable=True)
            barcode.fontSize = 6
            bounds = barcode.getBounds()
            bc_w = bounds[2] - bounds[0]
            bc_h = bounds[3] - bounds[1]
            
            d = Drawing(160, bc_h)
            offset_x = (160 - bc_w) / 2
            barcode.x = offset_x
            barcode.y = 0
            d.add(barcode)
            story.append(d)
        except Exception as e:
            print(f"[ETIQUETTE PDF] Erreur génération code-barres : {e}")
            story.append(Spacer(1, 24))
            
        story.append(Spacer(1, 3))
        
        if prix_solde:
            p_orig = Decimal(str(prix)).quantize(Decimal("0.01"))
            p_solde = Decimal(str(prix_solde)).quantize(Decimal("0.01"))
            txt_price = f"<font color='#86868B'><s>{p_orig:.2f} €</s></font>  <b><font color='#FF3B30'>{p_solde:.2f} € SOLDE</font></b>"
            story.append(Paragraph(txt_price, style_price))
        else:
            p_reg = Decimal(str(prix)).quantize(Decimal("0.01"))
            story.append(Paragraph(f"<b>{p_reg:.2f} €</b>", style_price))
            
        if page_idx < qte - 1:
            story.append(PageBreak())
            
    doc.build(story)
    print(f"Étiquettes PDF générées ({qte} page(s)) : {output_path}")
    return output_path
