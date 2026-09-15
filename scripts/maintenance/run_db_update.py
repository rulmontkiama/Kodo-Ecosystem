import sqlite3
import os

db_path = os.path.expanduser('~/Documents/Kodo_POS/ladresse_b.db')
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_subtitle', 'Boutique de Mode')")
c.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_address', 'Chemin Rue 53, 4960 Malmedy')")
c.execute("INSERT OR IGNORE INTO Parametres (cle, valeur) VALUES ('shop_vat', 'BE 1035.331.577')")
c.execute("UPDATE Parametres SET valeur = 'Chemin Rue 53, 4960 Malmedy' WHERE cle = 'shop_address'")
c.execute("UPDATE Parametres SET valeur = 'BE 1035.331.577' WHERE cle = 'shop_vat'")
conn.commit()
conn.close()
print("DB updated.")
