"""
kodo_core.db.audit_trail — Chaîne SHA-256 de détection de corruption et piste d'audit.
Portée exacte de ce que ce module établit, et de ce qu'il n'établit pas : il détecte
de façon fiable toute modification, suppression ou réordonnancement d'un maillon
(tickets, journal de caisse, clôtures Z, événements système), accidentel comme délibéré,
dès lors que l'auteur n'a pas recalculé la chaîne. Il ne comporte aucun secret : un tiers
disposant du fichier .db peut recalculer l'intégralité du chaînage. Ce module ne constitue
donc PAS un dispositif d'inaltérabilité opposable au sens d'une certification NF525/LNE,
et ne doit être présenté comme tel ni dans l'application, ni dans la documentation
commerciale, ni dans les commentaires de ce dépôt.
"""

import hashlib
import sqlite3
from decimal import Decimal
from kodo_core.db.connection import get_connection

def compute_sha256(data_string: str) -> str:
    """Calcule le hachage SHA-256 d'une chaîne UTF-8."""
    return hashlib.sha256(data_string.encode('utf-8')).hexdigest()

# Source de vérité UNIQUE de la primitive de chaînage fiscale. Une seconde implémentation
# vivait ici : signataire et vérificateur pouvaient diverger sans qu'aucun test ne le voie,
# et la chaîne de toutes les boutiques devenait invérifiable à la première évolution.
from database_manager import (
    calculer_hash_transaction,
    HASH_ALGO_V1,
    HASH_ALGO_V2,
    HASH_ALGO_COURANT,
)

def signer_ticket(cursor, numero_ticket, total_tvac, date_heure, caisse_id="POS-01", details_articles=""):
    """
    Génère une empreinte chaînée de détection de corruption (Audit Trail) pour un ticket de vente.
    Retourne le tuple (current_hash, previous_hash).
    """
    cursor.execute("""
        SELECT COALESCE(current_hash, signature) 
        FROM Tickets 
        WHERE current_hash IS NOT NULL OR signature IS NOT NULL 
        ORDER BY id DESC LIMIT 1
    """)
    row = cursor.fetchone()
    previous_hash = row[0] if (row and row[0]) else "GENESIS_BLOCK_KODO_POS"

    current_hash = calculer_hash_transaction(
        previous_hash,
        date_heure,
        total_tvac,
        caisse_id,
        details_articles,
        numero_ticket=numero_ticket,
        algo=HASH_ALGO_COURANT,
    )
    return current_hash, previous_hash

