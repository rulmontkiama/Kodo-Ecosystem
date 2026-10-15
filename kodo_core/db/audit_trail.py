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
    HASH_ALGO_V3,
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

# --- Scellement des Z : version 2 ----------------------------------------------------------------
# La v1 ne scelle que total TVAC, espèces et carte : les rubriques ajoutées ensuite (QR, avoirs, arrondi
# espèces, bons vendus), la TVA, les remises, le fond compté et l'écart pouvaient être retouchés après
# coup sans que la chaîne le voie. La v2 les scelle toutes, plus la période couverte.
#
# Déploiement en DEUX temps, pour qu'un retour arrière ne produise jamais de fausse alerte :
#   1. ce vérificateur sait lire v1 ET v2 (et ne condamne pas une version qu'il ne connaît pas) ;
#   2. seulement ensuite, une version suivante ÉCRIT en v2 (`Clotures_Caisse.hash_version` = 2).
# Les Z déjà scellés restent en v1, vérifiés avec leur formule d'origine : on ne réécrit jamais le passé.
HASH_Z_V1 = 1
HASH_Z_V2 = 2
CHAMPS_MONTANTS_HASH_Z_V2 = (
    "total_ventes_tvac", "total_htva", "total_tva", "total_remises", "total_especes", "total_carte",
    "total_qr", "total_avoir", "total_arrondi_cash", "total_bons_vendus", "fond_caisse_reel", "ecart",
)


def calculer_hash_cloture_v2(hash_prec, date_cloture, caisse_id, periode_jusqu_au, valeurs) -> str:
    """Hash SHA-256 v2 d'un Z. `valeurs` : mapping portant les CHAMPS_MONTANTS_HASH_Z_V2 et `total_tickets`
    (une ligne de `Clotures_Caisse`, ou les valeurs qu'on s'apprête à y écrire : la même fonction sert à
    sceller et à vérifier, donc aucune divergence possible)."""
    h_prec = hash_prec or "GENESIS_Z_00000000000000000000000000000000"
    montants = "|".join(f"{Decimal(str(valeurs.get(champ) or 0)):.2f}" for champ in CHAMPS_MONTANTS_HASH_Z_V2)
    nb = int(valeurs.get("total_tickets") or 0)
    payload = (f"Z2|{h_prec}|{date_cloture}|{caisse_id}|{periode_jusqu_au or ''}|{montants}|{nb}")
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

def _lire_lignes(cursor, table: str):
    """
    Lit les lignes d'une table scellée. Une valeur que le convertisseur de colonne ne sait plus lire (texte dans
    une colonne DECIMAL…) lève dès le `fetchall` : c'est un contenu ALTÉRÉ, pas une panne, et le contrôle doit le
    dire (None = illisible) au lieu de planter. Les vraies erreurs SQLite (`sqlite3.Error`) continuent de remonter.
    """
    try:
        return cursor.fetchall()
    except sqlite3.Error:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[ALERTE] Contenu illisible dans {table} ({type(e).__name__}).")
        return None


