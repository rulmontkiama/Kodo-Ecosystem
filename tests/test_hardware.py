"""
Tests unitaires pour les modules kodo_core/hardware (printer.py & pdf.py)
"""
import os
import sys
import unittest
import tempfile
import datetime
from decimal import Decimal

# Inclure le dossier racine au path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from kodo_core.hardware.printer import (
    COL,
    ESCPOSThermalPrinter,
    strip_accents,
    generer_ticket,
    generer_ticket_takeaway,
    generer_ticket_promo,
    pil_to_escpos_raster,
    GS_CUT_FUNCTION,
    ESC_DRAWER_PIN2,
)
from kodo_core.hardware.pdf import (
    generer_rapport_pdf,
    generer_etiquettes_pdf,
    generer_facture_pdf,
    generer_recu_pdf,
    generate_barcode_drawing,
)
import ticket_printer
import pdf_generator
class TestHardwareModules(unittest.TestCase):

    def setUp(self):
        import database_manager
        self.database_manager = database_manager
        self.temp_dir = tempfile.mkdtemp()
        self.temp_db_path = os.path.join(self.temp_dir, "test_hardware.db")
        self.database_manager.DB_NAME = self.temp_db_path
        self.database_manager.initialiser_db()

    def tearDown(self):
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def test_strip_accents(self):
        """Vérifie le nettoyage des caractères spéciaux pour imprimantes thermiques."""
        raw_txt = "Café & Thélée — Mode & Beauté €100"
        clean_txt = strip_accents(raw_txt)
        self.assertNotIn("é", clean_txt)
        self.assertNotIn("€", clean_txt)
        self.assertIn("Cafe", clean_txt)
        self.assertIn("EUR100", clean_txt)

    def test_printer_formatting(self):
        """Vérifie la génération des tickets de caisse, à emporter et promo."""
        panier = [{
            "nom": "Robe d'été",
            "taille": "M",
            "quantite": 2,
            "prix_vente_tvac": Decimal("49.99"),
            "taux_tva": Decimal("0.21")
        }]
        paiements = [("Bancontact", Decimal("99.98"))]

        # Ticket standard
        tck_text = generer_ticket(
            numero="TCK-1001",
            panier=panier,
            total_tvac=Decimal("99.98"),
            remise=Decimal("0.00"),
            paiements=paiements,
            rendu_monnaie=Decimal("0.00"),
            nom_client="Jean Dupont"
        )
        self.assertIn("TCK-1001", tck_text)
        self.assertIn("Robe d'ete", strip_accents(tck_text))
        self.assertIn("CARTE", tck_text)

        # Ticket Takeaway
        items_food = [{"nom": "Burger Artisan", "quantite": 1, "options": ["Sans oignon"], "note": "Bien cuit"}]
        tak_text = generer_ticket_takeaway(
            numero_commande="42",
            items=items_food,
            nom_client="Alice",
            heure_retrait="12:30"
        )
        self.assertIn("VENTE A EMPORTER", strip_accents(tak_text))
        self.assertIn("TAK-42", tak_text)
        self.assertIn("Burger Artisan", tak_text)

        # Ticket Promo
        promo_text = generer_ticket_promo(
            code_promo="SUMMER20",
            description="Remise estivale exclusive",
            pourcentage=20,
            date_expiration="31/08/2026"
        )
        self.assertIn("CODE PROMO : SUMMER20", promo_text)
        self.assertIn("-20%", promo_text)

    def test_escpos_commands_and_driver(self):
        """Vérifie l'instanciation du driver et la génération de raw bytes."""
        printer = ESCPOSThermalPrinter()
        self.assertTrue(printer.connect())

        # Test raster converter
        from PIL import Image
        img = Image.new("RGB", (100, 100), "white")
        raster_bytes = pil_to_escpos_raster(img)
        self.assertTrue(len(raster_bytes) > 0)
        self.assertEqual(raster_bytes[:3], b'\x1dv0')

    def test_pdf_generators(self):
        """Vérifie la génération des PDF vectoriels (Bilan Z, Factures, Reçus, Étiquettes)."""
        # Code-barres
        d_ean = generate_barcode_drawing("EAN13", "5412345678901")
        self.assertIsNotNone(d_ean)
        d_128 = generate_barcode_drawing("Code128", "TCK-2026-99")
        self.assertIsNotNone(d_128)
        d_qr = generate_barcode_drawing("QR", "https://kodo.pos")
        self.assertIsNotNone(d_qr)

        # Rapport Bilan Z
        z_pdf = os.path.join(self.temp_dir, "bilan_z.pdf")
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        res_z = generer_rapport_pdf("jour", today_str, z_pdf)
        self.assertTrue(os.path.exists(res_z))
        self.assertTrue(os.path.getsize(res_z) > 1000)

        # Étiquettes
        lbl_pdf = os.path.join(self.temp_dir, "etiquettes.pdf")
        res_lbl = generer_etiquettes_pdf("T-Shirt Cotton", "1234567890123", "L", 29.99, 19.99, 2, lbl_pdf)
        self.assertTrue(os.path.exists(res_lbl))

        # Facture Vectorielle A4
        inv_pdf = os.path.join(self.temp_dir, "facture.pdf")
        items = [{"code_barre": "12345678", "nom": "Veste Cuir", "quantite": 1, "prix_vente_tvac": 150.0, "taux_tva": 0.21}]
        res_inv = generer_facture_pdf("FAC-2026-001", "14/08/2026", {"nom": "Client Test"}, items, {}, save_path=inv_pdf)
        self.assertTrue(os.path.exists(res_inv))

        # Reçu Vectoriel Ticket
        rec_pdf = os.path.join(self.temp_dir, "recu.pdf")
        res_rec = generer_recu_pdf("TCK-001", "14/08/2026 12:00", items, {"total_tvac": 150.0}, [], save_path=rec_pdf)
        self.assertTrue(os.path.exists(res_rec))

    def test_facade_integrity(self):
        """Vérifie l'exportation transparente des façades root ticket_printer et pdf_generator."""
        self.assertEqual(ticket_printer.COL, COL)
        self.assertEqual(ticket_printer.strip_accents("Testé"), "Teste")
        self.assertIsNotNone(pdf_generator.generer_rapport_pdf)
        self.assertIsNotNone(pdf_generator.generer_facture_pdf)


if __name__ == "__main__":
    unittest.main()
