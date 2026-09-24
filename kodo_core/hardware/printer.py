"""
Driver d'impression thermique ESC/POS multiplateforme (macOS, Windows, Linux).
Supporte CUPS, win32print, socket direct, découpe papier (GS V), ouverture tiroir caisse (DLE DC4 / ESC p).
"""
import os
import sys
import socket
import tempfile
import subprocess
import datetime
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

# Configuration du backend USB sous macOS / Windows si disponible
try:
    import usb.backend.libusb1
    import libusb_package
    _old_get_backend = usb.backend.libusb1.get_backend
    usb.backend.libusb1.get_backend = lambda *a, **k: _old_get_backend(find_library=libusb_package.find_library)
except Exception:
    pass

COL = 42  # Largeur standard ticket 80mm (42 colonnes)

# ---------------------------------------------------------------------------
# SÉQUENCES DE COMMANDES ESC/POS
# ---------------------------------------------------------------------------
ESC = b'\x1b'
GS = b'\x1d'
DLE = b'\x10'

ESC_INIT = ESC + b'@'
ESC_ALIGN_LEFT = ESC + b'a\x00'
ESC_ALIGN_CENTER = ESC + b'a\x01'
ESC_ALIGN_RIGHT = ESC + b'a\x02'

ESC_BOLD_ON = ESC + b'E\x01'
ESC_BOLD_OFF = ESC + b'E\x00'

ESC_UNDERLINE_ON = ESC + b'-\x01'
ESC_UNDERLINE_OFF = ESC + b'-\x00'

GS_TEXT_NORMAL = GS + b'!\x00'
GS_TEXT_DOUBLE_HEIGHT = GS + b'!\x01'
GS_TEXT_DOUBLE_WIDTH = GS + b'!\x10'
GS_TEXT_DOUBLE_SIZE = GS + b'!\x11'

# Découpe de papier
GS_CUT_FULL = GS + b'V\x00'
GS_CUT_PARTIAL = GS + b'V\x01'
GS_CUT_FUNCTION = GS + b'VB\x00'  # GS V 66 0
ESC_CUT_ALT = ESC + b'i'          # ESC i (Alternative full cut)

# Ouverture tiroir-caisse (pulse)
ESC_DRAWER_PIN2 = ESC + b'p\x00\x19\xfa'  # ESC p 0 25 250 (Pin 2 RJ11)
ESC_DRAWER_PIN5 = ESC + b'p\x01\x19\xfa'  # ESC p 1 25 250 (Pin 5 RJ11)
DLE_DRAWER_PULSE = DLE + b'\x14\x01\x01\x01'  # DLE DC4 1 1 1


def get_resource_path(relative_path):
    """
    Retourne le chemin absolu de la ressource.
    Fonctionne en mode de développement et dans un bundle PyInstaller (sys._MEIPASS).
    """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    hw_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(hw_dir, relative_path)
    if os.path.exists(candidate):
        return candidate
    root_dir = os.path.abspath(os.path.join(hw_dir, "..", ".."))
    candidate_root = os.path.join(root_dir, relative_path)
    if os.path.exists(candidate_root):
        return candidate_root
    return candidate


def strip_accents(text):
    """
    Supprime les accents et remplace les caractères spéciaux pour éviter
    les caractères bizarres sur les imprimantes thermiques ESC/POS.
    """
    if not isinstance(text, str):
        text = str(text)
    nfd_form = unicodedata.normalize('NFD', text)
    only_ascii = "".join([c for c in nfd_form if unicodedata.category(c) != 'Mn'])
    
    replacements = {
        '’': "'",
        'œ': 'oe',
        'Œ': 'OE',
        'æ': 'ae',
        'Æ': 'AE',
        '€': 'EUR',
        'ō': 'o',
        'Ō': 'O',
        '\u00a0': ' ',
        '\u202f': ' ',
        '\u2007': ' ',
        '\u2009': ' ',
    }
    for k, v in replacements.items():
        only_ascii = only_ascii.replace(k, v)
        
    return only_ascii


def sanitize_escpos_text(text):
    """
    Purge les caractères de contrôle non imprimables pour empêcher toute
    injection de commandes matérielles ESC/POS non sollicitées (ex: ESC @, GS V).
    Conserve uniquement les retours chariot (\n), tabulations (\t)
    et les caractères ASCII imprimables (codes 32 à 126).
    """
    if not isinstance(text, str):
        text = str(text or "")
    clean = strip_accents(text)
    sanitized = "".join(c for c in clean if c in ("\n", "\t") or (32 <= ord(c) < 127))
    return sanitized


def _center(text, width=COL):
    return text.center(width)


def _right(label, value, width=COL):
    space = width - len(label) - len(value)
    return label + " " * max(space, 1) + value


def _separator(char="-", width=COL):
    return char * width


