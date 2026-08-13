"""
Générateur d'aperçu visuel de ticket optimisé Retina / HiDPI (Async Worker Thread).
"""
import os
import sys
import threading
from PIL import Image, ImageDraw, ImageFont

CONTENU_TICKET = """
         Une nouvelle saison...
         De nouvelles envies...
         
         Le moment est venu de
         renouveler votre garde-robe.
         
          C'EST LE DÉBUT DES
               S O L D E S
         
       Profitez de remises allant
        jusqu'à -30% durant tout
          le mois de juillet.
          
        L'Adresse B n'attend plus
                 que vous.
"""

def generer_apercu_image_sync(output_filename="ticket_promo_preview.png", scale=2) -> str:
    """
    Génère l'aperçu visuel du ticket en haute résolution Retina HiDPI (scale=2, 1024px).
    """
    logo_path = "logo_ticket.png"
    insta_path = "instagram_block.png"
    
    if not os.path.exists(logo_path) or not os.path.exists(insta_path):
        return None

    img_logo = Image.open(logo_path).convert("RGBA")
    img_insta = Image.open(insta_path).convert("RGBA")
    
    # Largeur de base 512px * scale = 1024px (Retina Crisp Output)
    base_width = 512
    width = base_width * scale
    
    # Redimensionner les logos avec LANCZOS pour un rendu Retina ultra-net
    w_logo = width
    h_logo = int(img_logo.height * (width / float(img_logo.width)))
    img_logo_scaled = img_logo.resize((w_logo, h_logo), Image.Resampling.LANCZOS)
    
    w_insta = width
    h_insta = int(img_insta.height * (width / float(img_insta.width)))
    img_insta_scaled = img_insta.resize((w_insta, h_insta), Image.Resampling.LANCZOS)

    font_size = 18 * scale
    font = None
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

    lines = CONTENU_TICKET.strip("\n").split("\n")
    line_height = 28 * scale
    text_section_height = len(lines) * line_height + (40 * scale)
    total_height = img_logo_scaled.height + text_section_height + img_insta_scaled.height + (60 * scale)

    ticket_img = Image.new("RGB", (width, total_height), "white")
    draw = ImageDraw.Draw(ticket_img)

    current_y = 20 * scale
    ticket_img.paste(img_logo_scaled, (0, current_y), img_logo_scaled)
    current_y += img_logo_scaled.height + (20 * scale)

    draw.text((10 * scale, current_y), "=" * 42, fill="black", font=font)
    current_y += line_height

    for line in lines:
        draw.text((10 * scale, current_y), line, fill="black", font=font)
        current_y += line_height

    draw.text((10 * scale, current_y), "=" * 42, fill="black", font=font)
    current_y += line_height + (20 * scale)

    ticket_img.paste(img_insta_scaled, (0, current_y), img_insta_scaled)

    ticket_img.save(output_filename, quality=95)
    return output_filename

def generer_apercu_image_async(callback=None):
    """
    Exécute la génération de l'image de ticket dans un thread d'arrière-plan non bloquant.
    """
    def _worker():
        res = generer_apercu_image_sync()
        if callback:
            callback(res)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    print("Génération asynchrone d'aperçu Retina...")
    t = generer_apercu_image_async(lambda p: print(f"Terminé : {p}"))
    t.join()
