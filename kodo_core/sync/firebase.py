"""
Kōdo POS - Synchronisation & Sauvegarde Miroir Cloud Firebase (Firestore & Realtime DB)
Permet d'assurer une sauvegarde sécurisée des tickets et des rapports Z.
"""

import threading
import time
import json
import os
import logging
from database_manager import get_connection, data_path

# Import conditionnel de Firebase Admin SDK
try:
    import firebase_admin
    from firebase_admin import credentials, firestore, db as realtime_db
except ImportError:
    firebase_admin = None
    firestore = None
    realtime_db = None

logger = logging.getLogger("kodo_core.sync.firebase")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[FIREBASE SYNC] %(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class FirebaseSync:
    """Gestionnaire de connexion et synchronisation miroir Firestore & Realtime DB."""

    def __init__(self, key_path: str = None, database_url: str = None):
        self.key_path = key_path or data_path("firebase-adminsdk.json")
        if not os.path.exists(self.key_path):
            alt_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "kodo-pos-firebase-adminsdk-fbsvc-c56ff45f8c.json")
            if os.path.exists(alt_path):
                self.key_path = alt_path

        self.database_url = database_url
        self.firestore_db = None
        self.realtime_ref = None
        self.init_firebase()

    def init_firebase(self):
        """Initialise les SDK Cloud Firestore et Realtime Database avec les identifiants SA."""
        if not firebase_admin:
            logger.warning("firebase-admin n'est pas installé. Installez-le via `pip install firebase-admin`.")
            return

        if not os.path.exists(self.key_path):
            logger.warning(f"Fichier de clé Firebase introuvable : {self.key_path}")
            return

        try:
            if not firebase_admin._apps:
                cred = credentials.Certificate(self.key_path)
                options = {}
                if self.database_url:
                    options["databaseURL"] = self.database_url
                else:
                    # Essayer de deviner le databaseURL depuis le service account JSON
                    try:
                        with open(self.key_path, "r") as f:
                            data = json.load(f)
                            project_id = data.get("project_id")
                            if project_id:
                                options["databaseURL"] = f"https://{project_id}-default-rtdb.firebaseio.com"
                    except Exception:
                        pass
                firebase_admin.initialize_app(cred, options if options else None)

            self.firestore_db = firestore.client()
            if realtime_db:
                try:
                    self.realtime_ref = realtime_db.reference("/")
                except Exception as ex_rtdb:
                    logger.debug(f"Realtime Database non configurée ou indisponible: {ex_rtdb}")

            logger.info("Firebase Firestore & Realtime Database initialisés avec succès.")
        except Exception as e:
            logger.error(f"Erreur initialisation Firebase : {e}")

    def migrate_sync_columns(self):
        """Ajoute les colonnes 'synced' si elles n'existent pas déjà."""
        try:
            conn = get_connection()
            c = conn.cursor()

            c.execute("PRAGMA table_info(Tickets)")
            cols_tickets = [r[1] for r in c.fetchall()]
            if "synced" not in cols_tickets:
                c.execute("ALTER TABLE Tickets ADD COLUMN synced INTEGER DEFAULT 0")

            c.execute("PRAGMA table_info(Rapports_Z)")
            cols_z = [r[1] for r in c.fetchall()]
            if "synced" not in cols_z:
                c.execute("ALTER TABLE Rapports_Z ADD COLUMN synced INTEGER DEFAULT 0")

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Erreur migration DB pour sync : {e}")

    def sync_tickets(self) -> int:
        """Pousse les tickets non synchronisés vers Firestore et Realtime DB."""
        if not self.firestore_db and not self.realtime_ref:
            return 0

        conn = None
        synced_count = 0
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id, numero_ticket, total_tvac, methode_paiement, date_heure, signature FROM Tickets WHERE synced = 0 OR synced IS NULL")
            tickets = c.fetchall()

            for t_id, num, total, methode, dt, sig in tickets:
                c.execute("""
                    SELECT p.nom, vd.prix_unitaire_tvac 
                    FROM Ventes_Details vd 
                    LEFT JOIN Stocks s ON vd.id_stock = s.id 
                    LEFT JOIN Produits p ON s.id_produit = p.id 
                    WHERE vd.id_ticket = ?
                """, (t_id,))
                items = [{"nom": row[0] or "Prestation", "prix": float(row[1])} for row in c.fetchall()]

                ticket_payload = {
                    "numero": num,
                    "total": float(total),
                    "methode": methode,
                    "date_heure": dt,
                    "signature": sig,
                    "items": items,
                    "timestamp": firestore.SERVER_TIMESTAMP if firestore else time.time()
                }

                # 1. Cloud Firestore (Collection principale)
                if self.firestore_db:
                    doc_ref = self.firestore_db.collection("pos_tickets").document(num)
                    doc_ref.set(ticket_payload)

                # 2. Realtime Database (Sauvegarde miroir)
                if self.realtime_ref:
                    try:
                        self.realtime_ref.child("backups").child("tickets").child(num.replace("/", "_")).set({
                            "numero": num,
                            "total": float(total),
                            "methode": methode,
                            "date_heure": dt,
                            "items_count": len(items)
                        })
                    except Exception as e_rt:
                        logger.debug(f"Miroir Realtime DB ticket {num} ignoré: {e_rt}")

                c.execute("UPDATE Tickets SET synced = 1 WHERE id = ?", (t_id,))
                synced_count += 1
                logger.info(f"Ticket {num} synchronisé sur le Cloud (Firestore & Realtime).")

            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Erreur sync tickets Cloud: {e}")
        finally:
            if conn:
                conn.close()
        return synced_count

    def sync_rapports_z(self) -> int:
        """Pousse les rapports Z vers Firestore et Realtime DB."""
        if not self.firestore_db and not self.realtime_ref:
            return 0

        conn = None
        synced_count = 0
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("SELECT id, date, donnees_json, signature FROM Rapports_Z WHERE synced = 0 OR synced IS NULL")
            rapports = c.fetchall()

            for r_id, date, json_data, sig in rapports:
                data = json.loads(json_data)
                data["signature_nf525"] = sig
                data["timestamp"] = firestore.SERVER_TIMESTAMP if firestore else time.time()

                # 1. Cloud Firestore
                if self.firestore_db:
                    doc_ref = self.firestore_db.collection("pos_rapports_z").document(date)
                    doc_ref.set(data)

                # 2. Realtime Database
                if self.realtime_ref:
                    try:
                        self.realtime_ref.child("backups").child("rapports_z").child(date).set({
                            "date": date,
                            "total_chiffre_affaires": data.get("total_ca", 0.0),
                            "signature_nf525": sig
                        })
                    except Exception as e_rt:
                        logger.debug(f"Miroir Realtime DB rapport Z {date} ignoré: {e_rt}")

                c.execute("UPDATE Rapports_Z SET synced = 1 WHERE id = ?", (r_id,))
                synced_count += 1
                logger.info(f"Rapport Z du {date} synchronisé sur le Cloud.")

            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Erreur sync rapports Z Cloud: {e}")
        finally:
            if conn:
                conn.close()
        return synced_count


class FirebaseSyncThread(threading.Thread):
    """Thread d'arrière-plan gérant la synchronisation Cloud continue."""

    def __init__(self, key_path: str = None, database_url: str = None):
        super().__init__()
        self.daemon = True
        self.running = True
        self.engine = FirebaseSync(key_path=key_path, database_url=database_url)

    @property
    def db(self):
        return self.engine.firestore_db

    def _migrate_sync_columns(self):
        return self.engine.migrate_sync_columns()

    def _sync_tickets(self):
        return self.engine.sync_tickets()

    def _sync_rapports_z(self):
        return self.engine.sync_rapports_z()

    def run(self):
        logger.info("Démarrage du thread Firebase en arrière-plan...")
        self._migrate_sync_columns()

        while self.running:
            if self.engine.firestore_db or self.engine.realtime_ref:
                self._sync_tickets()
                self._sync_rapports_z()
            time.sleep(30)


def start_sync_thread(key_path=None):
    thread = FirebaseSyncThread(key_path=key_path)
    thread.start()
    return thread
