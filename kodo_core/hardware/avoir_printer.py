# -*- coding: utf-8 -*-
"""
Impression du BON D'AVOIR sur imprimante thermique 80 mm.

Contrairement au ticket de caisse (texte monospace), le bon est une pièce que la cliente
GARDE et représente plus tard : il est dessiné comme une image (logo, bandeau du montant,
code-barres Code128 scannable à la douchette de la caisse) puis envoyé en raster ESC/POS.

Rien n'est inventé sur le bon : montant, code, tickets liés et dates viennent de
`Cartes_Cadeaux`. Un bon annulé ou épuisé n'est jamais imprimé (il ne vaut plus rien).
"""
import base64
import datetime
import io
import os
import tempfile
from decimal import Decimal, ROUND_HALF_UP

LARGEUR = 512          # 80 mm à 203 dpi
MARGE = 28

CONDITIONS_PAR_DEFAUT = (
    "Bon utilisable en une ou plusieurs fois dans la boutique. "
    "Non remboursable en espèces. Il est au porteur : conservez-le précieusement."
)


# --------------------------------------------------------------------------------------
# Données
# --------------------------------------------------------------------------------------

def formater_montant(valeur) -> str:
    """60.5 -> « 60,50 € » (séparateur de milliers : espace)."""
    v = Decimal(str(valeur or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    entier, _, dec = f"{v:.2f}".partition(".")
    signe = "-" if entier.startswith("-") else ""
    entier = entier.lstrip("-")
    groupes = []
    while len(entier) > 3:
        groupes.insert(0, entier[-3:])
        entier = entier[:-3]
    groupes.insert(0, entier)
    return f"{signe}{' '.join(groupes)},{dec} €"


def _date_locale(valeur) -> str:
    """`date_creation` est écrite en UTC par SQLite (CURRENT_TIMESTAMP) : on la montre en heure locale."""
    if not valeur:
        return ""
    try:
        dt = datetime.datetime.strptime(str(valeur)[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(valeur)


def charger_avoir(code, conn=None) -> dict:
    """Relit le bon en base. Lève ValueError s'il est introuvable, annulé ou épuisé."""
    import database_manager
    code_norm = str(code or "").strip().upper()
    if not code_norm:
        raise ValueError("Code de bon d'avoir manquant.")
    doit_fermer = conn is None
    if conn is None:
        conn = database_manager.get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT code, solde_initial, solde_actuel, date_creation, client_nom, emis_par, status,
                   ticket_origine, ticket_remboursement
            FROM Cartes_Cadeaux WHERE code = ?
        """, (code_norm,))
        r = c.fetchone()
    finally:
        if doit_fermer:
            conn.close()
    if not r:
        raise ValueError(f"Bon d'avoir introuvable : {code_norm}")
    avoir = {
        "code": r[0], "initial": Decimal(str(r[1])), "restant": Decimal(str(r[2])),
        "date": r[3], "client": r[4] or "", "emis_par": r[5] or "", "status": r[6] or "active",
        "ticket_origine": r[7] or "", "ticket_remboursement": r[8] or "",
    }
    if avoir["status"] != "active":
        raise ValueError(f"Le bon {code_norm} est annulé : il n'est plus imprimable.")
    if avoir["restant"] <= Decimal("0.00"):
        raise ValueError(f"Le bon {code_norm} est épuisé (solde 0,00 €) : il n'est plus imprimable.")
    return avoir


def charger_boutique(conn=None) -> dict:
    """Identité de la boutique et imprimante configurée (Paramètres)."""
    import database_manager
    doit_fermer = conn is None
    if conn is None:
        conn = database_manager.get_connection()
    params = {}
    try:
        c = conn.cursor()
        c.execute("SELECT cle, valeur FROM Parametres WHERE cle LIKE 'shop_%' "
                  "OR cle IN ('printer_ip', 'printer_name', 'avoir_conditions')")
        params = dict(c.fetchall())
    except Exception:
        pass
    finally:
        if doit_fermer:
            conn.close()
    ip = (params.get("printer_ip") or "").strip()
    return {
        "nom": params.get("shop_name") or "",
        "sous_titre": params.get("shop_subtitle") or "",
        "adresse": params.get("shop_address") or "",
        "tva": params.get("shop_vat") or "",
        "conditions": (params.get("avoir_conditions") or "").strip() or CONDITIONS_PAR_DEFAUT,
        "host": ip if ip and ip != "192.168.1.150" else None,
        "printer_name": (params.get("printer_name") or "").strip() or None,
    }


# --------------------------------------------------------------------------------------
# Dessin
# --------------------------------------------------------------------------------------

def _premiere_police(candidats):
    return next((p for p in candidats if os.path.exists(p)), None)


_POLICES = {
    "titre": [  # empattement élégant pour le titre et les signatures
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf", "C:\\Windows\\Fonts\\georgiab.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"],
    "italique": [
        "/System/Library/Fonts/Supplemental/Georgia Italic.ttf", "C:\\Windows\\Fonts\\georgiai.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"],
    "noir": [  # chiffres du montant : masse maximale, lisible même sur papier thermique pâle
        "/System/Library/Fonts/Supplemental/Arial Black.ttf", "C:\\Windows\\Fonts\\ariblk.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "gras": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:\\Windows\\Fonts\\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "normal": [
        "/System/Library/Fonts/Supplemental/Arial.ttf", "C:\\Windows\\Fonts\\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "mono": [
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf", "C:\\Windows\\Fonts\\courbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
}


def _police(role, taille):
    from PIL import ImageFont
    chemin = _premiere_police(_POLICES[role])
    if chemin:
        try:
            return ImageFont.truetype(chemin, taille)
        except Exception:
            pass
    return ImageFont.load_default()


def _largeur(draw, texte, font, suivi=0):
    if not texte:
        return 0
    if suivi == 0:
        b = draw.textbbox((0, 0), texte, font=font)
        return b[2] - b[0]
    return sum(draw.textlength(ch, font=font) + suivi for ch in texte) - suivi


def _texte(draw, x, y, texte, font, fill=0, suivi=0):
    """Texte avec espacement de lettres optionnel ; (x, y) = coin haut-gauche."""
    if suivi == 0:
        draw.text((x, y), texte, font=font, fill=fill)
        return
    for ch in texte:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + suivi


def _centre(draw, y, texte, font, fill=0, suivi=0, largeur=LARGEUR):
    w = _largeur(draw, texte, font, suivi)
    _texte(draw, (largeur - w) / 2, y, texte, font, fill, suivi)
    b = font.getbbox("Hg")
    return y + (b[3] - b[1]) + 6


def _hauteur_ligne(font):
    b = font.getbbox("Hg")
    return b[3] - b[1]


def _couper(draw, texte, font, largeur_max):
    mots, lignes, courante = texte.split(), [], ""
    for mot in mots:
        essai = f"{courante} {mot}".strip()
        if _largeur(draw, essai, font) <= largeur_max or not courante:
            courante = essai
        else:
            lignes.append(courante)
            courante = mot
    if courante:
        lignes.append(courante)
    return lignes


def _losange(draw, cx, cy, r):
    draw.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=0)


def _filet_orne(draw, y):
    """Filet fin interrompu par un losange central."""
    cx = LARGEUR // 2
    draw.line([(MARGE, y), (cx - 14, y)], fill=0, width=1)
    draw.line([(cx + 14, y), (LARGEUR - MARGE, y)], fill=0, width=1)
    _losange(draw, cx, y, 5)
    return y + 14


def _ciseaux(d, x, y):
    """Petits ciseaux dessinés (aucun glyphe de police : « ✂ » manque sur la plupart des systèmes)."""
    d.ellipse([x, y - 4, x + 10, y + 6], outline=0, width=2)
    d.ellipse([x, y + 10, x + 10, y + 20], outline=0, width=2)
    d.line([(x + 9, y + 4), (x + 32, y + 15)], fill=0, width=2)
    d.line([(x + 9, y + 12), (x + 32, y + 1)], fill=0, width=2)


def _pointilles(draw, y, x0=MARGE, x1=LARGEUR - MARGE, pas=8, trait=4, ep=2):
    x = x0
    while x < x1:
        draw.line([(x, y), (min(x + trait, x1), y)], fill=0, width=ep)
        x += pas


def _code_barres(code, largeur_max, hauteur):
    """Code128 dessiné module par module (aucun writer image requis). None si indisponible."""
    from PIL import Image
    try:
        import barcode
        modules = barcode.get("code128", code, writer=None).build()[0]
    except Exception:
        return None
    px = max(1, largeur_max // len(modules))
    img = Image.new("L", (len(modules) * px, hauteur), 255)
    pixels = img.load()
    for i, m in enumerate(modules):
        if m == "1":
            for dx in range(px):
                for y in range(hauteur):
                    pixels[i * px + dx, y] = 0
    return img


def generer_image_avoir(avoir: dict, boutique: dict, duplicata: bool = False, logo=None):
    """Dessine le bon (image PIL niveaux de gris, 512 px de large)."""
    from PIL import Image, ImageDraw

    canevas = Image.new("L", (LARGEUR, 1700), 255)
    d = ImageDraw.Draw(canevas)
    y = 18

    # --- En-tête boutique ------------------------------------------------------------
    if logo is not None:
        try:
            lg = logo.convert("RGBA")
            lg.thumbnail((320, 130))
            fond = Image.new("RGBA", lg.size, (255, 255, 255, 255))
            fond.alpha_composite(lg)
            canevas.paste(fond.convert("L"), ((LARGEUR - lg.width) // 2, y))
            y += lg.height + 12
        except Exception:
            logo = None
    nom = (boutique.get("nom") or "").strip().upper()
    if nom:
        taille = 30 if logo is None else 20
        f = _police("titre", taille)
        while _largeur(d, nom, f, 3) > LARGEUR - 2 * MARGE and taille > 14:
            taille -= 1
            f = _police("titre", taille)
        y = _centre(d, y, nom, f, suivi=3)
    for ligne in (boutique.get("sous_titre"), boutique.get("adresse")):
        if ligne and str(ligne).strip():
            y = _centre(d, y, str(ligne).strip(), _police("normal", 16))
    tva = (boutique.get("tva") or "").strip()
    if tva:
        y = _centre(d, y, tva if tva.upper().startswith("TVA") else f"TVA : {tva}", _police("normal", 16))
    y += 8

    # --- Titre encadré de doubles filets ----------------------------------------------
    d.rectangle([MARGE, y, LARGEUR - MARGE, y + 4], fill=0)
    d.line([(MARGE, y + 9), (LARGEUR - MARGE, y + 9)], fill=0, width=1)
    y += 24
    f_titre = _police("titre", 44)
    titre = "BON D'AVOIR" if str(avoir["code"]).upper().startswith("AVOIR") else "CARTE CADEAU"
    y = _centre(d, y, titre, f_titre, suivi=5)
    y += 8
    d.line([(MARGE, y), (LARGEUR - MARGE, y)], fill=0, width=1)
    d.rectangle([MARGE, y + 5, LARGEUR - MARGE, y + 9], fill=0)
    y += 28

    # --- Bandeau du montant (blanc sur noir) -------------------------------------------
    haut = 154
    d.rounded_rectangle([MARGE, y, LARGEUR - MARGE, y + haut], radius=18, fill=0)
    entame = avoir["restant"] < avoir["initial"]
    f_lab = _police("gras", 16)
    _centre(d, y + 16, "SOLDE DISPONIBLE" if entame else "VALEUR DU BON", f_lab, fill=255, suivi=5)
    montant = formater_montant(avoir["restant"])
    taille = 84
    f_mt = _police("noir", taille)
    while _largeur(d, montant, f_mt) > LARGEUR - 2 * MARGE - 40 and taille > 30:
        taille -= 2
        f_mt = _police("noir", taille)
    # Centrage sur l'encre réelle des chiffres (et non sur la boîte typographique de la police).
    bb = d.textbbox((0, 0), montant, font=f_mt)
    haut_zone = y + 52
    bas_zone = y + haut - (30 if entame else 16)
    y_encre = haut_zone + (bas_zone - haut_zone - (bb[3] - bb[1])) // 2
    d.text(((LARGEUR - (bb[2] - bb[0])) / 2 - bb[0], y_encre - bb[1]), montant, font=f_mt, fill=255)
    if entame:
        _centre(d, y + haut - 28, f"montant initial : {formater_montant(avoir['initial'])}",
                _police("normal", 14), fill=255)
    y += haut + 14

    if duplicata:
        f_dup = _police("gras", 18)
        w = _largeur(d, "DUPLICATA", f_dup, 6)
        x0 = (LARGEUR - w) / 2 - 16
        d.rounded_rectangle([x0, y + 4, x0 + w + 32, y + 4 + _hauteur_ligne(f_dup) + 16], radius=8, outline=0, width=2)
        _texte(d, x0 + 16, y + 12, "DUPLICATA", f_dup, suivi=6)
        y += _hauteur_ligne(f_dup) + 28
    y += 14

    # --- Code-barres + code en clair ---------------------------------------------------
    y = _centre(d, y, "PRÉSENTEZ CE CODE EN CAISSE", _police("gras", 15), suivi=2)
    y += 4
    barres = _code_barres(avoir["code"], LARGEUR - 2 * MARGE - 12, 92)
    if barres is not None:
        canevas.paste(barres, ((LARGEUR - barres.width) // 2, y))
        y += barres.height + 10
    code = avoir["code"]
    taille = 28
    f_code = _police("mono", taille)
    while _largeur(d, code, f_code, 2) > LARGEUR - 2 * MARGE and taille > 14:
        taille -= 1
        f_code = _police("mono", taille)
    y = _centre(d, y, code, f_code, suivi=2)
    y += 12
    y = _filet_orne(d, y) + 6

    # --- Détails -----------------------------------------------------------------------
    lignes = [("Émis le", _date_locale(avoir["date"])),
              ("Ticket d'origine", avoir["ticket_origine"]),
              ("Remboursement", avoir["ticket_remboursement"]),
              ("Client", avoir["client"]),
              ("Émis par", avoir["emis_par"])]
    f_l, f_v = _police("normal", 17), _police("gras", 17)
    for etiquette, valeur in lignes:
        if not valeur:
            continue
        valeur = str(valeur)
        while _largeur(d, valeur, f_v) > 270 and len(valeur) > 4:
            valeur = valeur[:-2]
        wl, wv = _largeur(d, etiquette, f_l), _largeur(d, valeur, f_v)
        _texte(d, MARGE, y, etiquette, f_l)
        _texte(d, LARGEUR - MARGE - wv, y, valeur, f_v)
        yl = y + _hauteur_ligne(f_l) - 2
        for x in range(int(MARGE + wl + 8), int(LARGEUR - MARGE - wv - 8), 6):
            d.point((x, yl), fill=0)
        y += _hauteur_ligne(f_l) + 12
    y += 4

    # --- Conditions + remerciement -----------------------------------------------------
    f_cond = _police("italique", 15)
    for ligne in _couper(d, boutique.get("conditions") or CONDITIONS_PAR_DEFAUT, f_cond, LARGEUR - 2 * MARGE - 20):
        y = _centre(d, y, ligne, f_cond)
    y += 10
    y = _centre(d, y, "Merci de votre confiance", _police("titre", 21))
    y += 12

    # --- Ligne de découpe --------------------------------------------------------------
    _pointilles(d, y + 12, x0=MARGE + 40)
    _ciseaux(d, MARGE, y + 2)
    y += 40

    return canevas.crop((0, 0, LARGEUR, y))


def apercu_base64(code, duplicata=False, conn=None) -> str:
    """Aperçu PNG (data URL) pour l'écran : exactement ce qui sortira du papier."""
    image = generer_image_avoir(charger_avoir(code, conn=conn), charger_boutique(conn=conn),
                                duplicata=duplicata, logo=_logo_boutique())
    tampon = io.BytesIO()
    image.convert("1").save(tampon, format="PNG")
    return "data:image/png;base64," + base64.b64encode(tampon.getvalue()).decode("ascii")


def _logo_boutique():
    try:
        from PIL import Image
        from kodo_core.hardware.printer import get_ticket_logo_path
        chemin = get_ticket_logo_path()
        if chemin and os.path.exists(chemin):
            return Image.open(chemin)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------------------
# Impression
# --------------------------------------------------------------------------------------

def construire_payload(image) -> bytes:
    """Image -> octets ESC/POS (raster GS v 0 par tranches, 6 sauts de ligne, découpe)."""
    from kodo_core.hardware.printer import (ESC_INIT, ESC_ALIGN_LEFT, GS_CUT_FUNCTION, pil_to_escpos_raster)
    raster = pil_to_escpos_raster(image.convert("RGB"), max_width=LARGEUR, target_width=LARGEUR, center=True)
    return bytes(ESC_INIT + b"\x1b2" + ESC_ALIGN_LEFT) + raster + b"\n\n\n\n\n\n" + GS_CUT_FUNCTION


def imprimer_avoir(code, duplicata=False, printer_name=None, host=None, port=9100) -> bool:
    """Imprime le bon. True si l'imprimante a accepté les données ; ValueError si le bon est inimprimable."""
    from kodo_core.hardware.printer import ESCPOSThermalPrinter
    avoir = charger_avoir(code)
    boutique = charger_boutique()
    image = generer_image_avoir(avoir, boutique, duplicata=duplicata, logo=_logo_boutique())

    # Copie de contrôle (même principe que ticket_virtuel_*.png) : permet de relire ce qui est parti.
    try:
        from database_manager import data_path
        chemin = data_path(f"avoir_{avoir['code']}.png")
    except Exception:
        chemin = os.path.join(tempfile.gettempdir(), f"avoir_{avoir['code']}.png")
    try:
        image.convert("1").save(chemin)
    except Exception:
        pass

    pilote = ESCPOSThermalPrinter(printer_name=printer_name or boutique["printer_name"],
                                  host=host or boutique["host"], port=port)
    return bool(pilote.send_raw(construire_payload(image)))
