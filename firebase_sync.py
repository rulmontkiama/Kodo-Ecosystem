import threading
import time
import json
import os
from database_manager import get_connection, data_path

# Firebase Admin SDK
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    firestore = None

class FirebaseSyncThread(threading.Thread):
    def __init__(self, key_path=None):
        super().__init__()
        self.daemon = True
        self.key_path = key_path or data_path("firebase-adminsdk.json")
        self.db = None
        self.running = True
        self._init_firebase()
        
    def _init_firebase(self):
        if not firebase_admin:
            print("[SYNC] firebase-admin non installé. Pip install firebase-admin requis.")
            return
            
        if not os.path.exists(self.key_path):
            print(f"[SYNC] Fichier de clé Firebase introuvable : {self.key_path}")
            return
            
        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(self.key_path)
                firebase_admin.initialize_app(cred)
            self.db = firestore.client()
            print("[SYNC] Firebase initialisé avec succès.")
        except Exception as e:
            print(f"[SYNC] Erreur initialisation Firebase : {e}")

    def run(self):
        print("[SYNC] Démarrage du thread de synchronisation en arrière-plan...")
        self._migrate_sync_columns()
        
        while self.running:
            if self.db:
                self._sync_tickets()
                self._sync_rapports_z()
            time.sleep(30) # Vérifie toutes les 30 secondes
            
    def _migrate_sync_columns(self):
        """Ajoute la colonne 'synced' si elle n'existe pas"""
        try:
            conn = get_connection()
            c = conn.cursor()
            
            c.execute("PRAGMA table_info(Tickets)")
            if 'synced' not in [r[1] for r in c.fetchall()]:
                c.execute("ALTER TABLE Tickets ADD COLUMN synced INTEGER DEFAULT 0")
                
            c.execute("PRAGMA table_info(Rapports_Z)")
            if 'synced' not in [r[1] for r in c.fetchall()]:
                c.execute("ALTER TABLE Rapports_Z ADD COLUMN synced INTEGER DEFAULT 0")
                
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[SYNC] Erreur migration DB pour sync : {e}")

    def _sync_tickets(self):
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id, numero_ticket, total_tvac, methode_paiement, date_heure, signature FROM Tickets WHERE synced = 0 OR synced IS NULL")
            tickets = c.fetchall()
            
            for t_id, num, total, methode, dt, sig in tickets:
                # Récupérer les détails du ticket
                c.execute("""
                    SELECT p.nom, vd.prix_unitaire_tvac 
                    FROM Ventes_Details vd 
                    LEFT JOIN Stocks s ON vd.id_stock = s.id 
                    LEFT JOIN Produits p ON s.id_produit = p.id 
                    WHERE vd.id_ticket = ?
                """, (t_id,))
                items = [{"nom": row[0] or "Prestation", "prix": float(row[1])} for row in c.fetchall()]
                
                doc_ref = self.db.collection('pos_tickets').document(num)
                doc_ref.set({
                    'numero': num,
                    'total': float(total),
                    'methode': methode,
                    'date_heure': dt,
                    'signature': sig,
                    'items': items,
                    'timestamp': firestore.SERVER_TIMESTAMP
                })
                
                c.execute("UPDATE Tickets SET synced = 1 WHERE id = ?", (t_id,))
                print(f"[SYNC] Ticket {num} synchronisé sur le Cloud.")
                
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"[SYNC] Erreur sync tickets : {e}")
        finally:
            if conn:
                conn.close()

    def _sync_rapports_z(self):
        conn = None
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id, date, donnees_json, signature FROM Rapports_Z WHERE synced = 0 OR synced IS NULL")
            rapports = c.fetchall()
            
            for r_id, date, json_data, sig in rapports:
                data = json.loads(json_data)
                data['signature_nf525'] = sig
                data['timestamp'] = firestore.SERVER_TIMESTAMP
                
                doc_ref = self.db.collection('pos_rapports_z').document(date)
                doc_ref.set(data)
                
                c.execute("UPDATE Rapports_Z SET synced = 1 WHERE id = ?", (r_id,))
                print(f"[SYNC] Rapport Z du {date} synchronisé sur le Cloud.")
                
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"[SYNC] Erreur sync rapports Z : {e}")
        finally:
            if conn:
                conn.close()

def start_sync_thread():
    thread = FirebaseSyncThread()
    thread.start()
    return thread
