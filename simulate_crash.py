import sys
sys.path.append(".")
from main_app import MainApp

app = MainApp()
app.update()

# simulate login
app._on_login_success({"nom": "admin", "admin": True})

# simulate add item
app.panier.append({"nom": "Test", "taille": "M", "prix_vente_tvac": 10, "taux_tva": 21, "stock_id": 1})
app._refresh_panier()

# simulate click 'Annuler le ticket'
app.vider_panier()

print("Simulated successfully")
