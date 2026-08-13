import sys
import os
import json
import time
from decimal import Decimal
import traceback
import datetime

sys.path.append(".")
import database_manager
database_manager.DB_NAME = "test_exhaustive.db"

from database_manager import (
    get_connection, initialiser_db, data_path, generer_numero_ticket,
    convert_decimal, adapt_decimal, enregistrer_vente, enregistrer_remboursement
)
import export_manager
import ticket_printer

def report_status(test_name, success, details=""):
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"[{status}] {test_name}")
    if not success and details:
        print(f"   => {details}")

def test_db_init():
    try:
        initialiser_db()
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'")
        tables = c.fetchone()[0]
        report_status("DB Initialization (Tables exist)", tables >= 6)
    except Exception as e:
        report_status("DB Initialization", False, str(e))

def test_decimal_conversion():
    try:
        val = adapt_decimal(Decimal("10.55"))
        val2 = adapt_decimal(Decimal("-0.01"))
        res1 = convert_decimal(val.encode('utf-8'))
        res2 = convert_decimal(val2.encode('utf-8'))
        report_status("Decimal Adaptation", res1 == Decimal("10.55") and res2 == Decimal("-0.01"))
    except Exception as e:
        report_status("Decimal Adaptation", False, str(e))

def test_ticket_number_generation():
    try:
        conn = get_connection(); c = conn.cursor()
        t1 = generer_numero_ticket(c)
        t2 = generer_numero_ticket(c)
        report_status("Ticket Number Unique & Formatted", t1 != t2 and t1.startswith("TCK-"))
    except Exception as e:
        report_status("Ticket Number Generation", False, str(e))

def test_transaction_and_inventory():
    try:
        conn = get_connection(); c = conn.cursor()
        c.execute("DELETE FROM Ventes_Details")
        c.execute("DELETE FROM Tickets")
        c.execute("DELETE FROM Ledger_Caisse")
        c.execute("DELETE FROM Stocks")
        c.execute("DELETE FROM Produits")
        c.execute("DELETE FROM Categories")
        conn.commit()

        c.execute("INSERT INTO Categories (nom) VALUES (?)", ("Test",))
        c.execute("INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, taux_tva) VALUES (?, ?, ?, ?, ?, ?)",
                  ("88888888", "Produit Extreme Test", "Test", 50.0, 121.0, 0.21))
        pid = c.lastrowid
        c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte) VALUES (?, ?, ?, ?)",
                  (pid, "XL", 50, 5))
        sid = c.lastrowid
        conn.commit()

        panier = [
            {"stock_id": sid, "nom": "Produit Extreme Test", "taille": "XL", "prix_vente_tvac": Decimal("121.00"), "taux_tva": Decimal("0.21")},
            {"stock_id": None, "nom": "Produit Virtuel (Presta)", "taille": "", "prix_vente_tvac": Decimal("50.00"), "taux_tva": Decimal("0.06")}
        ]
        
        tnum = generer_numero_ticket(c)
        date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        enregistrer_vente(c, tnum, Decimal("171.00"), Decimal("147.17"), Decimal("23.83"), Decimal("0.00"), "Espèces", None, Decimal("0.00"), panier, "Admin", date_heure, [("Espèces", Decimal("171.00"))])
        conn.commit()
        
        c.execute("SELECT quantite_actuelle FROM Stocks WHERE id=?", (sid,))
        qte = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM Tickets WHERE numero_ticket=?", (tnum,))
        trans_count = c.fetchone()[0]
        
        c.execute("SELECT montant FROM Ledger_Caisse WHERE type_mouvement='VENTE'")
        mv = c.fetchone()[0]
        
        report_status("Transaction Insertion & Stock Deduction & Ledger Update", qte == 49 and trans_count == 1 and float(mv) == 171.0)
        return tnum, sid, vd_id_ret(c, tnum)
    except Exception as e:
        report_status("Transaction Insertion & Stock Deduction", False, traceback.format_exc())
        return None, None, None

