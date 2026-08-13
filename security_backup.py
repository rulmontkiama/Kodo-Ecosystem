import os
import zipfile
import datetime
import sqlite3

from database_manager import DB_NAME
BACKUP_DIR = "Backups_L_ADRESSE_B"
LOG_FILE = "logs.txt"

def log_error(message):
    """Enregistre une erreur avec horodatage dans le fichier logs.txt"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] ERREUR: {message}\n")

def verifier_integrite_db(db_path):
    """
    Vérifie si la base de données est corrompue avant la sauvegarde.
    On utilise PRAGMA integrity_check qui scanne la structure entière.
    """
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Exécution de l'intégrité (pas de paramètres utilisateurs ici, donc pas d'injection possible)
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0] == "ok":
            return True
        else:
            log_error(f"Corruption détectée par l'integrity_check: {result}")
            return False
            
    except sqlite3.DatabaseError as e:
        log_error(f"Fichier de base de données invalide ou corrompu: {e}")
        return False
    except sqlite3.OperationalError as e:
        log_error(f"Base de données verrouillée (Database is locked): {e}")
        return False
    except Exception as e:
        log_error(f"Erreur inattendue lors du check d'intégrité: {e}")
        return False

def creer_sauvegarde():
    """
    Crée une sauvegarde compressée de la base de données.
    Gère les erreurs et empêche la copie si la base est verrouillée ou corrompue.
    """
    try:
        # 1. Vérification de la présence de la BDD
        if not os.path.exists(DB_NAME):
            raise FileNotFoundError(f"La base de données {DB_NAME} est introuvable.")
            
        # 2. Création du dossier de backup s'il n'existe pas
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)
            
        # 3. Vérification de l'intégrité de la BDD pour éviter de backuper des données corrompues
        print("Vérification de l'intégrité de la base de données...")
        if not verifier_integrite_db(DB_NAME):
            raise Exception("Base de données potentiellement corrompue ou verrouillée. Sauvegarde annulée.")
            
        # 4. Utilisation de l'API de backup native de SQLite pour cloner la BDD en toute sécurité 
        # (même si elle est en cours d'utilisation)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_backup_db = os.path.join(BACKUP_DIR, f"temp_{timestamp}.db")
        
        source_conn = sqlite3.connect(DB_NAME)
        dest_conn = sqlite3.connect(temp_backup_db)
        
        with source_conn:
            # L'API native s'occupe de gérer le verrouillage (locking) intelligemment
            source_conn.backup(dest_conn)
            
        dest_conn.close()
        source_conn.close()
        
        # 5. Compression ZIP de la base de données clonée
        zip_filename = os.path.join(BACKUP_DIR, f"backup_ladresse_b_{timestamp}.zip")
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(temp_backup_db, arcname=DB_NAME)
            
        # 6. Nettoyage du fichier temporaire non-zippé
        if os.path.exists(temp_backup_db):
            os.remove(temp_backup_db)
            
        print(f"[OK] Sauvegarde réussie avec succès : {zip_filename}")
        
        # 7. Purge des anciennes sauvegardes (Rotation 30 jours)
        nettoyer_anciennes_sauvegardes(jours=30)
        
        return True
        
    except sqlite3.OperationalError as e:
        erreur_msg = f"La base de données est verrouillée ou inaccessible : {e}"
        log_error(erreur_msg)
        print(f"[ALERTE SÉCURITÉ] {erreur_msg}. Détails ajoutés dans logs.txt.")
        return False
    except Exception as e:
        erreur_msg = f"Échec de la sauvegarde : {str(e)}"
        log_error(erreur_msg)
        print(f"[ALERTE SÉCURITÉ] {erreur_msg}. Détails ajoutés dans logs.txt.")
        return False

def nettoyer_anciennes_sauvegardes(jours=30):
    """
    Supprime les fichiers ZIP de sauvegarde plus anciens que le nombre de jours spécifié.
    Permet d'éviter la saturation du disque dur (Rotation des logs).
    """
    import time
    if not os.path.exists(BACKUP_DIR): return
    
    now = time.time()
    cutoff = now - (jours * 86400)
    
    compteur = 0
    for filename in os.listdir(BACKUP_DIR):
        if filename.endswith(".zip"):
            filepath = os.path.join(BACKUP_DIR, filename)
            if os.path.getmtime(filepath) < cutoff:
                try:
                    os.remove(filepath)
                    compteur += 1
                except Exception as e:
                    log_error(f"Impossible de supprimer l'ancienne sauvegarde {filename}: {e}")
                    
    if compteur > 0:
        print(f"[INFO] Rotation : {compteur} ancienne(s) sauvegarde(s) supprimée(s).")

# --- EXEMPLE D'UTILISATION SÉCURISÉE DES PREPARED STATEMENTS ---
def executer_requete_securisee(query, parametres=()):
    """
    Exécute une requête SQL en utilisant SYSTEMATIQUEMENT des prepared statements
    (paramétrage avec '?') pour prévenir toute injection SQL.
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # L'utilisation de '?' et le passage du tuple de paramètres permet au 
        # moteur SQLite d'échapper correctement les données (Prepared Statement)
        cursor.execute(query, parametres)
        
        resultats = cursor.fetchall()
        conn.commit()
        return resultats
    except Exception as e:
        log_error(f"Erreur d'exécution de requête SQL sécurisée : {e}")
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    print("--- Démarrage du processus de sauvegarde sécurisé ---")
    creer_sauvegarde()