def verifier_chainage(table: str, id_col="id", sig_col="signature", hash_prec_col="hash_precedent", compute_hash_func=None, conn=None) -> bool:
    """
    Vérifie l'intégrité de la chaîne séquentielle cryptographique pour une table donnée.
    """
    # Liste blanche stricte des tables et colonnes d'audit (CWE-89)
    ALLOWED_TABLES = {"Tickets", "Ledger_Caisse", "Clotures_Caisse", "Rapports_Z", "Audit_Trail", "tickets", "ledger_caisse", "clotures_caisse", "rapports_z", "audit_trail"}
    ALLOWED_COLS = {"id", "id_ticket", "rowid"}
    if table not in ALLOWED_TABLES or id_col not in ALLOWED_COLS:
        raise ValueError(f"Table ou colonne non autorisée pour verifier_chainage : {table}.{id_col}")

    safe_conn = get_connection(conn=conn)
    close_at_end = (conn is None)
    try:
        c = safe_conn.cursor()
        c.execute(f"SELECT * FROM {table} ORDER BY {id_col} ASC")
        rows = _lire_lignes(c, table)
        if rows is None:
            return False

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
                # Ancrage obligatoire sur le bloc de genèse officiel (NF525 / Audit Trail)
                expected_genesis = {
                    "ledger_caisse": "GENESIS_LEDGER_KODO_POS",
                    "rapports_z": "GENESIS_Z_KODO_POS",
                    "audit_trail": "GENESIS_AUDIT_TRAIL_KODO_POS",
                }.get(table.lower(), "GENESIS_BLOCK_KODO_POS")
                if actuel_hash_prec != expected_genesis:
                    print(f"[ALERTE] Faille de genèse dans {table} à l'ID {row_dict[id_col]}! Hash initial non conforme: {actuel_hash_prec}")
                    erreurs += 1
                expected_hash_prec = actuel_hash_prec
            else:
                expected_hash_prec = last_sig

            if actuel_hash_prec != expected_hash_prec:
                print(f"[ALERTE] Rupture de chaîne dans {table} à l'ID {row_dict[id_col]}! Attendu: {expected_hash_prec}, Trouvé: {actuel_hash_prec}")
                erreurs += 1

            if compute_hash_func:
                # Une ligne dont le contenu n'est plus lisible (montant devenu texte, date absente…) est une
                # ligne ALTÉRÉE : elle compte comme violation, elle ne doit jamais faire planter la vérification
                # (sinon l'appelant ne voit qu'une « erreur » au lieu d'une rupture de chaîne).
                try:
                    computed_sig = compute_hash_func(row_dict)
                except Exception as e:  # noqa: BLE001
                    print(f"[ALERTE] Ligne illisible dans {table} à l'ID {row_dict[id_col]} ({type(e).__name__}).")
                    erreurs += 1
                    last_sig = signature_enregistree
                    continue
                if computed_sig != signature_enregistree:
                    # Rétrocompatibilité : tester les algorithmes connus (V3 HMAC, V2, V1) si compute_hash_func
                    # diverge. Ces candidats ne valent QUE pour les tickets (seule table qui porte total_tvac) :
                    # appliqués au journal de caisse ou aux Z, ils calculaient sur des champs absents.
                    candidats = []
                    if "total_tvac" in row_dict:
                        num = row_dict.get('numero_ticket')
                        total = row_dict.get('total_tvac')
                        dt = row_dict.get('date_heure')
                        caisse = row_dict.get('caisse_id', 'POS-01')
                        details = row_dict.get('details_articles', '')
                        try:
                            candidats = [
                                calculer_hash_transaction(actuel_hash_prec, dt, total, caisse, details, numero_ticket=num, algo=HASH_ALGO_V3),
                                calculer_hash_transaction(actuel_hash_prec, dt, total, caisse, details, numero_ticket=num, algo=HASH_ALGO_V2),
                                calculer_hash_transaction(actuel_hash_prec, dt, total, caisse, details, algo=HASH_ALGO_V1),
                            ]
                        except Exception:  # noqa: BLE001 — contenu illisible : aucun candidat ne peut correspondre
                            candidats = []
                    if signature_enregistree not in candidats:
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
        rows = _lire_lignes(c, "Tickets")
        if rows is None:
            raise ValueError("Violation d'intégrité : contenu des tickets illisible (valeur altérée).")

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

            # Algorithme courant (V3 HMAC avec clé machine). Un montant ou une date devenus illisibles
            # (texte dans une colonne numérique…) = ticket altéré : violation, jamais un plantage.
            try:
                computed_hash = calculer_hash_transaction(
                    actuel_prev,
                    dt,
                    total,
                    caisse_val,
                    details_val,
                    numero_ticket=num,
                    algo=HASH_ALGO_COURANT,
                )
            except Exception as e:  # noqa: BLE001
                msg = f"Ticket illisible (ID {t_id}, {num}) : {type(e).__name__}"
                print(f"[ALERTE] {msg}")
                erreurs.append(msg)
                last_hash = actuel_curr
                continue

            if actuel_curr != computed_hash:
                # Rétrocompatibilité : les maillons antérieurs (V2 SHA-256 avec ticket, V1 legacy)
                # ou calculés avec un autre algorithme restent vérifiés.
                candidats = [
                    calculer_hash_transaction(actuel_prev, dt, total, caisse_val, details_val, numero_ticket=num, algo=HASH_ALGO_V3),
                    calculer_hash_transaction(actuel_prev, dt, total, caisse_val, details_val, numero_ticket=num, algo=HASH_ALGO_V2),
                    calculer_hash_transaction(actuel_prev, dt, total, caisse_val, details_val, algo=HASH_ALGO_V1),
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

def hash_ledger_row(r) -> str:
    """Empreinte attendue d'une ligne de Ledger_Caisse (r = dict de la ligne)."""
    dt = r['date_heure']
    dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, 'strftime') else str(dt)
    montant_str = f"{Decimal(str(r['montant'])):.2f}"
    data = f"{r['hash_precedent']}|{r['type_mouvement']}|{montant_str}|{r['methode_paiement']}|{r['reference']}|{dt_str}"
    return compute_sha256(data)


def hash_z_row(r) -> str:
    """Empreinte attendue d'une ligne de Rapports_Z (r = dict de la ligne)."""
    data = f"{r['hash_precedent']}|{r['date']}|{r['donnees_json']}"
    return compute_sha256(data)