def vd_id_ret(c, tnum):
    c.execute("SELECT id FROM Ventes_Details WHERE id_ticket = (SELECT id FROM Tickets WHERE numero_ticket=?) LIMIT 1", (tnum,))
    row = c.fetchone()
    return row[0] if row else None

def test_return_logic(tnum, sid, vd_id):
    try:
        conn = get_connection(); c = conn.cursor()
        date_heure = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_tk = enregistrer_remboursement(c, tnum, vd_id, sid, Decimal("121.00"), "Espèces", "Admin", date_heure)
        conn.commit()
        
        c.execute("SELECT quantite_actuelle FROM Stocks WHERE id=?", (sid,))
        qte = c.fetchone()[0]
        
        c.execute("SELECT COUNT(*) FROM Tickets WHERE numero_ticket=?", (new_tk,))
        ret_count = c.fetchone()[0]
        
        c.execute("SELECT montant FROM Ledger_Caisse WHERE type_mouvement='REMBOURSEMENT'")
        mv = c.fetchone()
        
        report_status("Return Logic & Stock Replenishment & Ledger Reversal", qte == 50 and ret_count == 1 and float(mv[0]) == -121.0)
    except Exception as e:
        report_status("Return Logic", False, traceback.format_exc())

def test_virtual_z():
    try:
        conn = get_connection(); c = conn.cursor()
        today_str = datetime.date.today().isoformat()
        stats = export_manager.calculer_rapport_z_virtuel(today_str, c)
        # Vente = 171, Retour = -121 => total_ca = 50.0
        success = (abs(stats["financier"]["ca_ttc"] - 50.0) < 0.01)
        report_status("Virtual Z Calculation Accuracy", success, f"Got: {stats['financier']['ca_ttc']} instead of 50.0")
    except Exception as e:
        report_status("Virtual Z Calculation", False, traceback.format_exc())

def test_malformed_session():
    try:
        p = data_path("panier_session.json")
        if os.path.exists(p):
            backup_p = p + ".bak"
            try: os.rename(p, backup_p)
            except: pass
        else:
            backup_p = None

        with open(p, "w") as f:
            f.write("MALFORMED JSON STRING { [")
        
        # Test if app would crash trying to read it
        from main_app import MainApp
        app = MainApp()
        # the mainapp _charger_panier_session suppresses JSONDecodeError or Exception
        app._charger_panier_session()
        app.destroy()
        
        if backup_p and os.path.exists(backup_p):
            try:
                os.remove(p)
                os.rename(backup_p, p)
            except: pass
        elif os.path.exists(p):
            try: os.remove(p)
            except: pass

        report_status("Malformed panier_session.json Resilience", True)
    except Exception as e:
        report_status("Malformed panier_session.json Resilience", False, str(e))

def test_ticket_formatting_extreme():
    try:
        panier = []
        for i in range(100):
            panier.append({"nom": "A Very Long Product Name That Exceeds Normal Limits "*2, "taille": "OS", "prix_vente_tvac": Decimal("9999.99"), "taux_tva": Decimal("0.21"), "stock_id": i})
        txt = ticket_printer.generer_ticket("T-99999999999", panier, Decimal("999999.00"), Decimal("0.00"), [("Bancontact", Decimal("999999.00"))], Decimal("0.00"), vendeur_nom="Test")
        report_status("Extreme Ticket Formatting (Length/Strings)", len(txt) > 500)
    except Exception as e:
        report_status("Extreme Ticket Formatting", False, str(e))

if __name__ == "__main__":
    print("="*50)
    print(" EXHAUSTIVE BACKEND TESTS")
    print("="*50)
    test_db_init()
    test_decimal_conversion()
    tnum, sid, vd_id = test_transaction_and_inventory()
    if tnum and sid and vd_id:
        test_return_logic(tnum, sid, vd_id)
    test_virtual_z()
    test_malformed_session()
    test_ticket_formatting_extreme()
    
    # Clean up test DB
    try:
        conn = get_connection()
        conn.close()
    except:
        pass
    for ext in ["", "-shm", "-wal"]:
        f = "test_exhaustive.db" + ext
        if os.path.exists(f):
            try: os.remove(f)
            except: pass
            
    print("="*50)
