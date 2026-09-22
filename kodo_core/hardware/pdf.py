"""
Générateur de PDF vectoriels via ReportLab pour Kōdo POS.
Supporte le bilan Z (jour/mois/année), reçus A4/A5/ticket, factures vectorielles et étiquettes avec codes-barres (EAN13, Code128, QR Code).
"""
import os
import re
import logging
import sqlite3
import datetime
import subprocess
from decimal import Decimal, ROUND_HALF_UP

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

logger = logging.getLogger("kodo_core.hardware.pdf")

# ---------------------------------------------------------------------------
# Constantes de symbologie code-barres
# ---------------------------------------------------------------------------
PT_MM = 25.4 / 72.0

# Un EAN-13 complet occupe 95 modules + 11 modules de zone de silence à gauche
# et 7 à droite, soit 113 modules (valeur mesurée sur le rendu ReportLab).
EAN13_MODULES = 113
MODULE_NOMINAL_MM = 0.330   # module nominal EAN-13 (grossissement SC2)
MODULE_MIN_MM = 0.264       # minimum absolu (SC0) : en dessous, plus de lecture fiable

# Valeurs de repli de l'étiquette : elles reproduisent exactement le format
# historique (6 x 3,5 cm, marges de 0,2 cm) pour ne casser aucun appel existant.
LABEL_DEFAUT_LARGEUR_MM = 60.0
LABEL_DEFAUT_HAUTEUR_MM = 35.0
LABEL_DEFAUT_MARGE_MM = 2.0
LABEL_DEFAUT_DPI = 203


class BarcodeTropEtroitError(ValueError):
    """Le support est trop étroit pour un EAN-13 lisible par une douchette."""