def verifier_clotures(conn=None) -> bool:
    """
    Chaîne des VRAIS Z de caisse (`Clotures_Caisse`, scellés par `enregistrer_cloture_caisse`), une chaîne
    par caisse. Ne vérifie PAS que `Rapports_Z` (journal des Z) : sans ce contrôle, une clôture dont les
    totaux ont été retouchés après coup passait inaperçue. Lecture seule.
    """
    safe_conn = get_connection(conn=conn)
    close_at_end = (conn is None)
    try:
        c = safe_conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='Clotures_Caisse'")
        if c.fetchone() is None:
            return True
        c.execute("PRAGMA table_info(Clotures_Caisse)")
        cols = [r[1] for r in c.fetchall()]
        if not {"hash_precedent", "current_hash", "total_ventes_tvac", "total_especes", "total_carte",
                "date_cloture", "caisse_id"} <= set(cols):
            return True  # base antérieure à la chaîne des Z : rien à vérifier
        c.execute("SELECT * FROM Clotures_Caisse ORDER BY id ASC")
        lignes = _lire_lignes(c, "Clotures_Caisse")
        if lignes is None:
            return False
        derniere: dict = {}
        erreurs = 0
        non_jugees = 0
        for row in lignes:
            r = dict(zip(cols, row))
            courant = r.get("current_hash") or r.get("signature")
            if courant is None:
                continue  # Z d'une version qui ne scellait pas encore
            caisse = r["caisse_id"]
            prec = r.get("hash_precedent")
            if caisse in derniere and prec != derniere[caisse]:
                print(f"[ALERTE] Rupture de chaîne des Z (caisse {caisse}) à l'ID {r['id']}.")
                erreurs += 1
            try:
                version = int(r.get("hash_version") or HASH_Z_V1)
            except (TypeError, ValueError):
                version = -1
            if version not in (HASH_Z_V1, HASH_Z_V2):
                # Scellé par une version PLUS RÉCENTE de Kōdo que ce code (retour arrière) : on ne peut pas
                # juger ce Z, et on ne l'accuse pas. Le chaînage (hash_precedent) est tout de même vérifié.
                print(f"[INFO] Clôture Z {r['id']} scellée en version {r.get('hash_version')!r} : formule inconnue "
                      f"de cette version, non jugée.")
                non_jugees += 1
                derniere[caisse] = courant
                continue
            try:
                if version == HASH_Z_V2:
                    attendu = calculer_hash_cloture_v2(prec, r["date_cloture"], caisse,
                                                       r.get("periode_jusqu_au"), r)
                else:
                    attendu = calculer_hash_cloture(prec, r["date_cloture"], caisse, r["total_ventes_tvac"],
                                                    r["total_especes"], r["total_carte"])
            except Exception as e:  # noqa: BLE001 — montant illisible = clôture altérée
                print(f"[ALERTE] Clôture Z illisible à l'ID {r['id']} ({type(e).__name__}).")
                erreurs += 1
                derniere[caisse] = courant
                continue
            if attendu != courant:
                print(f"[ALERTE] Totaux d'une clôture Z modifiés après scellement (ID {r['id']}).")
                erreurs += 1
            derniere[caisse] = courant
        if erreurs:
            print(f"[DANGER] {erreurs} violation(s) d'intégrité sur Clotures_Caisse!")
            return False
        print("[OK] La chaîne des clôtures Z de caisse est intacte.")
        return True
    finally:
        if close_at_end:
            safe_conn.close()


def audit_complet(conn=None) -> dict:
    """Effectue un audit cryptographique complet de toutes les tables scellées."""
    hash_ledger, hash_z = hash_ledger_row, hash_z_row

    print("\nLancement de l'Audit de Traçabilité & Chaînage SHA-256...\n")
    report = {
        "tickets_ok": False,
        "ledger_ok": False,
        "z_ok": False,
        "clotures_ok": False,
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
    report["clotures_ok"] = verifier_clotures(conn=conn)
    report["audit_ok"] = verifier_chainage("Audit_Trail", conn=conn)

    report["conforme"] = (report["tickets_ok"] and report["ledger_ok"] and report["z_ok"]
                          and report["clotures_ok"] and report["audit_ok"])

    if report["conforme"]:
        print("\n=> RÉSULTAT AUDIT : chaînage intact. Aucune altération détectée sur les "
              "tables scellées (tickets, journal de caisse, clôtures Z, piste d'audit).")
    else:
        print("\n=> RÉSULTAT AUDIT : ALTÉRATION OU CORRUPTION DÉTECTÉE. "
              "Ne pas poursuivre l'exploitation avant analyse du rapport ci-dessus.")

    return report

if __name__ == "__main__":
    audit_complet()
