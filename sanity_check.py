import os
import sys
import sqlite3
import subprocess

def verifier_environnement():
    print("==================================================")
    print("  Kōdo POS - Diagnostic de l'Environnement (Sanity Check)")
    print("==================================================\n")
    
    erreurs = 0
    avertissements = 0
    
    # 1. Vérification de l'OS
    print(f"[OS] Plateforme détectée : {sys.platform}")
    if sys.platform != "darwin":
        print("  -> [AVERTISSEMENT] Le système n'est pas macOS. Le comportement de l'imprimante (lp) peut différer.")
        avertissements += 1
    else:
        print("  -> [OK] macOS détecté.")

    # 2. Vérification des chemins (Isolation)
    dossier_app = os.path.abspath(os.path.dirname(__file__))
    print(f"\n[CHEMIN] Exécution depuis : {dossier_app}")
    if "/Users/kiamarulmont" in dossier_app and not getattr(sys, 'frozen', False):
        print("  -> [AVERTISSEMENT] Exécution depuis un dossier de développement. En production, utilisez l'exécutable compilé.")
        avertissements += 1
    else:
        print("  -> [OK] L'application semble isolée (ou compilée).")
        
    # 3. Vérification des droits d'écriture
    print("\n[PERMISSIONS] Vérification de l'accès disque...")
    try:
        test_file = os.path.join(dossier_app, ".test_write")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        print("  -> [OK] Droits d'écriture confirmés dans le dossier courant.")
    except Exception as e:
        print(f"  -> [ERREUR CRITIQUE] Impossible d'écrire dans le dossier. La base de données et les logs vont échouer. Détails : {e}")
        erreurs += 1

    # 4. Vérification de la base de données
    db_path = os.path.join(dossier_app, "ladresse_b.db")
    print(f"\n[BASE DE DONNÉES] Vérification de SQLite...")
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("PRAGMA integrity_check;")
        res = c.fetchone()
        conn.close()
        if res and res[0] == "ok":
            print("  -> [OK] Base de données saine et accessible.")
        else:
            print(f"  -> [ERREUR CRITIQUE] La base de données renvoie une erreur d'intégrité : {res}")
            erreurs += 1
    except Exception as e:
        print(f"  -> [AVERTISSEMENT] Impossible de se connecter à la base de données (normale si première exécution). Détails : {e}")
        avertissements += 1

    # 5. Vérification Pilotes Imprimante (ESC/POS & lp)
    print("\n[MATÉRIEL] Vérification des pilotes d'impression...")
    try:
        import escpos
        print("  -> [OK] Pilote natif 'python-escpos' disponible pour impression USB/Réseau rapide.")
    except ImportError:
        print("  -> [AVERTISSEMENT] Pilote natif 'python-escpos' absent. L'application utilisera le fallback système.")
        avertissements += 1
        
    # Vérification fallback système 'lp' (macOS/Linux)
    if sys.platform in ["darwin", "linux"]:
        try:
            subprocess.run(["lpstat", "-d"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            print("  -> [OK] Fallback système d'impression (lp) détecté et fonctionnel.")
        except Exception:
            print("  -> [AVERTISSEMENT] Impossible de vérifier l'état du spooler d'impression système.")
            avertissements += 1
            
    print("\n==================================================")
    if erreurs > 0:
        print(f"DIAGNOSTIC ÉCHOUÉ : {erreurs} Erreurs critiques détectées. Le système POS risque de planter.")
    else:
        print(f"DIAGNOSTIC RÉUSSI : Prêt pour la production ({avertissements} avertissements mineurs).")
    print("==================================================")

if __name__ == "__main__":
    verifier_environnement()