def ean13_cle_controle(douze_chiffres):
    """Clé de contrôle d'un EAN-13 à partir de ses 12 premiers chiffres."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(douze_chiffres))
    return str((10 - total % 10) % 10)


def ean13_valide(code):
    """True uniquement pour 13 chiffres dont la clé de contrôle est correcte."""
    code = str(code or "")
    return (len(code) == 13 and code.isdigit()
            and code[12] == ean13_cle_controle(code[:12]))


def largeur_mini_ean13_mm(dpi=LABEL_DEFAUT_DPI, marge_mm=LABEL_DEFAUT_MARGE_MM):
    """
    Largeur d'étiquette minimale permettant un EAN-13 conforme sur une tête `dpi`.

    Un module doit tomber sur un nombre ENTIER de points de chauffe, sinon les
    barres sont irrégulières. On cherche donc le plus petit nombre de points
    dont la largeur atteint le module nominal.
    """
    point_mm = 25.4 / float(dpi)
    points = 1
    while points * point_mm < MODULE_NOMINAL_MM:
        points += 1
    return EAN13_MODULES * points * point_mm + 2 * marge_mm


def build_barcode_drawing(barcode_type, value, width=160, height=40, dpi=LABEL_DEFAUT_DPI):
    """
    Code-barres vectoriel. Retourne (Drawing, métadonnées).

    Lève plutôt que de produire un symbole différent de la donnée fournie :
    ReportLab ne conserve que 12 chiffres d'un EAN-13 et recalcule lui-même la
    clé de contrôle, ce qui imprimerait un code introuvable en base.
    """
    from reportlab.graphics.barcode import createBarcodeDrawing

    b_type = (barcode_type or "").upper()
    val_str = str(value or "")
    if not val_str:
        raise ValueError("Aucune donnée à encoder : refus d'inventer un code.")

    if b_type in ("EAN13", "EAN-13"):
        if not ean13_valide(val_str):
            raise ValueError(
                f"'{val_str}' n'est pas un EAN-13 valide (13 chiffres + clé de contrôle). "
                "Encodage en Code128 requis pour respecter la donnée."
            )
        module_mm = (width / EAN13_MODULES) * PT_MM
        if module_mm < MODULE_MIN_MM:
            raise BarcodeTropEtroitError(
                f"Module de {module_mm:.3f} mm (minimum {MODULE_MIN_MM} mm) : "
                f"il faut {EAN13_MODULES * MODULE_NOMINAL_MM:.1f} mm de large pour un EAN-13, "
                f"{width * PT_MM:.1f} mm disponibles."
            )
        d = createBarcodeDrawing('EAN13', value=val_str, width=width, height=height,
                                 humanReadable=False)
        symbologie = "EAN13"
    elif b_type in ("QR", "QRCODE"):
        d = createBarcodeDrawing('QR', value=val_str, width=width, height=height)
        symbologie = "QR"
    else:  # Par défaut Code128
        d = createBarcodeDrawing('Code128', value=val_str, width=width, height=height,
                                 humanReadable=False)
        symbologie = "Code128"

    widget = d.contents[0]
    module_mm = getattr(widget, "barWidth", 0) * d.transform[0] * PT_MM
    meta = {
        "symbologie": symbologie,
        "valeur": val_str,
        "module_mm": module_mm,
        "points_par_module": module_mm / (25.4 / float(dpi)) if module_mm else 0.0,
        "sous_nominal": bool(module_mm) and module_mm < MODULE_NOMINAL_MM,
    }
    return d, meta


def generate_barcode_drawing(barcode_type, value, width=160, height=40):
    """
    Génère un Drawing ReportLab contenant un code-barres vectoriel (Code128, EAN13, QR).

    Conserve l'ancien contrat (retourne toujours un Drawing, ne lève jamais) pour
    les appelants historiques. Le code appelant qui doit SAVOIR si le symbole a
    été produit utilise `build_barcode_drawing`.
    """
    try:
        d, _meta = build_barcode_drawing(barcode_type, value, width=width, height=height)
        return d
    except Exception as e:
        logger.warning("Code-barres %s non généré pour %r : %s", barcode_type, value, e)
        return Drawing(width, height)


# ---------------------------------------------------------------------------
# Catalogue des formats d'étiquette
#
# Source de vérité : le PPD de la file d'impression choisie par la commerçante
# (`formats_etiquette_disponibles`). Chaque étiqueteuse déclare ses propres
# formats, il n'y a donc rien à coder en dur.
#
# La liste ci-dessous n'est qu'un REPLI, pour les files dont le PPD est
# générique ou illisible. Elle ne contient que des dimensions relevées dans un
# PPD DYMO réel (`*PaperDimension`, en points PostScript) : aucune dimension
# approximative n'y figure. Les consommables Brother (DK) et Zebra ne sont
# volontairement pas listés : leurs dimensions doivent être lues dans le PPD de
# la machine, ou saisies en millimètres par la commerçante d'après l'emballage
# de son rouleau. On ne fait pas acheter un consommable sur une dimension
# approximative.
# ---------------------------------------------------------------------------
LABEL_FORMATS_REPLI = [
    # (identifiant, libellé, largeur_pt, hauteur_pt)
    ("dymo_w54h144",  "DYMO Return Address (19 x 51 mm)",   54, 144),
    ("dymo_w81h252",  "DYMO Address (29 x 89 mm)",          81, 252),
    ("dymo_w101h252", "DYMO Large Address (36 x 89 mm)",   101, 252),
    ("dymo_w153h198", "DYMO 3.5\" Disk (54 x 70 mm)",      153, 198),
    ("dymo_w162h225", "DYMO Paint Can (57 x 79 mm)",       162, 225),
    ("dymo_w162h288", "DYMO 2.25 x 4.00\" (57 x 102 mm)",  162, 288),
    ("dymo_w41h144",  "DYMO Hanging Folder (14 x 51 mm)",   41, 144),
    ("dymo_w41h248",  "DYMO File Folder (14 x 87 mm)",      41, 248),
]


def _format_depuis_nom_cups(nom_cups):
    """
    Dimensions d'un format CUPS nommé `w<largeur>h<hauteur>` (en points).
    Retourne (largeur_mm, hauteur_mm) ou None si le nom ne suit pas ce schéma.
    """
    m = re.fullmatch(r"w(\d+)h(\d+)", str(nom_cups or "").strip())
    if not m:
        return None
    return (int(m.group(1)) * PT_MM, int(m.group(2)) * PT_MM)


def formats_etiquette_disponibles(printer_name=None):
    """
    Formats d'étiquette proposés à la commerçante pour une file d'impression.

    1. Lit le PPD de la file (`*PaperDimension`), qui donne les dimensions
       exactes des consommables reconnus par SA machine.
    2. À défaut, interroge `lpoptions -p <file> -l` (noms `wLARGEURhHAUTEUR`).
    3. En dernier recours, retourne le catalogue de repli.

    Ne fait aucune impression et n'écrit rien : uniquement de la lecture.
    """
    formats = []

    if printer_name:
        ppd = f"/etc/cups/ppd/{printer_name}.ppd"
        try:
            with open(ppd, "r", encoding="utf-8", errors="replace") as fh:
                for ligne in fh:
                    m = re.match(r'\*PaperDimension\s+(\S+?)\s*/([^:]*):\s*"([\d.]+)\s+([\d.]+)"', ligne)
                    if m:
                        formats.append({
                            "id": m.group(1),
                            "libelle": m.group(2).strip() or m.group(1),
                            "media": m.group(1),
                            "largeur_mm": round(float(m.group(3)) * PT_MM, 2),
                            "hauteur_mm": round(float(m.group(4)) * PT_MM, 2),
                            "source": "ppd",
                        })
        except Exception as e:
            logger.info("PPD illisible pour %s (%s), repli sur lpoptions.", printer_name, e)

        if not formats:
            try:
                out = subprocess.check_output(["lpoptions", "-p", printer_name, "-l"],
                                              stderr=subprocess.DEVNULL, timeout=2).decode()
                for ligne in out.splitlines():
                    if not ligne.startswith("PageSize"):
                        continue
                    for brut in ligne.split(":", 1)[1].split():
                        nom = brut.lstrip("*")
                        dims = _format_depuis_nom_cups(nom)
                        if dims:
                            formats.append({
                                "id": nom, "libelle": nom, "media": nom,
                                "largeur_mm": round(dims[0], 2),
                                "hauteur_mm": round(dims[1], 2),
                                "source": "lpoptions",
                            })
            except Exception as e:
                logger.info("lpoptions indisponible pour %s : %s", printer_name, e)

    if not formats:
        formats = [{
            "id": fid, "libelle": lib, "media": fid.split("_", 1)[-1],
            "largeur_mm": round(w * PT_MM, 2), "hauteur_mm": round(h * PT_MM, 2),
            "source": "repli",
        } for fid, lib, w, h in LABEL_FORMATS_REPLI]

    # Chaque format est annoté : un EAN-13 y tient-il à une densité lisible ?
    for f in formats:
        for sens, utile in (("portrait", f["largeur_mm"]), ("paysage", f["hauteur_mm"])):
            module = (utile - 2 * LABEL_DEFAUT_MARGE_MM) / EAN13_MODULES
            f[f"ean13_{sens}"] = round(module, 4) >= MODULE_MIN_MM
        f["ean13_ok"] = f["ean13_portrait"] or f["ean13_paysage"]

    return formats


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
    shop_name = get_param(c, "shop_name", "Mon Commerce")
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
def _largeur_ean13_alignee_pt(largeur_dispo_pt, dpi):
    """
    Largeur (pt) d'un EAN-13 dont le module tombe sur un nombre ENTIER de points
    de chauffe, sans dépasser la place disponible. Un module fractionnaire
    produit des barres irrégulières que les douchettes lisent mal.

    Retourne la largeur disponible telle quelle si aucun alignement n'est
    possible : `build_barcode_drawing` tranchera alors sur la conformité.
    """
    point_mm = 25.4 / float(dpi)
    module_max_mm = (largeur_dispo_pt * PT_MM) / EAN13_MODULES
    points = int(module_max_mm // point_mm)
    if points < 1 or points * point_mm < MODULE_MIN_MM:
        # Aucun alignement possible sans descendre sous le seuil de lisibilité.
        # On rend la largeur disponible telle quelle : le contrôle de conformité
        # de `build_barcode_drawing` rapportera alors la place réellement
        # manquante sur l'étiquette, et non une largeur rabotée par l'alignement.
        return largeur_dispo_pt
    return (EAN13_MODULES * points * point_mm) / PT_MM


def _etiquette_styles(prix_solde=None):
    """Styles de paragraphe d'une étiquette de prix."""
    styles = getSampleStyleSheet()
    return {
        "shop": ParagraphStyle(
            'EtiquetteShop', parent=styles['Normal'], fontName='Helvetica-Bold',
            fontSize=8, leading=9, alignment=1, textColor=C_PRIMARY),
        "nom": ParagraphStyle(
            'EtiquetteNom', parent=styles['Normal'], fontName='Helvetica',
            fontSize=7, leading=8, alignment=1, textColor=C_PRIMARY),
        # Le code lisible à l'œil : ReportLab écrase son propre texte lors de la
        # mise à l'échelle non uniforme du Drawing (jusqu'à 2,3 pt), on le
        # dessine donc nous-mêmes à une taille réellement lisible.
        "code": ParagraphStyle(
            'EtiquetteCode', parent=styles['Normal'], fontName='Helvetica',
            fontSize=6, leading=7, alignment=1, textColor=C_PRIMARY),
        "prix": ParagraphStyle(
            'EtiquettePrix', parent=styles['Normal'], fontName='Helvetica-Bold',
            fontSize=9, leading=10, alignment=1,
            textColor=C_CORAL if prix_solde else C_PRIMARY),
    }