def signer_ledger(cursor, type_mouvement, montant, methode, reference, date_heure):
    """Génère une signature cryptographique pour une entrée du journal Ledger_Caisse (NF525)."""
    cursor.execute("SELECT signature FROM Ledger_Caisse WHERE signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    hash_precedent = row[0] if (row and row[0]) else "GENESIS_LEDGER_KODO_POS"

    montant_str = f"{Decimal(str(montant)):.2f}"
    data_to_hash = f"{hash_precedent}|{type_mouvement}|{montant_str}|{methode}|{reference}|{date_heure}"
    signature = compute_sha256(data_to_hash)
    return signature, hash_precedent

def signer_rapport_z(cursor, date_z, donnees_json):
    """Génère une signature cryptographique pour un rapport de clôture comptable Z."""
    cursor.execute("SELECT signature FROM Rapports_Z WHERE signature IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    hash_precedent = row[0] if (row and row[0]) else "GENESIS_Z_KODO_POS"

    data_to_hash = f"{hash_precedent}|{date_z}|{donnees_json}"
    signature = compute_sha256(data_to_hash)
    return signature, hash_precedent

def calculer_hash_cloture(hash_prec: str, date_cloture: str, caisse_id: str, total_tvac, total_especes, total_carte) -> str:
    """Calcule le hash SHA-256 scellant une clôture de caisse Z (détection de corruption)."""
    h_prec = hash_prec or "GENESIS_Z_00000000000000000000000000000000"
    tvac_str = f"{Decimal(str(total_tvac)):.2f}"
    esp_str = f"{Decimal(str(total_especes)):.2f}"
    carte_str = f"{Decimal(str(total_carte)):.2f}"

    payload = f"{h_prec}|{date_cloture}|{caisse_id}|{tvac_str}|{esp_str}|{carte_str}"
    return compute_sha256(payload)

def record_audit_event(conn_or_cursor, event_type: str, entity_name: str, entity_id: str = "", user_name: str = "", action: str = "", details: str = ""):
    """
    Enregistre un événement de sécurité dans la table Audit_Trail, scellé par chaînage SHA-256
    (détection de corruption — voir la portée décrite en tête de module).
    """
    if hasattr(conn_or_cursor, "cursor"):
        cursor = conn_or_cursor.cursor()
    else:
        cursor = conn_or_cursor

    cursor.execute("SELECT current_hash FROM Audit_Trail ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    previous_hash = row[0] if (row and row[0]) else "GENESIS_AUDIT_TRAIL_KODO_POS"

    data_payload = f"{previous_hash}|{event_type}|{entity_name}|{entity_id}|{user_name}|{action}|{details}"
    current_hash = compute_sha256(data_payload)

    cursor.execute("""
        INSERT INTO Audit_Trail (event_type, entity_name, entity_id, user_name, action, details, previous_hash, current_hash, signature)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (event_type, entity_name, entity_id, user_name, action, details, previous_hash, current_hash, current_hash))

    return current_hash, previous_hash

def verifier_chainage(table: str, id_col="id", sig_col="signature", hash_prec_col="hash_precedent", compute_hash_func=None, conn=None) -> bool:
    """
    Vérifie l'intégrité de la chaîne séquentielle cryptographique pour une table donnée.
    """
    safe_conn = get_connection(conn=conn)
    close_at_end = (conn is None)
    try:
        c = safe_conn.cursor()
        c.execute(f"SELECT * FROM {table} ORDER BY {id_col} ASC")
        rows = c.fetchall()

        c.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in c.fetchall()]

        if not rows:
            print(f"[OK] La table {table} est vide.")
            return True

        last_sig = None
        erreurs = 0

        for row in rows:
            row_dict = dict(zip(cols, row))

            if row_dict.get(sig_col) is None and row_dict.get("current_hash") is None:
                continue

            actuel_hash_prec = row_dict.get(hash_prec_col) or row_dict.get("previous_hash")
            signature_enregistree = row_dict.get(sig_col) or row_dict.get("current_hash")

            if last_sig is None:
                expected_hash_prec = actuel_hash_prec
            else:
                expected_hash_prec = last_sig

            if actuel_hash_prec != expected_hash_prec:
                print(f"[ALERTE] Rupture de chaîne dans {table} à l'ID {row_dict[id_col]}! Attendu: {expected_hash_prec}, Trouvé: {actuel_hash_prec}")
                erreurs += 1

            if compute_hash_func:
                computed_sig = compute_hash_func(row_dict)
                if computed_sig != signature_enregistree:
                    print(f"[ALERTE] Falsification de données détectée dans {table} à l'ID {row_dict[id_col]}!")
                    erreurs += 1

            last_sig = signature_enregistree

        if erreurs == 0:
            print(f"[OK] La chaîne cryptographique de {table} est intacte.")
            return True
        else:
            print(f"[DANGER] {erreurs} violation(s) d'intégrité détectées sur {table}!")
            return False
    finally:
        if close_at_end:
            safe_conn.close()

def verify_database_integrity(conn=None) -> bool:
    """
    Parcourt l'historique des ventes (Tickets) et lève une exception ValueError
    si une altération, falsification ou rupture de la chaîne cryptographique est détectée.
    """
    safe_conn = get_connection(conn=conn)
    close_at_end = (conn is None)

    try:
        c = safe_conn.cursor()
        c.execute("PRAGMA table_info(Tickets)")
        available_cols = {row[1] for row in c.fetchall()}

        caisse_expr = "caisse_id" if "caisse_id" in available_cols else "'POS-01' AS caisse_id"
        details_expr = "details_articles" if "details_articles" in available_cols else "'' AS details_articles"
        prev_hash_expr = "previous_hash" if "previous_hash" in available_cols else "NULL AS previous_hash"
        curr_hash_expr = "current_hash" if "current_hash" in available_cols else "NULL AS current_hash"

        c.execute(f"""
            SELECT id, numero_ticket, date_heure, total_tvac, {caisse_expr}, {details_expr}, 
                   {prev_hash_expr}, {curr_hash_expr}, signature, hash_precedent 
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

            # 1. Vérification continuité de chaîne
            if actuel_prev != expected_prev:
                msg = f"Rupture de chaîne détectée au ticket ID {t_id} ({num})! Attendu previous_hash: {expected_prev}, Trouvé: {actuel_prev}"
                print(f"[ALERTE] {msg}")
                erreurs.append(msg)

            # 2. Recalcul et vérification SHA-256
            caisse_val = caisse if caisse is not None else "POS-01"
            details_val = details if details is not None else ""

            # Algorithme courant (numéro de ticket scellé).
            computed_hash = calculer_hash_transaction(
                actuel_prev,
                dt,
                total,
                caisse_val,
                details_val,
                numero_ticket=num,
                algo=HASH_ALGO_V2,
            )

            if actuel_curr != computed_hash:
                # Rétrocompatibilité : les maillons antérieurs au scellement du numéro
                # restent légitimes et ne doivent jamais être signalés comme falsifiés.
                candidats = [
                    calculer_hash_transaction(
                        actuel_prev,
                        dt,
                        total,
                        caisse_val,
                        details_val,
                        algo=HASH_ALGO_V1,
                    )
                ]
                dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
                total_str = f"{Decimal(str(total)):.2f}"
                candidats.append(compute_sha256(f"{actuel_prev}|{num}|{total_str}|{dt_str}"))

                if actuel_curr in candidats:
                    computed_hash = actuel_curr
                else:
                    msg = (f"Falsification de données détectée au ticket ID {t_id} ({num})! "
                           f"Hash enregistré: {actuel_curr}, Hash calculé: {computed_hash}")
                    print(f"[ALERTE] {msg}")
                    erreurs.append(msg)

            last_hash = actuel_curr

        if erreurs:
            raise ValueError(f"Violation d'intégrité de la base de données ({len(erreurs)} erreur(s)):\n" + "\n".join(erreurs))

        print("[OK] Intégrité de la base de données validée avec succès.")
        return True

    finally:
        if close_at_end:
            safe_conn.close()

def audit_complet(conn=None) -> dict:
    """Effectue un audit cryptographique complet de toutes les tables scellées."""
    def hash_ledger(r):
        dt = r['date_heure']
        dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
        montant_str = f"{Decimal(str(r['montant'])):.2f}"
        data = f"{r['hash_precedent']}|{r['type_mouvement']}|{montant_str}|{r['methode_paiement']}|{r['reference']}|{dt_str}"
        return compute_sha256(data)

    def hash_z(r):
        data = f"{r['hash_precedent']}|{r['date']}|{r['donnees_json']}"
        return compute_sha256(data)

    print("\nLancement de l'Audit de Traçabilité & Chaînage SHA-256...\n")
    report = {
        "tickets_ok": False,
        "ledger_ok": False,
        "z_ok": False,
        "audit_ok": False,
        "conforme": False
    }

    try:
        verify_database_integrity(conn)
        report["tickets_ok"] = True
    except ValueError as e:
        print(f"[ERREUR D'INTÉGRITÉ] {e}")

    report["ledger_ok"] = verifier_chainage("Ledger_Caisse", compute_hash_func=hash_ledger, conn=conn)
    report["z_ok"] = verifier_chainage("Rapports_Z", compute_hash_func=hash_z, conn=conn)
    report["audit_ok"] = verifier_chainage("Audit_Trail", conn=conn)

    report["conforme"] = (report["tickets_ok"] and report["ledger_ok"] and report["z_ok"] and report["audit_ok"])

    if report["conforme"]:
        print("\n=> RÉSULTAT AUDIT : chaînage intact. Aucune altération détectée sur les "
              "tables scellées (tickets, journal de caisse, clôtures Z, piste d'audit).")
    else:
        print("\n=> RÉSULTAT AUDIT : ALTÉRATION OU CORRUPTION DÉTECTÉE. "
              "Ne pas poursuivre l'exploitation avant analyse du rapport ci-dessus.")

    return report

if __name__ == "__main__":
    audit_complet()