# ---------------------------------------------------------------------------
# CLASSE DRIVER D'IMPRESSION THERMIQUE MULTIPLATEFORME
# ---------------------------------------------------------------------------
class ESCPOSThermalPrinter:
    """
    Driver universel d'impression thermique ESC/POS supportant macOS, Windows et Linux.
    Prend en charge CUPS / lpr, win32print, sockets réseau directs (TCP 9100) et USB direct.
    """

    def __init__(self, printer_name=None, host=None, port=9100, vendor_id=None, product_id=None):
        self.printer_name = printer_name
        self.host = host
        self.port = port
        self.vendor_id = vendor_id
        self.product_id = product_id

        # Détection automatique de l'imprimante par défaut sous macOS / Linux si non spécifiée
        if not self.printer_name and sys.platform in ["darwin", "linux"]:
            try:
                import subprocess, re
                # Langue des messages CUPS figée : voir `printer_service.env_cups`.
                from kodo_core.hardware.printer_service import env_cups
                out_d = subprocess.check_output(
                    ["lpstat", "-d"], stderr=subprocess.DEVNULL, timeout=1, env=env_cups()
                ).decode()
                m_d = re.search(r':\s*(\S+)', out_d)
                if m_d:
                    self.printer_name = m_d.group(1)
            except Exception:
                pass

    def connect(self):
        """Vérifie si la connexion ou l'imprimante est joignable."""
        if self.host:
            try:
                s = socket.create_connection((self.host, self.port), timeout=1.5)
                s.close()
                return True
            except Exception:
                return False
        return True

    def send_raw(self, raw_bytes: bytes) -> bool:
        """
        Envoie des données binaires brutes ESC/POS à l'imprimante.
        Supports: Socket TCP direct, Windows win32print, macOS CUPS/lpr.
        """
        if not raw_bytes:
            return False

        # 1. Socket réseau TCP direct (si hôte réseau configuré et actif)
        if self.host and self.host not in ["127.0.0.1", "localhost"]:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1.5)
                    s.connect((self.host, self.port))
                    s.sendall(raw_bytes)
                print(f"[SUCCESS] Données ESC/POS envoyées via Socket IP {self.host}:{self.port}")
                return True
            except Exception as e:
                print(f"[INFO Socket] IP {self.host}:{self.port} non joignable ({e}). Bascule vers l'imprimante USB/Spouleur...")

        # 2. Impresion sous Windows (win32print / spooler)
        if sys.platform == 'win32':
            try:
                import win32print
                p_name = self.printer_name or win32print.GetDefaultPrinter()
                h_printer = win32print.OpenPrinter(p_name)
                try:
                    job = win32print.StartDocPrinter(h_printer, 1, ("Kodo POS Ticket", None, "RAW"))
                    win32print.StartPagePrinter(h_printer)
                    win32print.WritePrinter(h_printer, raw_bytes)
                    win32print.EndPagePrinter(h_printer)
                    win32print.EndDocPrinter(h_printer)
                    print(f"[SUCCESS] Ticket imprimé sous Windows via win32print sur '{p_name}'")
                    return True
                finally:
                    win32print.ClosePrinter(h_printer)
            except ImportError:
                print("[INFO] win32print non disponible, tentative via spooler fichier...")
            except Exception as e:
                print(f"[ERROR win32print] {e}")

            # Fallback spooler Windows via fichier temporaire
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(prefix="kodo_win_", suffix=".bin")
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw_bytes)
                
                cmd = f'copy /b "{temp_path}" "{self.printer_name or "PRN"}"'
                res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2.0)
                if res.returncode == 0:
                    print("[SUCCESS] Ticket imprimé sous Windows via spooler CMD.")
                    return True
            except Exception as e:
                print(f"[ERROR Spooler Windows] {e}")
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

        # 3. Impression sous macOS & Linux (CUPS / lp / lpr)
        if sys.platform in ['darwin', 'linux']:
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(prefix="kodo_pos_", suffix=".bin")
                with os.fdopen(fd, 'wb') as f:
                    f.write(raw_bytes)

                printed = False
                # Tentative via lp -o raw (timeout strict 2.0s pour éviter de figer le thread)
                try:
                    cmd = ["lp", "-o", "raw"]
                    if self.printer_name:
                        cmd.extend(["-d", self.printer_name])
                    cmd.append(temp_path)
                    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2.0)
                    if res.returncode == 0:
                        printed = True
                        print("[SUCCESS] Ticket ESC/POS envoyé via CUPS (lp -o raw).")
                except Exception as e:
                    print(f"[INFO] Échec lp ({e}), tentative via lpr...")

                # Fallback via lpr (timeout strict 2.0s)
                if not printed:
                    try:
                        cmd = ["lpr", "-o", "raw"]
                        if self.printer_name:
                            cmd.extend(["-P", self.printer_name])
                        cmd.append(temp_path)
                        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2.0)
                        if res.returncode == 0:
                            printed = True
                            print("[SUCCESS] Ticket ESC/POS envoyé via lpr.")
                    except Exception as e:
                        print(f"[ERROR lpr] {e}")

                if printed:
                    return True
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass

        return False

    def cut_paper(self, full=False) -> bool:
        """Envoie la commande ESC/POS de découpe de papier."""
        cmd = GS_CUT_FULL if full else (GS_CUT_FUNCTION + b"\n\n")
        return self.send_raw(cmd)

    def open_cash_drawer(self, pin=0) -> bool:
        """Envoie l'impulsion électrique (ESC p / DLE DC4) pour ouvrir le tiroir-caisse."""
        pulse = ESC_DRAWER_PIN5 if pin == 1 else ESC_DRAWER_PIN2
        # On envoie également la variante DLE DC4 pour compatibilité maximale
        raw_cmd = ESC_INIT + pulse + DLE_DRAWER_PULSE
        return self.send_raw(raw_cmd)

    def set_align(self, align="left") -> bytes:
        """Retourne la commande d'alignement."""
        if align == "center":
            return ESC_ALIGN_CENTER
        elif align == "right":
            return ESC_ALIGN_RIGHT
        return ESC_ALIGN_LEFT

    def set_bold(self, enabled=True) -> bytes:
        """Retourne la commande pour activer/désactiver le gras."""
        return ESC_BOLD_ON if enabled else ESC_BOLD_OFF

    def print_receipt(self, content_text, numero="ticket") -> bool:
        """Prépare et imprime un ticket de caisse à partir d'un texte."""
        contenu_clean = strip_accents(content_text)
        payload = bytearray(ESC_INIT + ESC_ALIGN_LEFT)
        payload.extend(contenu_clean.encode('ascii', errors='replace'))
        payload.extend(b"\n\n\n\n" + GS_CUT_FUNCTION)
        return self.send_raw(bytes(payload))

    def print_takeaway_ticket(self, numero_commande, items, nom_client=None, telephone=None, heure_retrait=None, notes=None, shop_name="Kōdo POS") -> bool:
        """Génère et imprime un ticket spécial vente à emporter / cuisine."""
        txt = generer_ticket_takeaway(
            numero_commande=numero_commande,
            items=items,
            nom_client=nom_client,
            telephone=telephone,
            heure_retrait=heure_retrait,
            notes=notes,
            shop_name=shop_name
        )
        return self.print_receipt(txt, numero=f"TAK-{numero_commande}")

    def print_promo_ticket(self, code_promo, description, pourcentage=None, montant_fixe=None, date_expiration=None, shop_name="Kōdo POS") -> bool:
        """Génère et imprime un bon de réduction / ticket promo."""
        txt = generer_ticket_promo(
            code_promo=code_promo,
            description=description,
            pourcentage=pourcentage,
            montant_fixe=montant_fixe,
            date_expiration=date_expiration,
            shop_name=shop_name
        )
        return self.print_receipt(txt, numero=f"PROMO-{code_promo}")


# ---------------------------------------------------------------------------
# GÉNÉRATEURS DE TICKETS DE CAISSE (VENTE, A EMPORTER, PROMO)
# ---------------------------------------------------------------------------