def _etiquette_flowables(nom, code_barre, taille, prix, prix_solde, shop_name,
                         largeur_code_pt, hauteur_code_pt, dpi, show_price,
                         avertissements):
    """Contenu d'UNE étiquette. N'invente jamais de code-barres."""
    from xml.sax.saxutils import escape

    styles = _etiquette_styles(prix_solde)
    flow = []
    if shop_name:
        flow.extend([Paragraph(escape(str(shop_name)), styles["shop"]), Spacer(1, 2)])

    # La taille distingue les étiquettes entre elles : elle ne doit jamais être
    # avalée par la troncature du nom.
    taille_suffix = f" ({taille})" if taille and str(taille) != "—" else ""
    nom = str(nom or "")
    if len(nom) + len(taille_suffix) > 35:
        place = max(35 - len(taille_suffix) - 3, 8)
        nom_complet = f"{nom[:place]}...{taille_suffix}"
    else:
        nom_complet = f"{nom}{taille_suffix}"
    flow.append(Paragraph(escape(nom_complet), styles["nom"]))
    flow.append(Spacer(1, 3))

    barcode_text = str(code_barre or "").strip()
    if not barcode_text:
        # Règle projet : ne rien inventer sur le document remis à la cliente.
        # L'ancien repli "000000000000" imprimait un EAN-13 valide et scannable.
        avertissements.append(f"{nom_complet} : aucun code-barres enregistré, étiquette sans code.")
        flow.append(Paragraph("<i>Sans code-barres</i>", styles["nom"]))
        flow.append(Spacer(1, hauteur_code_pt - 8))
    else:
        est_ean13 = ean13_valide(barcode_text)
        symbologie = "EAN13" if est_ean13 else "Code128"
        largeur = (_largeur_ean13_alignee_pt(largeur_code_pt, dpi) if est_ean13
                   else largeur_code_pt)
        try:
            d, meta = build_barcode_drawing(symbologie, barcode_text,
                                            width=largeur, height=hauteur_code_pt, dpi=dpi)
            flow.append(d)
            if meta["sous_nominal"]:
                avertissements.append(
                    f"{barcode_text} : module de {meta['module_mm']:.3f} mm, "
                    f"sous le nominal de {MODULE_NOMINAL_MM} mm."
                )
        except BarcodeTropEtroitError:
            raise  # remonte à l'appelant : le format d'étiquette est à changer
        except Exception as e:
            logger.error("Code-barres %r non généré : %s", barcode_text, e)
            avertissements.append(f"{barcode_text} : code-barres non généré ({e}).")
            flow.append(Paragraph("<i>Code-barres indisponible</i>", styles["nom"]))
            flow.append(Spacer(1, hauteur_code_pt - 8))
        # Toujours imprimé : si le scan échoue, la caissière peut saisir le code.
        flow.append(Paragraph(escape(barcode_text), styles["code"]))

    flow.append(Spacer(1, 3))

    if show_price:
        if prix_solde:
            p_orig = Decimal(str(prix)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            p_solde = Decimal(str(prix_solde)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            txt_price = (f"<font color='#86868B'><s>{p_orig:.2f} €</s></font>  "
                         f"<b><font color='#FF3B30'>{p_solde:.2f} € SOLDE</font></b>")
            flow.append(Paragraph(txt_price, styles["prix"]))
        else:
            p_reg = Decimal(str(prix)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            flow.append(Paragraph(f"<b>{p_reg:.2f} €</b>", styles["prix"]))

    return flow


def generer_etiquettes_lot_pdf(lignes, output_path,
                               largeur_mm=LABEL_DEFAUT_LARGEUR_MM,
                               hauteur_mm=LABEL_DEFAUT_HAUTEUR_MM,
                               marge_mm=LABEL_DEFAUT_MARGE_MM,
                               dpi=LABEL_DEFAUT_DPI,
                               orientation="portrait",
                               shop_name=None,
                               show_price=True):
    """
    PDF d'étiquettes pour PLUSIEURS articles, au format d'étiquette configuré.

    `lignes` : liste de dicts
        {product_id, name, barcode, size, price, price_sale, quantity}
    `quantity` étiquettes sont produites par ligne, à raison d'UNE ÉTIQUETTE PAR
    PAGE au format du support : c'est la mise en page attendue d'une étiqueteuse
    à rouleau, où chaque page correspond à une étiquette détachée. Ce n'est pas
    une planche A4.

    `orientation="paysage"` permute largeur et hauteur du support : sur une
    étiquette étroite et longue (type adresse), c'est le seul sens qui laisse
    assez de place à un EAN-13 lisible.

    Retourne {"path", "pages", "avertissements"}.
    Lève `BarcodeTropEtroitError` si le format choisi ne permet aucun EAN-13
    conforme : mieux vaut refuser que livrer un code que la douchette ne lira pas.
    """
    if orientation == "paysage":
        page_w_mm, page_h_mm = float(hauteur_mm), float(largeur_mm)
    else:
        page_w_mm, page_h_mm = float(largeur_mm), float(hauteur_mm)

    page_w, page_h = page_w_mm / PT_MM, page_h_mm / PT_MM
    marge = float(marge_mm) / PT_MM

    doc = SimpleDocTemplate(
        output_path,
        pagesize=(page_w, page_h),
        leftMargin=marge, rightMargin=marge,
        topMargin=marge, bottomMargin=marge,
    )

    largeur_code_pt = page_w - 2 * marge
    hauteur_code_pt = max(20.0, (page_h - 2 * marge) * 0.32)

    avertissements = []
    story = []
    pages = 0
    for ligne in (lignes or []):
        qte = max(1, int(ligne.get("quantity") or 1))
        for _ in range(qte):
            if story:
                story.append(PageBreak())
            story.extend(_etiquette_flowables(
                ligne.get("name"), ligne.get("barcode"), ligne.get("size"),
                ligne.get("price") or 0, ligne.get("price_sale"),
                shop_name, largeur_code_pt, hauteur_code_pt, dpi, show_price,
                avertissements,
            ))
            pages += 1

    if not story:
        raise ValueError("Aucun article à étiqueter.")

    doc.build(story)
    logger.info("Étiquettes PDF générées (%d page(s), %.1f x %.1f mm) : %s",
                pages, page_w_mm, page_h_mm, output_path)
    return {"path": output_path, "pages": pages, "avertissements": avertissements}


def generer_etiquettes_pdf(nom, code_barre, taille, prix, prix_solde, qte, output_path,
                           largeur_mm=LABEL_DEFAUT_LARGEUR_MM,
                           hauteur_mm=LABEL_DEFAUT_HAUTEUR_MM,
                           marge_mm=LABEL_DEFAUT_MARGE_MM,
                           dpi=LABEL_DEFAUT_DPI,
                           orientation="portrait",
                           shop_name="Mon Commerce"):
    """
    Génère un PDF d'étiquettes adhésives de prix avec code-barres pour UN article.

    Les six premiers paramètres et les valeurs par défaut reproduisent le
    comportement historique (6 cm x 3,5 cm, marges de 0,2 cm) : tout appel
    existant continue de fonctionner à l'identique. Retourne `output_path`.
    """
    generer_etiquettes_lot_pdf(
        [{"name": nom, "barcode": code_barre, "size": taille,
          "price": prix, "price_sale": prix_solde, "quantity": qte}],
        output_path,
        largeur_mm=largeur_mm, hauteur_mm=hauteur_mm, marge_mm=marge_mm,
        dpi=dpi, orientation=orientation, shop_name=shop_name,
    )
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
    shop_name = shop.get("name", "Mon Commerce")
    shop_sub = shop.get("subtitle", "Boutique de Mode")
    shop_addr = shop.get("address", "")
    # Une facture porte le numéro de TVA et l'IBAN réels du commerçant, ou ne les porte
    # pas. Aucun repli : un IBAN par défaut sur une facture est une instruction de
    # paiement vers un compte qui n'est pas celui du commerçant.
    shop_vat = shop.get("vat", "")
    shop_iban = shop.get("iban", "")

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

    _identite = [f"<b>{shop_name}</b>", shop_sub]
    if shop_addr:
        _identite.append(shop_addr)
    _identite.append(f"TVA: {shop_vat}" if shop_vat else "<b>** N° TVA NON RENSEIGNÉ — Paramètres &gt; Boutique **</b>")
    if shop_iban:
        _identite.append(f"IBAN: {shop_iban}")

    header_table_data = [
        [
            Paragraph("<br/>".join(_identite), style_meta),
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
        htva = (p_total_tvac / (Decimal("1") + taux)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tva = p_total_tvac - htva
        pu_htva = (pu_tvac / (Decimal("1") + taux)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

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
    shop_name = shop.get("name", "Mon Commerce")
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


# ---------------------------------------------------------------------------
# 6. BORDEREAUX DE LIVRAISON (commandes issues du Live Shopping)
# ---------------------------------------------------------------------------

def generer_bordereaux_livraison_pdf(orders, save_path):
    """
    Génère un PDF A4 avec un bordereau de livraison par page (une page par commande).

    `orders` : liste de dicts {numero_ticket, session_reference, client_nom, telephone, email,
    adresse, mode_paiement, reference_paiement, date_commande, lignes: [{nom, variante, sku, quantite}]}
    (voir LiveBridge.get_delivery_orders). `save_path` : chemin ou fichier-like (BytesIO).
    """
    from xml.sax.saxutils import escape
    from kodo_core.db.connection import get_connection

    shop_name, shop_addr, shop_tel = "Kōdo POS", "", ""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT nom_magasin, adresse, telephone FROM ShopInfo ORDER BY id LIMIT 1")
            row = cur.fetchone()
            if row:
                shop_name, shop_addr, shop_tel = row[0] or shop_name, row[1] or "", row[2] or ""
            # Même source que l'export du stock et les autres PDF : Parametres.shop_name prime
            configured = get_param(cur, "shop_name", "")
            if configured:
                shop_name = configured
        finally:
            conn.close()
    except Exception:
        pass

    # Les textes clients viennent d'un fichier externe : échappés pour ne pas casser le rendu XML
    esc = lambda v: escape(str(v or ""))

    doc = SimpleDocTemplate(save_path, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    s_title = ParagraphStyle('BdlTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=20, leading=24, textColor=C_PRIMARY)
    s_shop = ParagraphStyle('BdlShop', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=C_SECONDARY)
    s_text = ParagraphStyle('BdlText', parent=styles['Normal'], fontName='Helvetica', fontSize=10, leading=14)
    s_label = ParagraphStyle('BdlLabel', parent=s_text, fontName='Helvetica-Bold', fontSize=8, textColor=C_SECONDARY)

    story = []
    for i, o in enumerate(orders):
        if i:
            story.append(PageBreak())
        story.append(Paragraph("BORDEREAU DE LIVRAISON", s_title))
        story.append(Paragraph(f"<b>{esc(shop_name)}</b> · {esc(shop_addr)} {esc(shop_tel)}", s_shop))
        story.append(Spacer(1, 14))

        ref_rows = [[Paragraph("RÉFÉRENCE COMMANDE", s_label), Paragraph("SESSION LIVE", s_label), Paragraph("DATE COMMANDE", s_label)],
                    [Paragraph(esc(o.get("numero_ticket")), s_text), Paragraph(esc(o.get("session_reference")), s_text),
                     Paragraph(esc(o.get("date_commande")[:10] if o.get("date_commande") else ""), s_text)]]
        t_ref = Table(ref_rows, colWidths=[6 * cm, 7 * cm, 5 * cm])
        t_ref.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), C_LIGHT_BG), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 8), ('TOPPADDING', (0, 0), (-1, -1), 5),
                                   ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
        story.append(t_ref)
        story.append(Spacer(1, 14))

        adresse = esc(o.get("adresse")).replace(",", "<br/>") if o.get("adresse") else "<i>Adresse non renseignée</i>"
        story.append(Paragraph("DESTINATAIRE", s_label))
        story.append(Paragraph(f"<b>{esc(o.get('client_nom'))}</b>", s_text))
        story.append(Paragraph(adresse, s_text))
        if o.get("telephone"):
            story.append(Paragraph(f"Tél. : {esc(o.get('telephone'))}", s_text))
        if o.get("email"):
            story.append(Paragraph(esc(o.get("email")), s_text))
        story.append(Spacer(1, 16))

        rows = [[Paragraph("<b>Article</b>", s_text), Paragraph("<b>Variante</b>", s_text),
                 Paragraph("<b>SKU</b>", s_text), Paragraph("<b>Qté</b>", s_text)]]
        total_qty = 0
        for l in o.get("lignes", []):
            total_qty += int(l.get("quantite") or 0)
            rows.append([Paragraph(esc(l.get("nom")), s_text), Paragraph(esc(l.get("variante")), s_text),
                         Paragraph(esc(l.get("sku")), s_text), Paragraph(str(l.get("quantite", 1)), s_text)])
        t_items = Table(rows, colWidths=[7 * cm, 4 * cm, 5 * cm, 2 * cm], repeatRows=1)
        t_items.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E5EA")),
                                     ('BACKGROUND', (0, 0), (-1, 0), C_LIGHT_BG), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
        story.append(t_items)
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"<b>{total_qty}</b> article(s) — <b>Réglé</b> ({esc(o.get('mode_paiement'))}"
                               f"{' · réf. ' + esc(o.get('reference_paiement')) if o.get('reference_paiement') else ''})", s_text))
        story.append(Spacer(1, 40))
        story.append(Paragraph("Signature du destinataire à la réception : ______________________________", s_shop))

    doc.build(story)
    return save_path
