"""
Générateur de ticket thermique 80mm (42 colonnes).
Compatible imprimante ESC/POS via fichier texte.
"""
import os, sys, subprocess, datetime, tempfile
# Patch global pour utiliser libusb-package s'il est disponible (évite l'erreur "No backend available" sur macOS/Windows)
try:
    import usb.backend.libusb1
    import libusb_package
    _old_get_backend = usb.backend.libusb1.get_backend
    usb.backend.libusb1.get_backend = lambda *a, **k: _old_get_backend(find_library=libusb_package.find_library)
except Exception:
    pass
from decimal import Decimal

COL = 42  # Largeur standard 80mm

def get_resource_path(relative_path):
    """
    Retourne le chemin absolu de la ressource.
    Fonctionne en mode de développement et dans un bundle PyInstaller (sys._MEIPASS).
    """
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def _center(text):
    return text.center(COL)

def _right(label, value, width=COL):
    space = width - len(label) - len(value)
    return label + " " * max(space, 1) + value

def _separator(char="-"):
    return char * COL

def generer_ticket(numero, panier, total_tvac, remise,
                   paiements, rendu_monnaie,
                   nom_client=None, shop_name="L'ADRESSE B",
                   shop_subtitle="Boutique de Mode",
                   shop_address="Chemin Rue 53, 4960 Malmedy",
                   shop_vat="BE 0123.456.789",
                   vendeur_nom="Sarah",
                   is_gift=False):
    """
    Génère le contenu texte d'un ticket thermique conforme au design exact réclamé (Photo modèle).
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
        vat_str = shop_vat if shop_vat.startswith("TVA:") else f"TVA: {shop_vat}"
        lines.append(_center(vat_str))
    lines.append(_separator("="))
    
    # Traçabilité
    date_str = now.strftime("%d/%m/%Y %H:%M")
    v_name = vendeur_nom if vendeur_nom else "Sarah"
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
        if item.get("taille") and item["taille"] not in ("—", "Unique"):
            nom = f"{nom} [{item['taille']}]"
        
        t = item.get("taux_tva", Decimal("0.21"))
        p_unit = item["prix_vente_tvac"]
        p_total = p_unit * qte

        htva = (p_total / (Decimal("1") + t)).quantize(Decimal("0.01"))
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
        if remise and remise > Decimal("0"):
            lines.append(_right("REMISE          :", f"-{remise:.2f} EUR"))
            lines.append(_separator("-"))
            
        # TOTAUX (HTVA & TVA)
        lines.append(_right("SUBTOTAL (HTVA) :", f"{total_htva_accum:.2f} EUR"))
        lines.append(_right("TAX (VAT)       :", f"{total_tva_accum:.2f} EUR"))
        lines.append(_separator("="))
        lines.append(_right("TOTAL TO PAY    :", f"{total_tvac:.2f} EUR"))
        lines.append(_separator("="))
        
        # Règlements
        for methode, montant in paiements:
            m_str = methode.upper()
            if methode.lower() in ("qr_code", "bancontact/mobile", "carte", "cb", "bancontact"):
                m_str = "CARTE"
            elif methode.lower() in ("especes", "espèces", "cash"):
                m_str = "ESPECES"
            lbl = f"PAID BY {m_str}"
            lbl_padded = f"{lbl:<16}:"
            lines.append(_right(lbl_padded, f"{montant:.2f} EUR"))
        
        if rendu_monnaie and rendu_monnaie > Decimal("0"):
            lines.append(_right("CHANGE RETURNED :", f"{rendu_monnaie:.2f} EUR"))
            
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


def strip_accents(text):
    """
    Supprime les accents et remplace les caractères spéciaux pour éviter
    les caractères bizarres (garbage) sur les imprimantes thermiques ESC/POS.
    """
    import unicodedata
    nfd_form = unicodedata.normalize('NFD', text)
    only_ascii = "".join([c for c in nfd_form if unicodedata.category(c) != 'Mn'])
    
    # Remplacements spécifiques
    replacements = {
        '’': "'",
        'œ': 'oe',
        'Œ': 'OE',
        'æ': 'ae',
        'Æ': 'AE',
        '€': 'EUR',
    }
    for k, v in replacements.items():
        only_ascii = only_ascii.replace(k, v)
        
    return only_ascii


def generer_image_ticket(contenu, numero):
    """
    Génère une image PNG du ticket complet (Logo + Texte + Instagram/QR Code)
    pour impression graphique haute résolution via le pilote d'imprimante Mac.
    """
    from PIL import Image, ImageDraw, ImageFont
    import os
    
    logo_path = get_resource_path("logo_ticket.png")
    insta_path = get_resource_path("instagram_block.png")
    
    img_logo = None
    img_insta = None
    if os.path.exists(logo_path):
        try:
            img_logo = Image.open(logo_path).convert("RGBA")
        except Exception:
            pass
    if os.path.exists(insta_path):
        try:
            img_insta = Image.open(insta_path).convert("RGBA")
        except Exception:
            pass
            
    width = 512  # Largeur standard 80mm
    
    font = None
    font_size = 18
    possible_fonts = [
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Menlo.ttc",
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
    
    # Dessiner le logo
    if img_logo:
        logo_x = (width - img_logo.width) // 2
        ticket_img.paste(img_logo, (logo_x, current_y), img_logo)
        current_y += img_logo.height + 20
        
    # Dessiner le texte du ticket
    for line in lines:
        draw.text((20, current_y), line, fill="black", font=font)
        current_y += line_height
        
    current_y += 10
    
    # Dessiner le bloc Instagram
    if img_insta:
        insta_x = (width - img_insta.width) // 2
        ticket_img.paste(img_insta, (insta_x, current_y), img_insta)
        current_y += img_insta.height + 20
        
    from database_manager import data_path
    nom_fichier_img = data_path(f"ticket_virtuel_{numero}.png")
    ticket_img.save(nom_fichier_img)
    return nom_fichier_img


def pil_to_escpos_raster(image, max_width=384):
    """
    Convertit une image PIL en bytes d'impression ESC/POS (Commande GS v 0).
    Compatible 100% avec toutes les imprimantes thermiques 80mm en mode raw/CUPS.
    """
    from PIL import Image
    if image.width > max_width:
        ratio = max_width / float(image.width)
        new_height = int(float(image.height) * ratio)
        image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)

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
    
    header = bytearray([0x1D, 0x76, 0x30, 0x00, byte_width & 0xFF, (byte_width >> 8) & 0xFF, height & 0xFF, (height >> 8) & 0xFF])
    pixels = image.load()
    raster_data = bytearray()
    
    for y in range(height):
        for x_byte in range(byte_width):
            byte_val = 0
            for bit in range(8):
                x = x_byte * 8 + bit
                if x < width:
                    if pixels[x, y] == 0:  # Pixel noir
                        byte_val |= (1 << (7 - bit))
            raster_data.append(byte_val)
            
    return bytes(header + raster_data)


def imprimer_ticket(contenu, numero):
    """
    Sauvegarde le ticket en .txt / .bin / .png et tente l'impression.
    1. Tente via 'python-escpos' (accès hardware natif).
    2. Tente l'impression d'image PNG via CUPS (Rendu parfait sans caractères spéciaux).
    3. Fallback sur 'lp -o raw' avec binaire ESC/POS enrichi.
    4. Fallback sur l'aperçu.
    """
    from PIL import Image
    contenu_clean = strip_accents(contenu)
    from database_manager import data_path
    
    # 1. Sauvegarde du fichier TXT standard pour référence / aperçu
    nom_fichier_txt = data_path(f"ticket_virtuel_{numero}.txt")
    with open(nom_fichier_txt, "w", encoding="utf-8") as f:
        f.write(contenu_clean)

    # 2. Génération de l'image haute définition du ticket (Logo + Texte + Instagram)
    nom_fichier_img = generer_image_ticket(contenu, numero)

    # 3. Construction du fichier binaire ESC/POS
    raw_payload = bytearray(b"\x1b@\x1ba\x01")  # Reset & Centrer
    
    logo_path = get_resource_path("logo_ticket.png")
    if os.path.exists(logo_path):
        try:
            img_logo = Image.open(logo_path)
            raw_payload.extend(pil_to_escpos_raster(img_logo))
            raw_payload.extend(b"\n")
        except Exception as e:
            print(f"[WARN] Impossible d'encoder le logo raster : {e}")
            
    raw_payload.extend(b"\x1ba\x00")  # Alignement gauche pour le texte
    raw_payload.extend(contenu_clean.encode('ascii', errors='replace'))
    raw_payload.extend(b"\n\x1ba\x01")  # Centrer pour l'image du bas
    
    insta_path = get_resource_path("instagram_block.png")
    if os.path.exists(insta_path):
        try:
            img_insta = Image.open(insta_path)
            raw_payload.extend(pil_to_escpos_raster(img_insta))
            raw_payload.extend(b"\n")
        except Exception as e:
            print(f"[WARN] Impossible d'encoder le bloc Instagram raster : {e}")
            
    feed_and_cut = b"\n\n\n\n\n\n\x1bi\x1dVB\x00"
    raw_payload.extend(feed_and_cut)
    
    nom_fichier_bin = data_path(f"ticket_virtuel_{numero}.bin")
    with open(nom_fichier_bin, "wb") as f:
        f.write(raw_payload)

    printed_successfully = False

    # 1. Tentative d'impression hardware via python-escpos (Production-Ready)
    try:
        from escpos.printer import Usb
        LOW_BUDGET_PRINTERS = [
            (0x04b8, 0x0202), 
            (0x0416, 0x5011),
            (0x04b8, 0x0e20),
            (0x0483, 0x5740),
            (0x1fc9, 0x2016)
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
            if os.path.exists(logo_path):
                try: p.image(logo_path, impl="bitImageColumn")
                except Exception as e: print(f"[WARN] Logo direct error: {e}")
            p.text(contenu_clean)
            if os.path.exists(insta_path):
                try: p.image(insta_path, impl="bitImageColumn")
                except Exception as e: print(f"[WARN] Insta direct error: {e}")
            p.cut()
            printed_successfully = True
            print(f"[SUCCESS] Impression via python-escpos (Imprimante native).")
    except ImportError:
        print("[INFO] python-escpos non installé, bascule sur CUPS/lp...")
    except Exception as e:
        print(f"[ERREUR ESC/POS] Impossible de joindre l'imprimante matérielle: {e}")

    # 2. Impression CUPS Image (Préférée pour rendu parfait sans caractères bruts)
    if not printed_successfully and sys.platform in ["darwin", "linux"]:
        try:
            result = subprocess.run(["lp", "-o", "fit-to-page", nom_fichier_img], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                printed_successfully = True
                print("[SUCCESS] Ticket haute définition imprimé via CUPS (Image PNG).")
        except Exception as img_err:
            print(f"[INFO] Échec lp image PNG ({img_err}), tentative en mode raw binaire...")

    # 3. Fallback via lp raw binaire
    if not printed_successfully and sys.platform in ["darwin", "linux"]:
        try:
            result = subprocess.run(["lp", "-o", "raw", nom_fichier_bin], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0:
                printed_successfully = True
                print("[SUCCESS] Ticket ESC/POS envoyé via lp -o raw.")
        except Exception as bin_err:
            print(f"[INFO] Échec lp -o raw binaire ({bin_err}), tentative en texte brut...")
            try:
                result = subprocess.run(["lp", "-o", "raw", nom_fichier_txt], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode == 0:
                    printed_successfully = True
                    print("[SUCCESS] Ticket envoyé en texte brut via lp -o raw.")
            except Exception as e:
                print(f"Échec de l'impression en texte brut via lp : {e}")

    # 4. Fallback aperçu écran
    if not printed_successfully:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", nom_fichier_txt])
            elif sys.platform == "win32":
                os.startfile(nom_fichier_txt)
            else:
                subprocess.Popen(["xdg-open", nom_fichier_txt])
            print("Fallback : Aperçu du ticket ouvert à l'écran.")
        except Exception as e:
            print(f"Erreur lors de l'ouverture de l'aperçu du ticket : {e}")

    return nom_fichier_txt

def ouvrir_tiroir_caisse():
    """
    Envoie le signal électrique RJ11 (ESC/POS) à travers l'imprimante pour ouvrir le tiroir.
    Code d'échappement standard : ESC p 0 25 250 (0x1b 0x70 0x00 0x19 0xfa)
    """
    try:
        from escpos.printer import Usb
        LOW_BUDGET_PRINTERS = [
            (0x04b8, 0x0202), 
            (0x0416, 0x5011),
            (0x04b8, 0x0e20),
            (0x0483, 0x5740),
            (0x1fc9, 0x2016)
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
            p.cashdraw(2)
            print("[SUCCESS] Tiroir ouvert via python-escpos.")
            return True
    except ImportError:
        pass

    # Fallback générique : envoyer la commande raw à l'imprimante par défaut via lp
    # (Création d'un fichier binaire contenant la commande et envoi direct sans filtre)
    try:
        drawer_cmd = b'\x1b\x70\x00\x19\xfa'
        fd, temp_path = tempfile.mkstemp(prefix="drawer_", suffix=".bin")
        with os.fdopen(fd, 'wb') as f:
            f.write(drawer_cmd)
            
        if sys.platform in ["darwin", "linux"]:
            # L'option -o raw est cruciale pour éviter que le driver modifie les bytes
            subprocess.run(["lp", "-o", "raw", temp_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print("Commande d'ouverture du tiroir envoyée (via lp raw).")
            return True
    except Exception as e:
        print(f"Erreur d'ouverture du tiroir : {e}")
        return False
