import sys
import os
sys.path.append(os.path.abspath("."))
from decimal import Decimal
import export_manager
from database_manager import get_connection

# Fake data
comptage = {
    "500": 0, "200": 0, "100": 1, "50": 3, "20": 12, "10": 8, "5": 14,
    "2": 25, "1": 18, "0.5": 12, "0.2": 20, "0.1": 15, "0.05": 10, "0.02": 5, "0.01": 8
}

total_compte = Decimal("0.00")
for k, v in comptage.items():
    total_compte += Decimal(k) * Decimal(v)

conn = get_connection(); c = conn.cursor()
c.execute("INSERT INTO Sessions_Caisse (fond_caisse_matin, montant_theorique_soir, montant_compté_soir, ecart_caisse, date_cloture) VALUES (200.00, 750.00, ?, ?, CURRENT_TIMESTAMP)", (float(total_compte), float(total_compte) - 750.00))
conn.commit()

export_manager.sauvegarder_rapport_z_journalier(comptage)
path = export_manager.export_synthese_gerant(comptage)
print(path)
os.system(f'open "{path}"')
