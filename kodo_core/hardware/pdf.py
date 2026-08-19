"""
Générateur de PDF vectoriels via ReportLab pour Kōdo POS.
Supporte le bilan Z (jour/mois/année), reçus A4/A5/ticket, factures vectorielles et étiquettes avec codes-barres (EAN13, Code128, QR Code).
"""
import os
import sqlite3
import datetime
from decimal import Decimal

from reportlab.lib.pagesizes import A4, A5
from reportlab.lib import colors
from reportlab.lib.units import cm, mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing

# Palette de couleurs Kōdo POS (Apple Chic / Minimaliste)
C_PRIMARY   = colors.HexColor("#1D1D1F")  # Noir Apple
C_CORAL     = colors.HexColor("#FF7F7F")  # Coral Kōdo Accent
C_SECONDARY = colors.HexColor("#86868B")  # Gris Apple
C_LIGHT_BG  = colors.HexColor("#F5F5F7")  # Gris perle arrière-plan
C_WHITE     = colors.HexColor("#FFFFFF")


def generate_barcode_drawing(barcode_type, value, width=160, height=40):
    """
    Génère un Drawing ReportLab contenant un code-barres vectoriel (Code128, EAN13, QR).
    """
    try:
        from reportlab.graphics.barcode import createBarcodeDrawing
        b_type = barcode_type.upper()
        val_str = str(value or "000000000000")
        
        if b_type in ("EAN13", "EAN-13"):
            # Ajustement longueur EAN13 si nécessaire (12 ou 13 chiffres)
            if len(val_str) < 12:
                val_str = val_str.zfill(12)
            d = createBarcodeDrawing('EAN13', value=val_str, width=width, height=height)
        elif b_type in ("QR", "QRCODE"):
            d = createBarcodeDrawing('QR', value=val_str, width=width, height=height)
        else:  # Par défaut Code128
            d = createBarcodeDrawing('Code128', value=val_str, width=width, height=height)
        return d
    except Exception as e:
        print(f"[BARCODE VECTOR] Warning: impossible de générer le code-barres {barcode_type} ({e})")
        d = Drawing(width, height)
        return d


class NumberedCanvas(canvas.Canvas):
    """Canvas personnalisé avec numérotation de page dynamique et bas de page officiel."""
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
        page_w = self._pagesize[0] if hasattr(self, '_pagesize') else A4[0]
        self.line(1.5 * cm, 1.2 * cm, page_w - 1.5 * cm, 1.2 * cm)
        
        # Informations bas de page
        self.drawString(1.5 * cm, 0.8 * cm, "Document officiel Kōdo POS — Systèmes de caisse certifiés")
        page_text = f"Page {self._pageNumber} sur {page_count}"
        self.drawRightString(page_w - 1.5 * cm, 0.8 * cm, page_text)
        self.restoreState()


def get_param(c, key, default=""):
    c.execute("SELECT valeur FROM Parametres WHERE cle = ?", (key,))
    row = c.fetchone()
    return row[0] if row else default