# Un ticket de caisse belge porte obligatoirement le numéro de TVA du commerçant.
# Aucun numéro par défaut n'est acceptable : un numéro inventé sur un document fiscal
# est plus grave qu'une mention manquante. Tant que le commerçant ne l'a pas saisi dans
# Paramètres, le ticket le signale explicitement au lieu de sortir silencieusement
# non conforme ou au nom d'un tiers.
TVA_NON_RENSEIGNEE = ""

def generer_ticket(numero, panier, total_tvac, remise,
                   paiements, rendu_monnaie,
                   nom_client=None, shop_name="Mon Commerce",
                   shop_subtitle="Boutique",
                   shop_address="",
                   shop_vat=TVA_NON_RENSEIGNEE,
                   vendeur_nom="Caissier",
                   is_gift=False,
                   ecart_arrondi_cash=None):
    """
    Génère le contenu texte d'un ticket thermique de caisse standard.
    """
    now = datetime.datetime.now()
    lines = []

    # En-tête
    lines.append(_separator("="))
    lines.append(_center(shop_name))
    if is_gift:
        lines.append(_center("*** TICKET CADEAU ***"))
    elif shop_subtitle:
        lines.append(_center(shop_subtitle))
    if shop_address:
        lines.append(_center(shop_address))
    if shop_vat:
        vat_str = shop_vat if str(shop_vat).startswith("TVA") else f"TVA: {shop_vat}"
        lines.append(_center(vat_str))
    else:
        # Rien d'imprime ici. Un ticket sans ligne de TVA ne surprend personne ;
        # « Parametres > Boutique » est un chemin de menu destine au commercant,
        # pas a sa cliente. L'alerte reste bien visible sur l'ecran de caisse
        # (ReceiptModal), que le commercant voit a chaque vente.
        pass
    lines.append(_separator("="))
    
    # Traçabilité
    date_str = now.strftime("%d/%m/%Y %H:%M")
    # Aucun nom invente : le ticket sert la tracabilite, attribuer une vente a
    # une personne codee en dur est une fausse mention sur un document fiscal.
    v_name = vendeur_nom if vendeur_nom else "Non renseigné"
    lines.append(f"Date   : {date_str} Ticket : {numero}")
    lines.append(f"Caisse : Caisse 01      Vendeur: {v_name}")
    if nom_client:
        lines.append(f"Client : {nom_client[:25]}")
    lines.append(_separator("-"))
    
    # Entête Tableau Articles
    if is_gift:
        lines.append(f"{'QTY':<4}{'ITEM DESCRIPTION':<38}")
    else:
        lines.append(f"{'QTY':<4}{'ITEM DESCRIPTION':<25}{'PRICE (EUR)':>13}")
    lines.append(_separator("-"))
    
    total_htva_accum = Decimal("0.00")
    total_tva_accum = Decimal("0.00")
    tva_breakdown = {}

    for item in panier:
        qte = item.get("quantite", 1)
        nom = item["nom"]
        if item.get("taille") and item["taille"] not in ("—", "Unique", ""):
            nom = f"{nom} [{item['taille']}]"
        
        t = item.get("taux_tva", Decimal("0.21"))
        if not isinstance(t, Decimal):
            t = Decimal(str(t))
        p_unit = item["prix_vente_tvac"]
        if not isinstance(p_unit, Decimal):
            p_unit = Decimal(str(p_unit))
        p_total = p_unit * qte

        htva = (p_total / (Decimal("1") + t)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tva = p_total - htva

        total_htva_accum += htva
        total_tva_accum += tva

        rate_key = f"{float(t)*100:.0f}%"
        if rate_key not in tva_breakdown:
            tva_breakdown[rate_key] = {"base": Decimal("0.00"), "tva": Decimal("0.00")}
        tva_breakdown[rate_key]["base"] += htva
        tva_breakdown[rate_key]["tva"] += tva

        prix_str = f"{p_total:.2f}"
        qte_str = str(qte)

        if is_gift:
            if len(nom) <= 37:
                lines.append(f"{qte_str:<4}{nom:<38}")
            else:
                lines.append(f"{qte_str:<4}{nom[:37]:<38}")
                rest = nom[37:]
                while rest:
                    lines.append(f"    {rest[:38]}")
                    rest = rest[38:]
        else:
            if len(nom) <= 24:
                lines.append(f"{qte_str:<4}{nom:<25}{prix_str:>13}")
            else:
                lines.append(f"{qte_str:<4}{nom[:24]:<25}{prix_str:>13}")
                rest = nom[24:]
                while rest:
                    lines.append(f"    {rest[:25]}")
                    rest = rest[25:]
            
    lines.append(_separator("-"))

    if is_gift:
        lines.append(_separator("-"))
        lines.append(_center("MERCI DE VOTRE VISITE !"))
        lines.append(_center("Échange sous 14 jours sur présentation"))
        lines.append(_center("de ce ticket cadeau. Articles non portés"))
        lines.append(_center("et dans leur emballage d'origine."))
        lines.append(_separator("="))
    else:
        if remise and Decimal(str(remise)) > Decimal("0"):
            rem_dec = Decimal(str(remise))
            lines.append(_right("REMISE          :", f"-{rem_dec:.2f} EUR"))
            lines.append(_separator("-"))
            
        # TOTAUX (HTVA & TVA)
        lines.append(_right("SUBTOTAL (HTVA) :", f"{total_htva_accum:.2f} EUR"))
        lines.append(_right("TAX (VAT)       :", f"{total_tva_accum:.2f} EUR"))
        lines.append(_separator("="))
        if ecart_arrondi_cash and Decimal(str(ecart_arrondi_cash)) != Decimal("0.00"):
            ecart_dec = Decimal(str(ecart_arrondi_cash))
            tot_arrondi = Decimal(str(total_tvac)) + ecart_dec
            lines.append(_right("TOTAL BRUT      :", f"{Decimal(str(total_tvac)):.2f} EUR"))
            lines.append(_right("ARRONDI LÉGAL 5c:", f"{ecart_dec:+.2f} EUR"))
            lines.append(_right("TOTAL À PAYER   :", f"{tot_arrondi:.2f} EUR"))
        else:
            lines.append(_right("TOTAL TO PAY    :", f"{Decimal(str(total_tvac)):.2f} EUR"))
        lines.append(_separator("="))
        
        # Règlements
        for methode, montant in paiements:
            m_str = str(methode).upper()
            if str(methode).lower() in ("qr_code", "bancontact/mobile", "carte", "cb", "bancontact"):
                m_str = "CARTE"
            elif str(methode).lower() in ("especes", "espèces", "cash"):
                m_str = "ESPECES"
            lbl = f"PAID BY {m_str}"
            lbl_padded = f"{lbl:<16}:"
            m_dec = Decimal(str(montant))
            lines.append(_right(lbl_padded, f"{m_dec:.2f} EUR"))
        
        if rendu_monnaie and Decimal(str(rendu_monnaie)) > Decimal("0"):
            rendu_dec = Decimal(str(rendu_monnaie))
            lines.append(_right("CHANGE RETURNED :", f"{rendu_dec:.2f} EUR"))
            
        # Détail des taxes (TVA)
        lines.append("")
        lines.append(_center("DÉTAIL DES TAXES (TVA)"))
        lines.append(f"{'Taux':<10}{'Base HTVA':>14}{'Montant TVA':>18}")
        for rate, vals in tva_breakdown.items():
            base_str = f"{vals['base']:.2f} EUR"
            tax_str = f"{vals['tva']:.2f} EUR"
            lines.append(f"{rate:<10}{base_str:>14}{tax_str:>18}")
        
        # Pied de page
        lines.append(_separator("-"))
        lines.append(_center("MERCI DE VOTRE VISITE !"))
        lines.append(_center("Échange sous 14 jours sur présentation"))
        lines.append(_center("de ce ticket. Articles non portés et"))
        lines.append(_center("dans leur emballage d'origine."))
        lines.append(_separator("="))
    
    lines.append("")
    lines.append("\n")

    return "\n".join(lines)


def generer_ticket_takeaway(numero_commande, items, nom_client=None, telephone=None, heure_retrait=None, notes=None, shop_name="Kōdo Food", vendeur_nom=None):
    """
    Génère un ticket de vente à emporter (Takeaway / Restauration / Click & Collect).
    """
    now = datetime.datetime.now()
    lines = []
    lines.append(_separator("="))
    lines.append(_center(shop_name))
    lines.append(_center("*** VENTE À EMPORTER ***"))
    lines.append(_separator("="))

    lines.append(f"Commande N° : TAK-{numero_commande}")
    lines.append(f"Date        : {now.strftime('%d/%m/%Y %H:%M')}")
    if heure_retrait:
        lines.append(f"Heure Retrait: {heure_retrait}")
    if vendeur_nom:
        lines.append(f"Pris par    : {vendeur_nom}")
    if nom_client:
        lines.append(f"Client      : {nom_client}")
    if telephone:
        lines.append(f"Téléphone   : {telephone}")
    lines.append(_separator("-"))

    lines.append(f"{'QTY':<4}{'ARTICLE & OPTIONS':<38}")
    lines.append(_separator("-"))

    total_items = 0
    for item in items:
        qte = item.get("quantite", 1)
        nom = item.get("nom", "Article")
        total_items += qte
        lines.append(f"{qte:<4}{nom:<38}")
        opts = item.get("options", [])
        for opt in opts:
            lines.append(f"    + {opt}")
        if item.get("note"):
            lines.append(f"    * NOTE: {item['note']}")

    lines.append(_separator("-"))
    lines.append(f"Nombre total d'articles: {total_items}")
    if notes:
        lines.append(_separator("-"))
        lines.append(f"INSTRUCTIONS SPECIALES:")
        lines.append(notes)
    lines.append(_separator("="))
    lines.append(_center("PREPARATION COMMANDE"))
    lines.append(_separator("="))
    lines.append("\n\n")

    return "\n".join(lines)


def generer_ticket_promo(code_promo, description, pourcentage=None, montant_fixe=None, date_expiration=None, shop_name="Mon Commerce", min_achat=None):
    """
    Génère un bon de réduction / ticket promotionnel.
    """
    lines = []
    lines.append(_separator("="))
    lines.append(_center(shop_name))
    lines.append(_center("*** BON DE RÉDUCTION ***"))
    lines.append(_separator("="))

    lines.append(_center(f"CODE PROMO : {code_promo}"))
    lines.append(_separator("-"))
    lines.append(_center(description))

    if pourcentage:
        lines.append(_center(f"REMISE DE -{pourcentage}%"))
    elif montant_fixe:
        lines.append(_center(f"REMISE DE -{montant_fixe:.2f} EUR"))

    if min_achat:
        lines.append(_center(f"Valable dès {min_achat:.2f} EUR d'achat"))

    if date_expiration:
        lines.append(_center(f"Valable jusqu'au {date_expiration}"))

    lines.append(_separator("-"))
    lines.append(_center("Présentez ce bon lors de votre prochain passage."))
    lines.append(_center("Non cumulable avec d'autres promotions."))
    lines.append(_separator("="))
    lines.append("\n\n")

    return "\n".join(lines)


def get_ticket_logo_path():
    """
    Retourne le chemin d'accès au logo pour le ticket de caisse.
    Cherche en priorité le logo personnalisé téléversé par l'utilisateur.
    Si aucun logo n'a été téléversé ou s'il a été supprimé, retourne None
    (aucun logo par défaut d'un autre client n'est jamais injecté).
    """
    try:
        import database_manager
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("SELECT cle, valeur FROM Parametres WHERE cle IN ('receipt_logo_b64', 'receipt_logo_custom')")
        params = dict(c.fetchall())
        conn.close()

        # Si le logo n'est pas explicitement personnalisé ou s'il est absent
        if params.get("receipt_logo_custom") != "1" and "receipt_logo_b64" not in params:
            return None

        raw_b64 = params.get("receipt_logo_b64")
        if raw_b64:
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            import base64
            img_bytes = base64.b64decode(raw_b64)
            target_p = database_manager.data_path("logo_ticket.png")
            try:
                os.makedirs(os.path.dirname(target_p), exist_ok=True)
                with open(target_p, "wb") as f:
                    f.write(img_bytes)
                return target_p
            except Exception:
                pass
    except Exception:
        pass

    return None


def generate_social_qr_image(title=None, url=None, subtitle=None, header=None, width=512, qr_size="large"):
    """
    Génère un bloc visuel de communication avec disposition horizontale élégante :
    QR Code à gauche, ligne séparatrice verticale, et textes hiérarchisés à droite
    (chapeau, titre réseau en gras, et identifiant/@compte/message).
    """
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    url_str = (url or "https://kōdo-solutions.com").strip()

    # Taille du QR Code
    if qr_size == "extra_large":
        box_size = 6
    elif qr_size == "normal":
        box_size = 4
    else:  # "large" par défaut (environ 120-130px de côté)
        box_size = 5

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(url_str)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_w, qr_h = qr_img.size

    # Polices de caractères optimisées pour l'impression thermique
    possible_regular_fonts = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    possible_bold_fonts = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ]

    font_header = None
    font_title = None
    font_sub = None

    for p in possible_regular_fonts:
        if os.path.exists(p):
            try:
                font_header = ImageFont.truetype(p, 14)
                break
            except Exception:
                pass
    for p in possible_bold_fonts:
        if os.path.exists(p):
            try:
                font_title = ImageFont.truetype(p, 22)
                font_sub = ImageFont.truetype(p, 20)
                break
            except Exception:
                pass

    if not font_header:
        font_header = ImageFont.load_default()
    if not font_title:
        font_title = ImageFont.load_default()
    if not font_sub:
        font_sub = ImageFont.load_default()

    header_text = (header or "").strip()
    title_text = (title or "").strip()
    sub_text = (subtitle or "").strip()

    # Si aucun chapeau fourni mais un titre est présent, déduction intelligente
    if not header_text and title_text:
        upper_title = title_text.upper()
        if any(k in upper_title for k in ("INSTAGRAM", "TIKTOK", "FACEBOOK", "RESEAU")):
            header_text = "SUIVEZ-NOUS SUR"
        elif any(k in upper_title for k in ("SITE", "WEB", "SHOP", "BOUTIQUE EN LIGNE")):
            header_text = "VISITEZ NOTRE SITE"
        elif any(k in upper_title for k in ("AVIS", "GOOGLE", "ETOILE")):
            header_text = "VOTRE AVIS COMPTE"

    dummy_draw = ImageDraw.Draw(Image.new("RGB", (1, 1), "white"))
    gap_lines = 4

    b_hdr = dummy_draw.textbbox((0, 0), header_text, font=font_header) if header_text else (0, 0, 0, 0)
    b_ttl = dummy_draw.textbbox((0, 0), title_text, font=font_title) if title_text else (0, 0, 0, 0)
    b_sub = dummy_draw.textbbox((0, 0), sub_text, font=font_sub) if sub_text else (0, 0, 0, 0)

    h_hdr = (b_hdr[3] - b_hdr[1]) if header_text else 0
    h_ttl = (b_ttl[3] - b_ttl[1]) if title_text else 0
    h_sub = (b_sub[3] - b_sub[1]) if sub_text else 0

    text_lines_count = sum(1 for h in [h_hdr, h_ttl, h_sub] if h > 0)
    text_total_h = h_hdr + h_ttl + h_sub + max(0, text_lines_count - 1) * gap_lines

    sep_spacing = 20
    text_w = max(b_hdr[2] - b_hdr[0], b_ttl[2] - b_ttl[0], b_sub[2] - b_sub[0], 0)
    content_w = qr_w + (sep_spacing * 2 + 1 + text_w if text_total_h > 0 else 0)

    pad_y = 14
    max_h = max(qr_h, text_total_h)
    total_h = max_h + 2 * pad_y

    img = Image.new("RGB", (width, total_h), "white")
    draw = ImageDraw.Draw(img)

    # Position horizontale centrée sur la largeur du ticket (512 dots)
    start_x = max(10, (width - content_w) // 2)
    qr_y = (total_h - qr_h) // 2
    img.paste(qr_img, (start_x, qr_y))

    if text_total_h > 0:
        sep_x = start_x + qr_w + sep_spacing
        line_y1 = max(pad_y, (total_h - max(qr_h, text_total_h)) // 2 + 4)
        line_y2 = min(total_h - pad_y, line_y1 + max(qr_h, text_total_h) - 8)
        draw.line([(sep_x, line_y1), (sep_x, line_y2)], fill=(180, 180, 180), width=1)

        text_x = sep_x + sep_spacing
        cur_text_y = (total_h - text_total_h) // 2

        if header_text:
            draw.text((text_x, cur_text_y), header_text, fill="black", font=font_header)
            cur_text_y += h_hdr + gap_lines
        if title_text:
            draw.text((text_x, cur_text_y), title_text, fill="black", font=font_title)
            cur_text_y += h_ttl + gap_lines
        if sub_text:
            draw.text((text_x, cur_text_y), sub_text, fill="black", font=font_sub)

    return img


def get_ticket_social_path():
    """
    Retourne le chemin d'accès au bloc réseaux sociaux / communication du ticket de caisse.
    Cherche en priorité le bloc personnalisé configuré par l'utilisateur (QR Code ou image personnalisée).
    Si le bloc est désactivé ('none') ou non configuré, retourne None.
    Si le bloc n'existe pas sur disque mais est présent en base SQLite (Parametres), le régénère.
    """
    try:
        import database_manager
        conn = database_manager.get_connection()
        c = conn.cursor()
        c.execute("SELECT cle, valeur FROM Parametres WHERE cle LIKE 'receipt_social_%'")
        params = dict(c.fetchall())
        conn.close()

        mode = params.get("receipt_social_mode")
        if not mode or mode == "none":
            return None

        raw_b64 = params.get("receipt_social_b64")

        # Régénération automatique si mode QR sans image stockée
        if not raw_b64 and mode == "qr":
            header = params.get("receipt_social_header", "")
            title = params.get("receipt_social_title", "INSTAGRAM")
            url = params.get("receipt_social_url", "https://kōdo-solutions.com")
            subtitle = params.get("receipt_social_subtitle", "")
            qr_size = params.get("receipt_social_size", "large")
            img = generate_social_qr_image(title=title, url=url, subtitle=subtitle, header=header, width=512, qr_size=qr_size)
            from io import BytesIO
            import base64
            buf = BytesIO()
            img.save(buf, format="PNG")
            raw_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

        if raw_b64:
            if "," in raw_b64:
                raw_b64 = raw_b64.split(",", 1)[1]
            import base64
            img_bytes = base64.b64decode(raw_b64)
            target_p = database_manager.data_path("social_ticket.png")
            try:
                os.makedirs(os.path.dirname(target_p), exist_ok=True)
                with open(target_p, "wb") as f:
                    f.write(img_bytes)
                return target_p
            except Exception:
                pass
    except Exception:
        pass

    return None


def generer_image_ticket(contenu, numero):
    """
    Génère une image PNG du ticket complet (Logo + Texte + Instagram/QR Code)
    pour impression graphique via PIL.
    """
    from PIL import Image, ImageDraw, ImageFont

    logo_path = get_ticket_logo_path()
    insta_path = get_ticket_social_path()

    img_logo = None
    img_insta = None
    if logo_path and os.path.exists(logo_path):
        try:
            img_logo = Image.open(logo_path).convert("RGBA")
        except Exception:
            pass
    if insta_path and os.path.exists(insta_path):
        try:
            img_insta = Image.open(insta_path).convert("RGBA")
        except Exception:
            pass

    width = 512  # Largeur 80mm
    font = None
    font_size = 18
    possible_fonts = [
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "C:\\Windows\\Fonts\\cour.ttf",
    ]
    for path in possible_fonts:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    lines = contenu.strip("\n").split("\n")
    line_height = 24
    text_section_height = len(lines) * line_height + 20

    total_height = text_section_height + 40
    if img_logo:
        total_height += img_logo.height + 20
    if img_insta:
        total_height += img_insta.height + 20

    ticket_img = Image.new("RGB", (width, total_height), "white")
    draw = ImageDraw.Draw(ticket_img)

    current_y = 20

    # Logos
    if img_logo:
        logo_x = (width - img_logo.width) // 2
        ticket_img.paste(img_logo, (logo_x, current_y), img_logo)
        current_y += img_logo.height + 20

    # Texte
    for line in lines:
        draw.text((20, current_y), line, fill="black", font=font)
        current_y += line_height

    current_y += 10

    # Block Insta
    if img_insta:
        insta_x = (width - img_insta.width) // 2
        ticket_img.paste(img_insta, (insta_x, current_y), img_insta)
        current_y += img_insta.height + 20

    try:
        from database_manager import data_path
        nom_fichier_img = data_path(f"ticket_virtuel_{numero}.png")
    except Exception:
        nom_fichier_img = os.path.join(tempfile.gettempdir(), f"ticket_virtuel_{numero}.png")

    ticket_img.save(nom_fichier_img)
    return nom_fichier_img


def pil_to_escpos_raster(image, max_width=512, max_height=None):
    """
    Convertit une image PIL en bytes d'impression ESC/POS (Commande GS v 0).
    Ajusté pour une largeur d'impression 80mm nette et découpé par tranches de 48 dots
    pour éviter tout débordement de buffer sur les imprimantes thermiques sensibles.
    """
    from PIL import Image
    if image.width > max_width:
        ratio = max_width / float(image.width)
        new_height = int(float(image.height) * ratio)
        image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)

    if max_height and image.height > max_height:
        ratio = max_height / float(image.height)
        new_width = int(float(image.width) * ratio)
        image = image.resize((new_width, max_height), Image.Resampling.LANCZOS)

    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == 'RGBA':
            bg.paste(image, mask=image.split()[-1])
        else:
            bg.paste(image)
        image = bg

    if image.mode != '1':
        image = image.convert('L').point(lambda p: 255 if p > 160 else 0, mode='1')

    width, height = image.size
    byte_width = (width + 7) // 8
    pixels = image.load()

    # Découpage par tranches horizontales (slice chunking de 24 dots)
    # Empêche la saturation du buffer matériel de l'imprimante (micro-buffer <= 2048 octets : 64*24+8 = 1544 <= 2048)
    chunk_h = 24
    payload = bytearray()

    for y_start in range(0, height, chunk_h):
        cur_h = min(chunk_h, height - y_start)
        header = bytearray([
            0x1D, 0x76, 0x30, 0x00,
            byte_width & 0xFF, (byte_width >> 8) & 0xFF,
            cur_h & 0xFF, (cur_h >> 8) & 0xFF
        ])
        slice_data = bytearray()
        for y in range(y_start, y_start + cur_h):
            for x_byte in range(byte_width):
                byte_val = 0
                for bit in range(8):
                    x = x_byte * 8 + bit
                    if x < width:
                        if pixels[x, y] == 0:  # Pixel noir
                            byte_val |= (1 << (7 - bit))
                slice_data.append(byte_val)
        payload.extend(header + slice_data)

    return bytes(payload)


def imprimer_ticket(contenu, numero, printer_name=None, host=None, port=9100, allow_gui_preview=False, return_status=False):
    """
    Sauvegarde le ticket et tente l'impression thermique ESC/POS.
    1. Direct Hardware python-escpos / Socket si hôte spécifié.
    2. Driver ESCPOSThermalPrinter multiplateforme (CUPS / win32print / lp).
    3. Fallback sur ouverture d'un aperçu texte (si allow_gui_preview=True).
    """
    from PIL import Image
    contenu_clean = sanitize_escpos_text(contenu)

    try:
        from database_manager import data_path
        nom_fichier_txt = data_path(f"ticket_virtuel_{numero}.txt")
        nom_fichier_bin = data_path(f"ticket_virtuel_{numero}.bin")
    except Exception:
        nom_fichier_txt = os.path.join(tempfile.gettempdir(), f"ticket_virtuel_{numero}.txt")
        nom_fichier_bin = os.path.join(tempfile.gettempdir(), f"ticket_virtuel_{numero}.bin")

    # 1. Sauvegarde TXT
    with open(nom_fichier_txt, "w", encoding="utf-8") as f:
        f.write(contenu_clean)

    # 2. Image ticket PNG
    nom_fichier_img = generer_image_ticket(contenu, numero)

    # 3. Payload ESC/POS
    raw_payload = bytearray(ESC_INIT + ESC_ALIGN_CENTER)
    logo_path = get_ticket_logo_path()
    if logo_path and os.path.exists(logo_path):
        try:
            img_logo = Image.open(logo_path)
            raw_payload.extend(pil_to_escpos_raster(img_logo, max_width=384, max_height=180))
            raw_payload.extend(b"\n")
        except Exception as e:
            print(f"[WARN] Logo raster error: {e}")

    raw_payload.extend(ESC_ALIGN_LEFT)
    raw_payload.extend(contenu_clean.encode('ascii', errors='replace'))
    raw_payload.extend(b"\n" + ESC_ALIGN_CENTER)

    insta_path = get_ticket_social_path()
    if insta_path and os.path.exists(insta_path):
        try:
            img_insta = Image.open(insta_path)
            raw_payload.extend(pil_to_escpos_raster(img_insta))
            raw_payload.extend(b"\n")
        except Exception as e:
            print(f"[WARN] Insta raster error: {e}")

    raw_payload.extend(b"\n\n\n\n\n\n" + GS_CUT_FUNCTION)

    with open(nom_fichier_bin, "wb") as f:
        f.write(raw_payload)

    # Tentative avec le driver unifié ESCPOSThermalPrinter
    driver = ESCPOSThermalPrinter(printer_name=printer_name, host=host, port=port)
    printed_successfully = driver.send_raw(bytes(raw_payload))

    # Fallback 1: python-escpos USB si échec
    if not printed_successfully:
        try:
            from escpos.printer import Usb
            LOW_BUDGET_PRINTERS = [
                (0x04b8, 0x0202), (0x0416, 0x5011), (0x04b8, 0x0e20),
                (0x0483, 0x5740), (0x1fc9, 0x2016)
            ]
            p = None
            for vid, pid in LOW_BUDGET_PRINTERS:
                try:
                    import usb.core
                    backend = None
                    try:
                        import libusb_package
                        backend = libusb_package.get_libusb1_backend()
                    except Exception:
                        pass
                    if usb.core.find(idVendor=vid, idProduct=pid, backend=backend) is not None:
                        p = Usb(vid, pid)
                        break
                except Exception:
                    continue

            if p is not None:
                if logo_path and os.path.exists(logo_path):
                    try: p.image(logo_path, impl="bitImageColumn")
                    except Exception: pass
                p.text(contenu_clean)
                if insta_path and os.path.exists(insta_path):
                    try: p.image(insta_path, impl="bitImageColumn")
                    except Exception: pass
                p.cut()
                printed_successfully = True
                print("[SUCCESS] Ticket imprimé via python-escpos USB.")
        except Exception as e:
            print(f"[INFO ESC/POS USB] {e}")

    # Fallback 2: Aperçu fichier (uniquement si explicitement demandé, ex: test manuel)
    if not printed_successfully and allow_gui_preview:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", nom_fichier_txt])
            elif sys.platform == "win32":
                os.startfile(nom_fichier_txt)
            else:
                subprocess.Popen(["xdg-open", nom_fichier_txt])
            print("Fallback : Aperçu du ticket ouvert à l'écran.")
        except Exception as e:
            print(f"Erreur ouverture aperçu ticket : {e}")

    if return_status:
        return bool(printed_successfully)
    return nom_fichier_txt


def imprimer_ticket_caisse(num_ticket, printer_name=None, host=None, port=9100) -> bool:
    """
    Récupère un ticket depuis la base de données SQLite et lance son impression.
    Retourne True si l'impression physique a réussi, False sinon.
    """
    try:
        from database_manager import get_connection
        conn = get_connection()
        c = conn.cursor()

        c.execute("""
            SELECT id, date_heure, total_tvac, remise, methode_paiement, id_client, rendu_monnaie, vendeur_nom, ecart_arrondi_cash
            FROM Tickets WHERE numero_ticket = ?
        """, (num_ticket,))
        ticket_row = c.fetchone()
        if not ticket_row:
            print(f"[WARN] Ticket {num_ticket} introuvable en base de données.")
            return False

        t_id, d_h, total_tvac, remise, methode, id_client, rendu, vendeur, ecart_arrondi = ticket_row

        # Articles
        c.execute("""
            SELECT p.nom, s.taille, vd.quantite, vd.prix_unitaire_tvac, vd.taux_tva
            FROM Ventes_Details vd
            LEFT JOIN Stocks s ON vd.id_stock = s.id
            LEFT JOIN Produits p ON s.id_produit = p.id
            WHERE vd.id_ticket = ?
        """, (t_id,))
        details = c.fetchall()

        panier = []
        for nom, taille, qte, pu, taux in details:
            panier.append({
                "nom": nom or "Article",
                "taille": taille or "",
                "quantite": qte,
                "prix_vente_tvac": Decimal(str(pu)),
                "taux_tva": Decimal(str(taux))
            })

        nom_client = None
        if id_client:
            c.execute("SELECT nom, prenom FROM Clients WHERE id = ?", (id_client,))
            cli = c.fetchone()
            if cli:
                nom_client = f"{cli[1]} {cli[0]}".strip()

        # Règlements réels enregistrés dans le grand livre
        c.execute("""
            SELECT methode_paiement, montant FROM Ledger_Caisse
            WHERE reference = ? AND type_mouvement = 'VENTE'
        """, (num_ticket,))
        ledger_pmts = c.fetchall()
        if ledger_pmts:
            paiements = [(m, Decimal(str(mt))) for m, mt in ledger_pmts]
        else:
            ecart_dec = Decimal(str(ecart_arrondi or 0))
            is_cash = str(methode or "").strip().lower() in ("especes", "espèces", "cash")
            mt_base = Decimal(str(total_tvac)) + (ecart_dec if is_cash else Decimal("0.00"))
            paiements = [(methode or "Espèces", mt_base)]

        # Infos Boutique
        shop_name = "Mon Commerce"
        shop_sub = "Boutique"
        shop_addr = ""
        shop_vat = TVA_NON_RENSEIGNEE
        try:
            c.execute("SELECT cle, valeur FROM Parametres WHERE cle LIKE 'shop_%'")
            params = dict(c.fetchall())
            shop_name = params.get("shop_name", shop_name)
            shop_sub = params.get("shop_subtitle", shop_sub)
            shop_addr = params.get("shop_address", shop_addr)
            shop_vat = params.get("shop_vat", shop_vat)
        except Exception:
            pass

        conn.close()

        contenu = generer_ticket(
            numero=num_ticket,
            panier=panier,
            total_tvac=Decimal(str(total_tvac)),
            remise=Decimal(str(remise or 0)),
            paiements=paiements,
            rendu_monnaie=Decimal(str(rendu or 0)),
            nom_client=nom_client,
            shop_name=shop_name,
            shop_subtitle=shop_sub,
            shop_address=shop_addr,
            shop_vat=shop_vat,
            vendeur_nom=vendeur or "Non renseigné",
            ecart_arrondi_cash=Decimal(str(ecart_arrondi or 0))
        )

        return bool(imprimer_ticket(contenu, num_ticket, printer_name=printer_name, host=host, port=port, return_status=True))

    except Exception as e:
        print(f"[ERROR imprimer_ticket_caisse] {e}")
        return False


def ouvrir_tiroir_caisse(printer_name=None, host=None, port=9100):
    """
    Envoie l'impulsion électrique (ESC/POS) pour ouvrir le tiroir-caisse.
    """
    driver = ESCPOSThermalPrinter(printer_name=printer_name, host=host, port=port)
    res = driver.open_cash_drawer(pin=0)
    if res:
        print("[SUCCESS] Signal d'ouverture tiroir caisse envoyé.")
        return True

    # Fallback générique via lp -o raw
    temp_path = None
    try:
        drawer_cmd = ESC_INIT + ESC_DRAWER_PIN2 + DLE_DRAWER_PULSE
        fd, temp_path = tempfile.mkstemp(prefix="drawer_", suffix=".bin")
        with os.fdopen(fd, 'wb') as f:
            f.write(drawer_cmd)

        if sys.platform in ["darwin", "linux"]:
            res = subprocess.run(["lp", "-o", "raw", temp_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2.0)
            if res.returncode == 0:
                print("[SUCCESS] Tiroir ouvert via lp raw.")
                return True
    except Exception as e:
        print(f"[ERROR ouvrir_tiroir_caisse] {e}")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return False


def generer_ticket_test(shop_name="KŌDO POS",
                        shop_address="",
                        shop_vat=TVA_NON_RENSEIGNEE,
                        shop_iban="",
                        printer_ip=""):
    """
    Génère le texte d'un ticket de test thermique ESC/POS 80mm.
    """
    now = datetime.datetime.now()
    lines = []
    lines.append(_separator("="))
    lines.append(_center(shop_name))
    lines.append(_center("*** TICKET TEST D'IMPRESSION ***"))
    if shop_address:
        lines.append(_center(shop_address))
    if shop_vat:
        vat_str = shop_vat if str(shop_vat).startswith("TVA") else f"TVA: {shop_vat}"
        lines.append(_center(vat_str))
    if shop_iban:
        lines.append(_center(f"IBAN: {shop_iban}"))
    lines.append(_separator("="))

    date_str = now.strftime("%d/%m/%Y %H:%M:%S")
    lines.append(f"Date   : {date_str}")
    lines.append(f"Ticket : TEST-0001      Caisse : Caisse 01")
    lines.append(f"Statut : TEST MATERIEL REUSSI")
    lines.append(_separator("-"))

    lines.append(f"{'QTE':<4}{'DESIGNATION':<25}{'PRIX (EUR)':>13}")
    lines.append(_separator("-"))
    lines.append(f"{'1':<4}{'Article Test A (Taille M)':<25}{'15.00':>13}")
    lines.append(f"{'1':<4}{'Impr. Thermique ESC/POS':<25}{'5.00':>13}")
    lines.append(_separator("-"))

    lines.append(f"{'TOTAL TVAC':<25}{'20.00 EUR':>17}")
    lines.append(f"{'Paiement Test':<25}{'20.00 EUR':>17}")
    lines.append(_separator("-"))
    lines.append("DETAIL TVA :")
    lines.append(f"  Taux 21.0% : HTVA 16.53 EUR | TVA 3.47 EUR")
    lines.append(_separator("="))
    lines.append(_center("TEST MATERIEL & COMMUNICATION"))
    lines.append(_center("Vitesse : OK | Decoupe : OK"))
    try:
        import kodo_base
        version_pos = kodo_base.BASE_VERSION
    except Exception:
        version_pos = "2.0.4"
    lines.append(_center(f"Kōdo POS v{version_pos}"))
    lines.append(_separator("-"))
    lines.append(_center("Merci pour votre confiance !"))
    lines.append(_center("https://kōdo-solutions.com"))
    lines.append(_separator("="))
    lines.append("\n\n")

    return "\n".join(lines)


def imprimer_ticket_test(printer_name=None, host=None, port=9100):
    """
    Imprime un ticket de test sur l'imprimante thermique configurée.
    Récupère automatiquement les paramètres boutique depuis SQLite.
    """
    try:
        from database_manager import get_connection
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT cle, valeur FROM Parametres")
        params = {r[0]: r[1] for r in c.fetchall()}
        conn.close()
    except Exception:
        params = {}

    shop_name = params.get("shop_name", "KŌDO POS")
    shop_addr = params.get("shop_address", "")
    shop_vat = params.get("shop_tva", params.get("shop_bce", TVA_NON_RENSEIGNEE))
    shop_iban = params.get("shop_iban", "")
    printer_ip = host or params.get("printer_ip", "192.168.1.150")

    txt = generer_ticket_test(
        shop_name=shop_name,
        shop_address=shop_addr,
        shop_vat=shop_vat,
        shop_iban=shop_iban,
        printer_ip=printer_ip
    )

    num_test = datetime.datetime.now().strftime("TEST-%H%M%S")
    path_or_success = imprimer_ticket(txt, numero=num_test, printer_name=printer_name, host=printer_ip, port=port)
    return {
        "success": True,
        "receiptNumber": num_test,
        "file": str(path_or_success),
        "printerIP": printer_ip,
        "content": txt
    }


def imprimer_pdf_etiquette(pdf_path, printer_name, media=None, copies=1):
    """
    Envoie un PDF d'étiquette à CUPS en laissant les filtres faire leur travail.

    Ne PAS passer par `ESCPOSThermalPrinter.send_raw` : celui-ci force `-o raw`,
    qui court-circuite les filtres CUPS. Un PDF arriverait alors tel quel à
    l'étiqueteuse, qui ne sait pas l'interpréter.

    `media` doit correspondre EXACTEMENT au format du PDF (nom de format du PPD,
    par exemple « w101h252 », ou « Custom.LARGEURxHAUTEURmm »). Sans lui, CUPS
    impose le format par défaut du PPD et dessine la page SANS mise à l'échelle
    (« Drawing unscaled page » dans cgpdftoraster) : une page plus large que le
    support est alors rognée, code-barres compris.

    `printer_name` est obligatoire : on n'envoie jamais une étiquette sur la file
    par défaut, qui est l'imprimante à tickets.
    """
    if not pdf_path or not os.path.exists(pdf_path):
        print(f"[ETIQUETTE] PDF introuvable : {pdf_path}")
        return False
    if not printer_name:
        print("[ETIQUETTE] Aucune étiqueteuse configurée : impression annulée.")
        return False

    cmd = ["lp", "-d", str(printer_name), "-n", str(max(1, int(copies or 1)))]
    if media:
        cmd += ["-o", f"media={media}"]
    # Pas de mise à l'échelle : le PDF est déjà produit à la taille du support.
    cmd += ["-o", "fit-to-page=false", str(pdf_path)]

    try:
        # 10 s (et non les 2 s du chemin ticket) : un PDF traverse une chaîne de
        # filtres CUPS, là où le ticket part en octets bruts sur une socket.
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0)
    except Exception as e:
        print(f"[ETIQUETTE] Échec de l'envoi à CUPS : {e}")
        return False

    if res.returncode == 0:
        print(f"[SUCCESS] Étiquette envoyée à '{printer_name}' (media={media or 'défaut'}).")
        return True

    print(f"[ETIQUETTE] lp a échoué (code {res.returncode}) : {res.stderr.decode(errors='replace').strip()}")
    return False

