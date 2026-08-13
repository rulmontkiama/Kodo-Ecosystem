import hashlib
import sqlite3
from database_manager import get_connection

def verifier_chainage(table, id_col="id", sig_col="signature", hash_prec_col="hash_precedent", compute_hash_func=None):
    print(f"=== Vérification de l'intégrité : {table} ===")
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(f"SELECT * FROM {table} ORDER BY {id_col} ASC")
        rows = c.fetchall()
        
        # Récupérer les noms de colonnes
        c.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in c.fetchall()]
        
        if not rows:
            print(f"[OK] La table {table} est vide.")
            return True
            
        last_sig = None
        erreurs = 0
        
        for row in rows:
            row_dict = dict(zip(cols, row))
            
            # Ignorer les lignes créées avant la mise en place de la signature (optionnel, mais utile pour rétrocompatibilité)
            if row_dict.get(sig_col) is None:
                continue
                
            actuel_hash_prec = row_dict[hash_prec_col]
            signature_enregistree = row_dict[sig_col]
            
            if last_sig is None:
                # Premier enregistrement signé
                expected_hash_prec = actuel_hash_prec # On accepte le hash précédent initial
            else:
                expected_hash_prec = last_sig
                
            if actuel_hash_prec != expected_hash_prec:
                print(f"[ALERTE] Rupture de chaîne détectée à l'ID {row_dict[id_col]} ! Attendu: {expected_hash_prec}, Trouvé: {actuel_hash_prec}")
                erreurs += 1
                
            # Re-calcul de la signature
            computed_sig = compute_hash_func(row_dict)
            if computed_sig != signature_enregistree:
                print(f"[ALERTE] Falsification de données détectée à l'ID {row_dict[id_col]} ! La signature ne correspond pas aux données.")
                erreurs += 1
                
            last_sig = signature_enregistree
            
        if erreurs == 0:
            print(f"[OK] La chaîne cryptographique de {table} est intacte et certifiée conforme.")
            return True
        else:
            print(f"[DANGER] {erreurs} violation(s) d'intégrité détectées sur {table} !")
            return False
    finally:
        conn.close()

def verify_database_integrity(conn=None):
    """
    Parcourt la table des ventes (Tickets) et lève une alerte (ValueError) si un hash ne correspond pas
    (détection de falsification de données ou de suppression d'enregistrement).
    """
    from database_manager import get_connection, calculer_hash_transaction
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, numero_ticket, date_heure, total_tvac, caisse_id, details_articles, 
                   previous_hash, current_hash, signature, hash_precedent 
            FROM Tickets ORDER BY id ASC
        """)
        rows = c.fetchall()

        if not rows:
            print("[OK] La table des ventes (Tickets) est vide.")
            return True

        last_hash = None
        erreurs = []

        for row in rows:
            t_id, num, dt, total, caisse, details, prev_h, curr_h, sig, hash_prec = row
            
            actuel_prev = prev_h if prev_h is not None else hash_prec
            actuel_curr = curr_h if curr_h is not None else sig

            if actuel_curr is None:
                continue

            if last_hash is None:
                expected_prev = actuel_prev
            else:
                expected_prev = last_hash

            # 1. Vérification de la continuité de la chaîne
            if actuel_prev != expected_prev:
                msg = f"Rupture de chaîne détectée au ticket ID {t_id} ({num})! Attendu previous_hash: {expected_prev}, Trouvé: {actuel_prev}"
                print(f"[ALERTE] {msg}")
                erreurs.append(msg)

            # 2. Recalcul et vérification du hash SHA-256
            caisse_val = caisse if caisse is not None else "POS-01"
            details_val = details if details is not None else ""

            computed_hash = calculer_hash_transaction(actuel_prev, dt, total, caisse_val, details_val)
            
            if actuel_curr != computed_hash:
                import hashlib
                from decimal import Decimal
                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
                total_str = f"{Decimal(str(total)):.2f}"
                legacy_data = f"{actuel_prev}|{num}|{total_str}|{dt_str}"
                legacy_hash = hashlib.sha256(legacy_data.encode('utf-8')).hexdigest()

                if actuel_curr != legacy_hash:
                    msg = f"Falsification de données détectée au ticket ID {t_id} ({num})! Hash enregistré: {actuel_curr}, Hash calculé: {computed_hash}"
                    print(f"[ALERTE] {msg}")
                    erreurs.append(msg)
                else:
                    computed_hash = legacy_hash

            last_hash = actuel_curr

        if erreurs:
            raise ValueError(f"Violation d'intégrité de la base de données ({len(erreurs)} erreur(s)):\n" + "\n".join(erreurs))

        print("[OK] L'intégrité de la table des ventes (Tickets) est validée.")
        return True
    finally:
        if close_conn:
            conn.close()

def audit_complet():
    def hash_ticket(r):
        import datetime
        from decimal import Decimal
        dt = r['date_heure']
        dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
        total_str = f"{Decimal(str(r['total_tvac'])):.2f}"
        data = f"{r['hash_precedent']}|{r['numero_ticket']}|{total_str}|{dt_str}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
        
    def hash_ledger(r):
        import datetime
        from decimal import Decimal
        dt = r['date_heure']
        dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
        montant_str = f"{Decimal(str(r['montant'])):.2f}"
        data = f"{r['hash_precedent']}|{r['type_mouvement']}|{montant_str}|{r['methode_paiement']}|{r['reference']}|{dt_str}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
        
    def hash_z(r):
        data = f"{r['hash_precedent']}|{r['date']}|{r['donnees_json']}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    print("\nLancement de l'Audit Trail NF525...\n")
    try:
        verify_database_integrity()
        integrity_ok = True
    except ValueError as e:
        print(f"[ERREUR D'INTEGRITE] {e}")
        integrity_ok = False

    t = verifier_chainage("Tickets", compute_hash_func=hash_ticket)
    l = verifier_chainage("Ledger_Caisse", compute_hash_func=hash_ledger)
    z = verifier_chainage("Rapports_Z", compute_hash_func=hash_z)
    
    if integrity_ok and t and l and z:
        print("\n=> RÉSULTAT: CONFORME. Aucune altération des données n'a été détectée.")
    else:
        print("\n=> RÉSULTAT: NON CONFORME. La base de données a été modifiée frauduleusement ou corrompue.")

def calculer_hash_cloture(hash_prec: str, date_cloture: str, caisse_id: str, total_tvac, total_especes, total_carte) -> str:
    """Calcule le hash cryptographique SHA-256 scellant un Z de Caisse (NF525)."""
    from decimal import Decimal
    h_prec = hash_prec or "GENESIS_Z_00000000000000000000000000000000"
    tvac_str = f"{Decimal(str(total_tvac)):.2f}"
    esp_str = f"{Decimal(str(total_especes)):.2f}"
    carte_str = f"{Decimal(str(total_carte)):.2f}"
    payload = f"{h_prec}|{date_cloture}|{caisse_id}|{tvac_str}|{esp_str}|{carte_str}"
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

if __name__ == "__main__":
    audit_complet()