# ---------------------------------------------------------------------------
# 1. RAPPORT COMPTABILITÉ ET BILAN Z (JOUR / MOIS / ANNÉE)
# ---------------------------------------------------------------------------
def generer_rapport_pdf(type_rapport, date_val, save_path):
    """
    Génère un rapport de recettes PDF (Bilan Z / Synthèse financière).
    - type_rapport: "jour", "mois", "annee"
    - date_val: "YYYY-MM-DD", "YYYY-MM", "YYYY"
    """
    if type_rapport == "jour":
        start_date = f"{date_val} 00:00:00"
        end_date = f"{date_val} 23:59:59"
        titre_periode = f"du {datetime.datetime.strptime(date_val, '%Y-%m-%d').strftime('%d/%m/%Y')}"
    elif type_rapport == "mois":
        parts = date_val.split("-")
        if len(parts) == 3:  # YYYY-MM-DD
            y, m = int(parts[0]), int(parts[1])
        elif len(parts) == 2:
            if len(parts[0]) == 4:  # YYYY-MM
                y, m = int(parts[0]), int(parts[1])
            else:  # MM-YYYY
                m, y = int(parts[0]), int(parts[1])
        else:
            now = datetime.datetime.now()
            y, m = now.year, now.month

        start_date = f"{y:04d}-{m:02d}-01 00:00:00"
        if m == 12:
            y_next, m_next = y + 1, 1
        else:
            y_next, m_next = y, m + 1
        end_date = f"{y_next:04d}-{m_next:02d}-01 00:00:00"
        titre_periode = f"du mois {m:02d}/{y:04d}"
    elif type_rapport == "annee":
        start_date = f"{date_val}-01-01 00:00:00"
        end_date = f"{int(date_val)+1:04d}-01-01 00:00:00"
        titre_periode = f"de l'année {date_val}"
    else:
        raise ValueError("Type de rapport invalide.")

    try:
        from database_manager import get_connection
        conn = get_connection()
    except Exception:
        db_p = os.path.join(os.path.dirname(__file__), "..", "..", "kodo_pos.db")
        conn = sqlite3.connect(db_p)

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

    # Mode de paiement
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

    # Dépenses de caisse
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

    # Liste des tickets
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

    # Articles vendus
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

    # Cumul par produit
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

    # Document PDF
    doc = SimpleDocTemplate(
        save_path,
        pagesize=A4,
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm
    )

    styles = getSampleStyleSheet()
    
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
        alignment=1
    )
    style_kpi_label = ParagraphStyle(
        'KPILabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=C_PRIMARY,
        alignment=1
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
        alignment=2
    )
    style_cell_right_bold = ParagraphStyle(
        'CellRightBold',
        parent=style_cell_text_bold,
        alignment=2
    )

    story = []

    # En-tête
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
    
    sep_table = Table([[""]], colWidths=[18*cm], rowHeights=[1])
    sep_table.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA")),
        ('BOTTOMPADDING', (0,0), (-1,-1), 15),
    ]))
    story.append(sep_table)
    story.append(Spacer(1, 0.4*cm))

    # Titre du rapport
    story.append(Paragraph("Rapport Financier & Ventes Détaillées", style_report_title))
    story.append(Paragraph(f"Période {titre_periode} &bull; Généré le {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}", style_report_sub))

    # Cartes KPI
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

    # Mode de paiement
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

    # Journal des dépenses
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

    # Articles vendus
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

    # Synthèse cumulée par produit
    if produits_cumul_list:
        story.append(Paragraph("Récapitulatif Cumulé des Ventes par Produit", style_h2))
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

    # Journal synthétique des tickets
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

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Rapport PDF généré : {save_path}")
    return save_path


