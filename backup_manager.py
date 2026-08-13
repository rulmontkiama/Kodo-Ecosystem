import os
import shutil
import datetime
import zipfile
from database_manager import DB_NAME

def get_backup_directory():
    """
    Retourne le chemin du répertoire de sauvegarde.
    On privilégie un dossier dans les Documents de l'utilisateur,
    qui est souvent synchronisé avec iCloud par défaut sur macOS.
    """
    doc_dir = os.path.expanduser("~/Documents/Kodo_Backups")
    os.makedirs(doc_dir, exist_ok=True)
    return doc_dir

def creer_backup_local():
    """
    Crée une copie compressée (ZIP) de la base de données actuelle
    dans le dossier de sauvegarde.
    """
    try:
        if not os.path.exists(DB_NAME):
            print(f"[Backup] Erreur : La base de données {DB_NAME} est introuvable.")
            return False

        backup_dir = get_backup_directory()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Copie temporaire pour éviter de locker la base en cours de lecture
        temp_copy = os.path.join(backup_dir, f"temp_{timestamp}.db")
        shutil.copy2(DB_NAME, temp_copy)
        
        # Compression en ZIP
        zip_filename = os.path.join(backup_dir, f"kodo_backup_{timestamp}.zip")
        with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(temp_copy, arcname=f"ladresse_b_{timestamp}.db")
            
        # Nettoyage fichier temporaire
        os.remove(temp_copy)
        
        print(f"[Backup] Succès : Base de données sauvegardée dans {zip_filename}")
        
        # Nettoyage des anciennes sauvegardes (garder les 30 dernières max)
        _nettoyer_anciennes_sauvegardes(backup_dir, limit=30)
        
        return zip_filename
    except Exception as e:
        print(f"[Backup] Exception lors de la sauvegarde : {e}")
        return False

def _nettoyer_anciennes_sauvegardes(backup_dir, limit=30):
    """Conserve uniquement les `limit` sauvegardes les plus récentes."""
    try:
        backups = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.startswith("kodo_backup_") and f.endswith(".zip")]
        backups.sort(key=os.path.getmtime, reverse=True) # Du plus récent au plus ancien
        
        if len(backups) > limit:
            for old_backup in backups[limit:]:
                os.remove(old_backup)
                print(f"[Backup] Ancienne sauvegarde supprimée : {old_backup}")
    except Exception as e:
        print(f"[Backup] Erreur lors du nettoyage : {e}")

if __name__ == '__main__':
    creer_backup_local()