# ---------------------------------------------------------------------------
# 2. GÉNÉRATEUR D'ÉTIQUETTES DE PRIX AVEC CODE-BARRES
# ---------------------------------------------------------------------------
def generer_etiquettes_pdf(nom, code_barre, taille, prix, prix_solde, qte, output_path):
    """
    Génère un PDF d'étiquettes adhésives de prix avec code-barres (6cm x 3.5cm).
    """
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
        alignment=1,
        textColor=C_PRIMARY
    )
    
    style_name = ParagraphStyle(
        'EtiquetteNom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7,
        leading=8,
        alignment=1,
        textColor=C_PRIMARY
    )
    
    style_price = ParagraphStyle(
        'EtiquettePrix',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=10,
        alignment=1,
        textColor=C_CORAL if prix_solde else C_PRIMARY
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
            b_type = "EAN13" if len(barcode_text) in (12, 13) and barcode_text.isdigit() else "Code128"
            d = generate_barcode_drawing(b_type, barcode_text, width=150, height=24)
            story.append(d)
        except Exception as e:
            print(f"[ETIQUETTE PDF] Erreur code-barres : {e}")
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


# ---------------------------------------------------------------------------
# 3. GÉNÉRATEUR DE FACTURES VECTORIELLES A4 / A5 (CONFORME FISCALITÉ)
# ---------------------------------------------------------------------------
def generer_facture_pdf(numero_facture, date_facture, client_info, items, totaux, shop_info=None, save_path=None, format_page="A4", barcode_data=None):
    """
    Génère une facture vectorielle haute qualité A4 ou A5 avec codes-barres (EAN13/Code128) et QR code.
    """
    if not save_path:
        save_path = f"facture_{numero_facture}.pdf"

    page_size = A5 if format_page.upper() == "A5" else A4
    
    doc = SimpleDocTemplate(
        save_path,
        pagesize=page_size,
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm
    )

    shop = shop_info or {}
    shop_name = shop.get("name", "L'ADRESSE B")
    shop_sub = shop.get("subtitle", "Boutique de Mode")
    shop_addr = shop.get("address", "Chemin Rue 53, 4960 Malmedy")
    shop_vat = shop.get("vat", "BE 0123.456.789")
    shop_iban = shop.get("iban", "BE68 0000 0000 0000")

    styles = getSampleStyleSheet()

    style_title = ParagraphStyle('InvTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=22, leading=26, textColor=C_PRIMARY)
    style_meta = ParagraphStyle('InvMeta', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=C_SECONDARY)
    style_h3 = ParagraphStyle('InvH3', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, leading=14, textColor=C_PRIMARY, spaceBefore=10, spaceAfter=4)
    style_cell = ParagraphStyle('InvCell', parent=styles['Normal'], fontName='Helvetica', fontSize=8.5, leading=11, textColor=C_PRIMARY)
    style_cell_bold = ParagraphStyle('InvCellBold', parent=style_cell, fontName='Helvetica-Bold')
    style_cell_right = ParagraphStyle('InvCellRight', parent=style_cell, alignment=2)
    style_cell_right_bold = ParagraphStyle('InvCellRightBold', parent=style_cell_bold, alignment=2)

    story = []

    # 1. En-tête : Vendeur & Client
    cli_nom = client_info.get("nom", "Client Comptant")
    cli_addr = client_info.get("adresse", "")
    cli_vat = client_info.get("tva", "")

    header_table_data = [
        [
            Paragraph(f"<b>{shop_name}</b><br/>{shop_sub}<br/>{shop_addr}<br/>TVA: {shop_vat}<br/>IBAN: {shop_iban}", style_meta),
            Paragraph(f"<b>FACTURER À :</b><br/><b>{cli_nom}</b><br/>{cli_addr}<br/>{f'TVA: {cli_vat}' if cli_vat else ''}", style_meta)
        ]
    ]
    t_header = Table(header_table_data, colWidths=[9*cm, 9*cm])
    t_header.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(t_header)

    story.append(Spacer(1, 0.4*cm))

    # 2. Titre Facture & Méta
    meta_box = [
        [
            Paragraph(f"<b>FACTURE N° : {numero_facture}</b>", style_title),
            Paragraph(f"Date : {date_facture}<br/>Échéance : Comptant", style_cell_right)
        ]
    ]
    t_meta = Table(meta_box, colWidths=[11*cm, 7*cm])
    t_meta.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    story.append(t_meta)
    story.append(Spacer(1, 0.5*cm))

    # 3. Tableau des Articles
    table_rows = [
        [
            Paragraph("<b>Réf / EAN</b>", style_cell_bold),
            Paragraph("<b>Désignation</b>", style_cell_bold),
            Paragraph("<b>Qté</b>", style_cell_right_bold),
            Paragraph("<b>P.U. HTVA (€)</b>", style_cell_right_bold),
            Paragraph("<b>Taux TVA</b>", style_cell_right_bold),
            Paragraph("<b>Total TVAC (€)</b>", style_cell_right_bold)
        ]
    ]

    total_htva = Decimal("0.00")
    total_tva = Decimal("0.00")
    total_tvac = Decimal("0.00")
    tva_map = {}

    for item in items:
        ref = item.get("code_barre", "-")
        nom = item.get("nom", "Article")
        qte = item.get("quantite", 1)
        pu_tvac = Decimal(str(item.get("prix_vente_tvac", 0.0)))
        taux = Decimal(str(item.get("taux_tva", 0.21)))

        p_total_tvac = pu_tvac * qte
        htva = (p_total_tvac / (Decimal("1") + taux)).quantize(Decimal("0.01"))
        tva = p_total_tvac - htva
        pu_htva = (pu_tvac / (Decimal("1") + taux)).quantize(Decimal("0.01"))

        total_htva += htva
        total_tva += tva
        total_tvac += p_total_tvac

        t_key = f"{float(taux)*100:.0f}%"
        if t_key not in tva_map:
            tva_map[t_key] = {"base": Decimal("0.00"), "montant": Decimal("0.00")}
        tva_map[t_key]["base"] += htva
        tva_map[t_key]["montant"] += tva

        table_rows.append([
            Paragraph(ref, style_cell),
            Paragraph(nom, style_cell),
            Paragraph(str(qte), style_cell_right),
            Paragraph(f"{pu_htva:.2f}", style_cell_right),
            Paragraph(t_key, style_cell_right),
            Paragraph(f"{p_total_tvac:.2f}", style_cell_right)
        ])

    t_items = Table(table_rows, colWidths=[3.0*cm, 6.5*cm, 1.5*cm, 2.3*cm, 2.0*cm, 2.7*cm])
    t_items.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), C_LIGHT_BG),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA")),
    ]))
    story.append(t_items)
    story.append(Spacer(1, 0.6*cm))

    # 4. Totaux & Décomposition TVA
    tot_data = [
        [Paragraph("<b>TOTAL SOUS-TOTAL (HTVA) :</b>", style_cell_bold), Paragraph(f"{total_htva:.2f} €", style_cell_right)],
        [Paragraph("<b>TOTAL TVA COLLECTÉE :</b>", style_cell_bold), Paragraph(f"{total_tva:.2f} €", style_cell_right)],
        [Paragraph("<b>TOTAL GÉNÉRAL A PAYER (TVAC) :</b>", style_cell_bold), Paragraph(f"<b>{total_tvac:.2f} €</b>", style_cell_right_bold)]
    ]
    t_tot = Table(tot_data, colWidths=[12*cm, 6*cm])
    t_tot.setStyle(TableStyle([
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA")),
        ('BACKGROUND', (0,-1), (-1,-1), C_LIGHT_BG),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_tot)
    story.append(Spacer(1, 0.6*cm))

    # 5. Code-barres Vectoriel / QR Code en bas de facture
    b_code = barcode_data or numero_facture
    bc_drawing = generate_barcode_drawing("Code128", b_code, width=180, height=35)
    qr_drawing = generate_barcode_drawing("QR", f"PAY-FACTURE-{numero_facture}-{total_tvac:.2f}", width=50, height=50)

    t_code = Table([[bc_drawing, qr_drawing]], colWidths=[12*cm, 6*cm])
    t_code.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER'), ('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    story.append(t_code)

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Facture PDF générée : {save_path}")
    return save_path


# ---------------------------------------------------------------------------
# 4. GÉNÉRATEUR DE REÇUS PDF VECTORIELS (TICKET 80mm / A5 / A4)
# ---------------------------------------------------------------------------
def generer_recu_pdf(numero_ticket, date_heure, items, totaux, paiements, shop_info=None, save_path=None, format_page="ticket"):
    """
    Génère un reçu PDF vectoriel au format ticket (80mm width) ou A5/A4.
    """
    if not save_path:
        save_path = f"recu_{numero_ticket}.pdf"

    if format_page == "ticket":
        # Multi-hauteur dynamique pour ticket 80mm
        total_h = 150 + len(items) * 20 + len(paiements) * 15
        pagesize = (80 * mm, max(total_h, 180) * mm)
        margin = 3 * mm
    elif format_page == "A5":
        pagesize = A5
        margin = 1 * cm
    else:
        pagesize = A4
        margin = 1.5 * cm

    doc = SimpleDocTemplate(
        save_path,
        pagesize=pagesize,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin
    )

    shop = shop_info or {}
    shop_name = shop.get("name", "L'ADRESSE B")
    shop_sub = shop.get("subtitle", "Boutique de Mode")

    styles = getSampleStyleSheet()
    style_center = ParagraphStyle('RecCenter', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, leading=12, alignment=1)
    style_text = ParagraphStyle('RecText', parent=styles['Normal'], fontName='Helvetica', fontSize=8, leading=10)
    style_right = ParagraphStyle('RecRight', parent=style_text, alignment=2)

    story = []
    story.append(Paragraph(f"<b>{shop_name}</b>", style_center))
    story.append(Paragraph(shop_sub, style_center))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Ticket N° : {numero_ticket}", style_text))
    story.append(Paragraph(f"Date : {date_heure}", style_text))
    story.append(Spacer(1, 4))

    rows = [[Paragraph("<b>Qte Item</b>", style_text), Paragraph("<b>Total</b>", style_right)]]
    for it in items:
        nom = it.get("nom", "Item")
        qte = it.get("quantite", 1)
        pu = Decimal(str(it.get("prix_vente_tvac", 0)))
        rows.append([
            Paragraph(f"{qte}x {nom}", style_text),
            Paragraph(f"{pu*qte:.2f} €", style_right)
        ])

    tot_val = Decimal(str(totaux.get("total_tvac", 0)))
    rows.append([Paragraph("<b>TOTAL TVAC</b>", style_text), Paragraph(f"<b>{tot_val:.2f} €</b>", style_right)])

    width_table = 74 * mm if format_page == "ticket" else (12 * cm if format_page == "A5" else 16 * cm)
    t = Table(rows, colWidths=[width_table*0.7, width_table*0.3])
    t.setStyle(TableStyle([('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#E5E5EA"))]))
    story.append(t)
    story.append(Spacer(1, 6))

    # Code-barres
    bc = generate_barcode_drawing("Code128", numero_ticket, width=120, height=30)
    story.append(bc)

    doc.build(story)
    print(f"Reçu PDF vectoriel généré : {save_path}")
    return save_path
