# -*- coding: utf-8 -*-
"""
Gestionnaire Live Shopping - Kōdo POS Core
Gère les sessions de vente en direct, la file d'attente FIFO (1er arrivé / 1er servi),
les acheteurs live, la synchronisation avec le CRM Clients et l'encaissement POS.
"""

import json
import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Optional

from kodo_core.db.connection import get_connection


class LiveManager:
    """
    Gestionnaire centralisé du module Live Shopping.
    Algorithme FIFO garanti : le rang est attribué selon l'ordre strict de création.
    Statuts de paiement : non_paye | paye | sur_place
    Statuts d'attribution : attribué | file_attente | annulé | encaissé
    Statuts de commande : en_attente | validé | expédié | retiré | annulé
    """

    # -------------------------------------------------------------------------
    # 0. INITIALISATION DES TABLES
    # -------------------------------------------------------------------------

    @classmethod
    def init_tables(cls, conn=None):
        """Initialise les tables Live Shopping si elles n'existent pas encore."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS Live_Sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    titre TEXT NOT NULL,
                    statut TEXT DEFAULT 'en_cours',
                    date_debut DATETIME DEFAULT CURRENT_TIMESTAMP,
                    date_fin DATETIME,
                    produit_vedette_id INTEGER,
                    notes TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS Live_Buyers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER,
                    nom TEXT,
                    prenom TEXT,
                    telephone TEXT,
                    email TEXT,
                    pseudo_social TEXT,
                    mode_reception TEXT DEFAULT 'retrait_magasin',
                    adresse_rue TEXT,
                    code_postal TEXT,
                    ville TEXT,
                    pays TEXT,
                    taille_haut TEXT,
                    taille_bas TEXT,
                    pointure TEXT,
                    notes TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS Live_Claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    buyer_id INTEGER NOT NULL,
                    client_id INTEGER,
                    product_id INTEGER NOT NULL,
                    stock_id INTEGER,
                    article_nom TEXT NOT NULL,
                    taille TEXT,
                    prix_unitaire_tvac REAL NOT NULL DEFAULT 0.0,
                    quantite INTEGER NOT NULL DEFAULT 1,
                    statut_attribution TEXT DEFAULT 'file_attente',
                    rang_file INTEGER DEFAULT 1,
                    statut_paiement TEXT DEFAULT 'non_paye',
                    statut_commande TEXT DEFAULT 'en_attente',
                    ticket_pos_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 1. SESSIONS LIVE
    # -------------------------------------------------------------------------

    @classmethod
    def get_active_session(cls, conn=None) -> Optional[Dict[str, Any]]:
        """Retourne la session live active (la plus récente), ou None si aucune."""
        cls.init_tables(conn=conn)
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.id, s.titre, s.statut, s.date_debut, s.produit_vedette_id,
                       p.nom as produit_vedette_nom, p.prix_vente_tvac, p.image_path, s.notes
                FROM Live_Sessions s
                LEFT JOIN Produits p ON s.produit_vedette_id = p.id
                WHERE s.statut = 'active'
                ORDER BY s.id DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "titre": row[1],
                "statut": row[2],
                "date_debut": str(row[3]) if row[3] else None,
                "produit_vedette_id": row[4],
                "produit_vedette_nom": row[5],
                "produit_vedette_prix": float(row[6]) if row[6] else None,
                "produit_vedette_image": row[7] or "",
                "notes": row[8] or "",
            }
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_all_sessions(cls, conn=None) -> List[Dict[str, Any]]:
        """Retourne toutes les sessions live dans l'ordre décroissant."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.id, s.titre, s.statut, s.date_debut, s.date_fin,
                       s.produit_vedette_id, s.notes, p.nom as produit_nom
                FROM Live_Sessions s
                LEFT JOIN Produits p ON s.produit_vedette_id = p.id
                ORDER BY s.id DESC
            """)
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "titre": r[1],
                    "statut": r[2],
                    "date_debut": str(r[3]) if r[3] else None,
                    "date_fin": str(r[4]) if r[4] else None,
                    "produit_vedette_id": r[5],
                    "notes": r[6] or "",
                    "produit_vedette_nom": r[7] or "",
                }
                for r in rows
            ]
        finally:
            if should_close:
                conn.close()

    @classmethod
    def create_or_update_session(cls, data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Crée ou met à jour une session live.
        Si une session active existe et que action='close', la ferme proprement.
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            session_id = data.get("id")
            action = data.get("action", "")
            titre = data.get("titre", "Live Shopping")
            produit_vedette_id = data.get("produit_vedette_id")
            statut = data.get("statut", "active")
            notes = data.get("notes", "")

            if action == "close":
                # Fermeture de la session active
                cursor.execute("""
                    UPDATE Live_Sessions SET statut='terminé', date_fin=CURRENT_TIMESTAMP
                    WHERE statut='active'
                """)
                conn.commit()
                return {"success": True, "action": "closed"}

            if session_id:
                cursor.execute("""
                    UPDATE Live_Sessions
                    SET titre=?, statut=?, produit_vedette_id=?, notes=?
                    WHERE id=?
                """, (titre, statut, produit_vedette_id, notes, session_id))
                conn.commit()
                return {"success": True, "session_id": session_id}
            else:
                # Fermer toute session active préalable
                cursor.execute("""
                    UPDATE Live_Sessions SET statut='terminé', date_fin=CURRENT_TIMESTAMP
                    WHERE statut='active'
                """)
                cursor.execute("""
                    INSERT INTO Live_Sessions (titre, statut, produit_vedette_id, notes)
                    VALUES (?, 'active', ?, ?)
                """, (titre, produit_vedette_id, notes))
                new_id = cursor.lastrowid
                conn.commit()
                return {"success": True, "session_id": new_id}
        finally:
            if should_close:
                conn.close()

    @classmethod
    def set_featured_product(cls, session_id: int, product_id: Optional[int], conn=None) -> bool:
        """Met à jour le produit vedette actuellement présenté en live."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE Live_Sessions SET produit_vedette_id=? WHERE id=?
            """, (product_id, session_id))
            conn.commit()
            return True
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 2. ACHETEURS LIVE (Live_Buyers + sync CRM Clients)
    # -------------------------------------------------------------------------

    @classmethod
    def register_buyer(cls, data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Inscrit ou met à jour un acheteur live.
        Synchronise automatiquement avec la table Clients du CRM.
        Retourne le buyer_id et le client_id.
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()

            nom = (data.get("nom") or "").strip()
            prenom = (data.get("prenom") or "").strip()
            telephone = (data.get("telephone") or "").strip()
            email = (data.get("email") or "").strip()
            pseudo_social = (data.get("pseudo_social") or data.get("pseudo") or "").strip()
            mode_reception = data.get("mode_reception", "retrait_magasin")
            adresse_rue = (data.get("adresse_rue") or data.get("adresse") or "").strip()
            code_postal = (data.get("code_postal") or "").strip()
            ville = (data.get("ville") or "").strip()
            pays = (data.get("pays") or "France").strip()
            taille_haut = (data.get("taille_haut") or "").strip()
            taille_bas = (data.get("taille_bas") or "").strip()
            pointure = (data.get("pointure") or "").strip()
            notes = (data.get("notes") or "").strip()

            if not nom and not telephone:
                raise ValueError("Au moins le nom ou le téléphone est requis")

            full_nom = f"{prenom} {nom}".strip() if prenom else nom

            # --- Sync CRM Clients ---
            client_id = None
            # Chercher par téléphone puis email
            if telephone:
                cursor.execute("PRAGMA table_info(Clients)")
                client_cols = [row[1] for row in cursor.fetchall()]
                if 'telephone' in client_cols:
                    cursor.execute("SELECT id FROM Clients WHERE telephone=?", (telephone,))
                    row = cursor.fetchone()
                    if row:
                        client_id = row[0]

            if not client_id and email:
                cursor.execute("SELECT id FROM Clients WHERE email=?", (email,))
                row = cursor.fetchone()
                if row:
                    client_id = row[0]

            if client_id:
                # Mise à jour du client existant
                cursor.execute("""
                    UPDATE Clients SET nom=?, email=?, taille_haut=?, taille_bas=?, pointure=?
                    WHERE id=?
                """, (full_nom, email or None, taille_haut, taille_bas, pointure, client_id))
                # Mise à jour colonnes étendues si existantes
                cursor.execute("PRAGMA table_info(Clients)")
                cl = [r[1] for r in cursor.fetchall()]
                for col, val in [('telephone', telephone), ('adresse', adresse_rue),
                                  ('ville', ville), ('code_postal', code_postal),
                                  ('pays', pays), ('instagram', pseudo_social)]:
                    if col in cl and val:
                        cursor.execute(f"UPDATE Clients SET {col}=? WHERE id=?", (val, client_id))
            else:
                # Création du client CRM
                cursor.execute("PRAGMA table_info(Clients)")
                cl = [r[1] for r in cursor.fetchall()]
                if 'telephone' in cl:
                    cursor.execute("""
                        INSERT INTO Clients (nom, email, telephone, taille_haut, taille_bas, pointure)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (full_nom, email or None, telephone or None, taille_haut, taille_bas, pointure))
                else:
                    cursor.execute("""
                        INSERT INTO Clients (nom, email, taille_haut, taille_bas, pointure)
                        VALUES (?, ?, ?, ?, ?)
                    """, (full_nom, email or None, taille_haut, taille_bas, pointure))
                client_id = cursor.lastrowid
                # Ajout colonnes étendues
                for col, val in [('adresse', adresse_rue), ('ville', ville),
                                  ('code_postal', code_postal), ('pays', pays),
                                  ('instagram', pseudo_social)]:
                    if col in cl and val:
                        cursor.execute(f"UPDATE Clients SET {col}=? WHERE id=?", (val, client_id))

            # --- Live_Buyers : chercher acheteur existant par téléphone ou email ---
            buyer_id = None
            if telephone:
                cursor.execute("SELECT id FROM Live_Buyers WHERE telephone=?", (telephone,))
                row = cursor.fetchone()
                if row:
                    buyer_id = row[0]
            if not buyer_id and email:
                cursor.execute("SELECT id FROM Live_Buyers WHERE email=?", (email,))
                row = cursor.fetchone()
                if row:
                    buyer_id = row[0]

            if buyer_id:
                cursor.execute("""
                    UPDATE Live_Buyers
                    SET client_id=?, nom=?, prenom=?, telephone=?, email=?, pseudo_social=?,
                        mode_reception=?, adresse_rue=?, code_postal=?, ville=?, pays=?,
                        taille_haut=?, taille_bas=?, pointure=?, notes=?
                    WHERE id=?
                """, (client_id, nom, prenom, telephone, email, pseudo_social,
                      mode_reception, adresse_rue, code_postal, ville, pays,
                      taille_haut, taille_bas, pointure, notes, buyer_id))
            else:
                cursor.execute("""
                    INSERT INTO Live_Buyers
                    (client_id, nom, prenom, telephone, email, pseudo_social, mode_reception,
                     adresse_rue, code_postal, ville, pays, taille_haut, taille_bas, pointure, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (client_id, nom, prenom, telephone, email, pseudo_social, mode_reception,
                      adresse_rue, code_postal, ville, pays, taille_haut, taille_bas, pointure, notes))
                buyer_id = cursor.lastrowid

            conn.commit()
            return {"success": True, "buyer_id": buyer_id, "client_id": client_id}
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_buyers(cls, session_id: Optional[int] = None, conn=None) -> List[Dict[str, Any]]:
        """Liste tous les acheteurs d'une session (ou tous si session_id=None)."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            if session_id:
                cursor.execute("""
                    SELECT DISTINCT b.id, b.nom, b.prenom, b.telephone, b.email,
                           b.pseudo_social, b.mode_reception, b.adresse_rue,
                           b.code_postal, b.ville, b.pays, b.taille_haut,
                           b.taille_bas, b.pointure, b.created_at, b.client_id
                    FROM Live_Buyers b
                    INNER JOIN Live_Claims c ON c.buyer_id = b.id
                    WHERE c.session_id = ?
                    ORDER BY b.created_at DESC
                """, (session_id,))
            else:
                cursor.execute("""
                    SELECT id, nom, prenom, telephone, email, pseudo_social,
                           mode_reception, adresse_rue, code_postal, ville, pays,
                           taille_haut, taille_bas, pointure, created_at, client_id
                    FROM Live_Buyers ORDER BY created_at DESC
                """)
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0], "nom": r[1], "prenom": r[2] or "",
                    "telephone": r[3] or "", "email": r[4] or "",
                    "pseudo_social": r[5] or "", "mode_reception": r[6] or "retrait_magasin",
                    "adresse_rue": r[7] or "", "code_postal": r[8] or "",
                    "ville": r[9] or "", "pays": r[10] or "France",
                    "taille_haut": r[11] or "", "taille_bas": r[12] or "",
                    "pointure": r[13] or "", "created_at": str(r[14]) if r[14] else "",
                    "client_id": r[15],
                }
                for r in rows
            ]
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 3. FILE D'ATTENTE & RÉSERVATIONS (Live_Claims) — Algorithme FIFO
    # -------------------------------------------------------------------------

    @classmethod
    def submit_claim(cls, data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Soumet une réservation live pour un acheteur.
        Applique l'algorithme FIFO :
          - Rang <= Stock disponible => statut_attribution = 'attribué'
          - Rang > Stock disponible  => statut_attribution = 'file_attente'
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            # Verrou d'écriture immédiat : empêche une lecture concurrente du stock
            # (TOCTOU) entre le calcul du rang FIFO et l'insertion de la claim.
            cursor.execute("BEGIN IMMEDIATE")
            session_id = int(data.get("session_id"))
            buyer_id = int(data.get("buyer_id"))
            product_id = int(data.get("product_id"))
            taille = (data.get("taille") or "Taille Unique").strip()
            quantite = int(data.get("quantite", 1))
            client_id = data.get("client_id")

            # Récupérer infos produit (jointure alignée sur _recalculate_queue
            # pour garantir la même vue du stock disponible)
            cursor.execute("""
                SELECT p.nom, p.prix_vente_tvac, s.id, s.quantite_actuelle
                FROM Produits p
                LEFT JOIN Stocks s ON s.id_produit = p.id AND (s.taille = ? OR s.taille IS NULL)
                WHERE p.id = ?
                ORDER BY CASE WHEN s.taille = ? THEN 0 ELSE 1 END
                LIMIT 1
            """, (taille, product_id, taille))
            prod_row = cursor.fetchone()
            if not prod_row:
                conn.rollback()
                return {"success": False, "error": "Produit ou taille introuvable"}

            article_nom = prod_row[0]
            prix_unitaire = Decimal(str(prod_row[1])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if prod_row[1] else Decimal('0.00')
            stock_id = prod_row[2]
            stock_dispo = int(prod_row[3]) if prod_row[3] else 0

            # Compter les claims déjà attribués (non annulés, non encaissés) pour ce produit/taille/session
            cursor.execute("""
                SELECT COUNT(*) FROM Live_Claims
                WHERE session_id=? AND product_id=? AND taille=?
                  AND statut_attribution NOT IN ('annulé', 'encaissé')
            """, (session_id, product_id, taille))
            existing_claims = cursor.fetchone()[0]

            rang_file = existing_claims + 1
            statut_attribution = 'attribué' if rang_file <= stock_dispo else 'file_attente'

            # Vérifier si cet acheteur a déjà une claim active pour ce produit/taille/session
            cursor.execute("""
                SELECT id FROM Live_Claims
                WHERE session_id=? AND buyer_id=? AND product_id=? AND taille=?
                  AND statut_attribution NOT IN ('annulé')
            """, (session_id, buyer_id, product_id, taille))
            existing = cursor.fetchone()
            if existing:
                conn.rollback()
                return {
                    "success": False,
                    "error": "Vous avez déjà une réservation pour cet article en cette taille."
                }

            cursor.execute("""
                INSERT INTO Live_Claims
                (session_id, buyer_id, client_id, product_id, stock_id, article_nom,
                 taille, prix_unitaire_tvac, quantite, statut_attribution, rang_file,
                 statut_paiement, statut_commande)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'non_paye', 'en_attente')
            """, (session_id, buyer_id, client_id, product_id, stock_id, article_nom,
                  taille, prix_unitaire, quantite, statut_attribution, rang_file))

            claim_id = cursor.lastrowid
            conn.commit()
            return {
                "success": True,
                "claim_id": claim_id,
                "rang_file": rang_file,
                "statut_attribution": statut_attribution,
                "article_nom": article_nom,
                "prix_unitaire": float(prix_unitaire),
                "stock_dispo": stock_dispo,
            }
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_claims(cls, session_id: int, filters: Dict[str, Any] = None, conn=None) -> List[Dict[str, Any]]:
        """Liste les claims d'une session avec filtres optionnels."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            filters = filters or {}

            query = """
                SELECT c.id, c.session_id, c.buyer_id, c.client_id, c.product_id,
                       c.article_nom, c.taille, c.prix_unitaire_tvac, c.quantite,
                       c.statut_attribution, c.rang_file, c.statut_paiement,
                       c.statut_commande, c.ticket_pos_id, c.created_at,
                       b.nom, b.prenom, b.telephone, b.email, b.pseudo_social,
                       b.mode_reception, b.adresse_rue, b.code_postal, b.ville,
                       b.pays, p.image_path
                FROM Live_Claims c
                INNER JOIN Live_Buyers b ON c.buyer_id = b.id
                INNER JOIN Produits p ON c.product_id = p.id
                WHERE c.session_id=?
            """
            params = [session_id]

            if filters.get("statut_attribution"):
                query += " AND c.statut_attribution=?"
                params.append(filters["statut_attribution"])
            if filters.get("statut_paiement"):
                query += " AND c.statut_paiement=?"
                params.append(filters["statut_paiement"])
            if filters.get("buyer_id"):
                query += " AND c.buyer_id=?"
                params.append(filters["buyer_id"])

            query += " ORDER BY c.created_at ASC"
            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [
                {
                    "id": r[0], "session_id": r[1], "buyer_id": r[2],
                    "client_id": r[3], "product_id": r[4],
                    "article_nom": r[5], "taille": r[6],
                    "prix_unitaire_tvac": float(r[7]) if r[7] else 0.0,
                    "quantite": r[8] or 1,
                    "statut_attribution": r[9], "rang_file": r[10],
                    "statut_paiement": r[11], "statut_commande": r[12],
                    "ticket_pos_id": r[13],
                    "created_at": str(r[14]) if r[14] else "",
                    "buyer_nom": r[15] or "", "buyer_prenom": r[16] or "",
                    "buyer_telephone": r[17] or "", "buyer_email": r[18] or "",
                    "buyer_pseudo": r[19] or "", "mode_reception": r[20] or "retrait_magasin",
                    "adresse_rue": r[21] or "", "code_postal": r[22] or "",
                    "ville": r[23] or "", "pays": r[24] or "France",
                    "image_path": r[25] or "",
                }
                for r in rows
            ]
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_buyer_claims(cls, buyer_id: int, session_id: int, conn=None) -> List[Dict[str, Any]]:
        """Liste les claims d'un acheteur spécifique pour une session."""
        return cls.get_claims(
            session_id,
            filters={"buyer_id": buyer_id},
            conn=conn
        )


    @classmethod
    def get_my_claims(cls, buyer_id: int, session_id: int, conn=None) -> List[Dict[str, Any]]:
        """Retourne uniquement les claims du buyer_id dans la session."""
        all_claims = cls.get_claims(session_id, conn=conn)
        return [c for c in all_claims if c["buyer_id"] == buyer_id]

    @classmethod
    def update_claim_status(cls, claim_id: int, updates: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Met à jour le statut d'une claim (paiement, attribution, commande).
        Recalcule automatiquement les rangs FIFO après annulation.
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()

            allowed_fields = {
                "statut_attribution": str,
                "statut_paiement": str,
                "statut_commande": str,
                "ticket_pos_id": int,
            }
            set_clauses = []
            params = []
            for field, cast in allowed_fields.items():
                if field in updates and updates[field] is not None:
                    set_clauses.append(f"{field}=?")
                    params.append(cast(updates[field]))

            if not set_clauses:
                return {"success": False, "error": "Aucun champ à mettre à jour"}

            params.append(claim_id)
            cursor.execute(
                f"UPDATE Live_Claims SET {', '.join(set_clauses)} WHERE id=?",
                params
            )

            # Si on annule une claim, recalculer les rangs pour ce produit/taille/session
            if updates.get("statut_attribution") == "annulé":
                cursor.execute("""
                    SELECT session_id, product_id, taille FROM Live_Claims WHERE id=?
                """, (claim_id,))
                ref = cursor.fetchone()
                if ref:
                    cls._recalculate_queue(ref[0], ref[1], ref[2], cursor)

            conn.commit()
            return {"success": True}
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            if should_close:
                conn.close()

    @classmethod
    def _recalculate_queue(cls, session_id: int, product_id: int, taille: str, cursor) -> None:
        """
        Recalcule les rangs FIFO et les statuts d'attribution après une annulation.
        Les claims non-annulées/non-encaissées sont renumérotées depuis 1.
        """
        # Récupérer le stock disponible
        cursor.execute("""
            SELECT COALESCE(s.quantite_actuelle, 0)
            FROM Produits p
            LEFT JOIN Stocks s ON s.id_produit = p.id AND (s.taille = ? OR s.taille IS NULL)
            WHERE p.id = ?
            ORDER BY CASE WHEN s.taille = ? THEN 0 ELSE 1 END
            LIMIT 1
        """, (taille, product_id, taille))
        stock_row = cursor.fetchone()
        stock_dispo = int(stock_row[0]) if stock_row else 0

        # Récupérer claims actives dans l'ordre chronologique
        cursor.execute("""
            SELECT id FROM Live_Claims
            WHERE session_id=? AND product_id=? AND taille=?
              AND statut_attribution NOT IN ('annulé', 'encaissé')
            ORDER BY created_at ASC
        """, (session_id, product_id, taille))
        active_claims = cursor.fetchall()

        for idx, (cid,) in enumerate(active_claims):
            rang = idx + 1
            statut = 'attribué' if rang <= stock_dispo else 'file_attente'
            cursor.execute("""
                UPDATE Live_Claims SET rang_file=?, statut_attribution=? WHERE id=?
            """, (rang, statut, cid))

    # -------------------------------------------------------------------------
    # 4. ENCAISSEMENT EN 1-CLIC DANS KŌDO POS
    # -------------------------------------------------------------------------

    @classmethod
    def checkout_claim(cls, claim_id: int, payment_data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Convertit une claim live en vente officielle dans Kōdo POS.
        Décrémente le stock SQLite et génère un ticket de caisse.
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()

            # Récupérer la claim avec toutes ses infos
            cursor.execute("""
                SELECT c.id, c.session_id, c.buyer_id, c.client_id, c.product_id,
                       c.stock_id, c.article_nom, c.taille, c.prix_unitaire_tvac,
                       c.quantite, c.statut_attribution, c.statut_commande,
                       b.nom, b.prenom, b.telephone
                FROM Live_Claims c
                INNER JOIN Live_Buyers b ON c.buyer_id = b.id
                WHERE c.id=?
            """, (claim_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": "Claim introuvable"}

            statut_attr = row[10]
            if statut_attr == "encaissé":
                return {"success": False, "error": "Cette commande a déjà été encaissée"}
            if statut_attr not in ("attribué",):
                return {"success": False, "error": "Seules les commandes attribuées peuvent être encaissées"}

            client_id = row[3]
            product_id = row[4]
            stock_id = row[5]
            article_nom = row[6]
            taille = row[7]
            prix_unitaire = float(row[8])
            quantite = int(row[9])
            buyer_nom = f"{row[13] or ''} {row[12]}".strip()

            mode_paiement = payment_data.get("paymentMethod", "CB")
            vendeur = payment_data.get("cashierName", "Live Admin")

            # Construire les items du cart pour le moteur de vente POS
            from kodo_core.domain.sales.cart_engine import process_sale_transaction
            cart_items = [{
                # Sans stock_id connu, la ligne de stock est retrouvée par (produit, taille) : l'id produit
                # n'est PAS un id de stock (cart_engine._resolve_stock_id).
                "product_id": product_id,
                "stock_id": stock_id or None,
                "code_barre": "",
                "nom": f"{article_nom} ({taille})" if taille and taille != "Taille Unique" else article_nom,
                "quantite": quantite,
                "prix_vente_tvac": prix_unitaire,
                "taux_tva": 0.21,
                "taille": taille,
            }]

            total_ttc = float(Decimal(str(prix_unitaire)) * quantite)

            result = process_sale_transaction(
                cart_items=cart_items,
                total_tvac=total_ttc,
                payments=[(mode_paiement, total_ttc)],
                client_id=client_id,
                cashier_name=vendeur,
                discount_percent=0.0,
                change_given=0.0,
                conn=conn
            )

            if result.get("success"):
                ticket_id = result.get("ticket_id")
                # Mettre à jour la claim
                cursor.execute("""
                    UPDATE Live_Claims
                    SET statut_attribution='encaissé', statut_commande='validé',
                        statut_paiement='paye', ticket_pos_id=?
                    WHERE id=?
                """, (ticket_id, claim_id))
                conn.commit()
                return {
                    "success": True,
                    "ticket_id": ticket_id,
                    "receipt_number": result.get("numero_ticket", ""),
                    "buyer_nom": buyer_nom,
                    "article_nom": article_nom,
                    "total_ttc": total_ttc,
                }
            else:
                return {"success": False, "error": result.get("error", "Erreur POS")}
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 5. CATALOGUE LIVE (Produits + Stocks pour l'espace acheteur)
    # -------------------------------------------------------------------------

    @classmethod
    def get_live_catalog(cls, session_id: Optional[int] = None, conn=None) -> List[Dict[str, Any]]:
        """
        Retourne le catalogue de produits avec stocks réels par taille,
        enrichi du nombre de claims actives pour chaque taille (calcul de disponibilité réelle).
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            # Vérifier si la session restreint les articles à une sélection spécifique
            product_filter_ids = None
            if session_id:
                cursor.execute("SELECT notes FROM Live_Sessions WHERE id=?", (session_id,))
                nrow = cursor.fetchone()
                if nrow and nrow[0]:
                    try:
                        n_data = json.loads(nrow[0])
                        if isinstance(n_data, dict) and n_data.get("product_ids"):
                            product_filter_ids = [int(pid) for pid in n_data["product_ids"]]
                    except Exception:
                        pass

            cursor.execute("""
                SELECT p.id, p.nom, p.categorie, p.prix_vente_tvac, p.en_solde,
                       p.prix_solde_tvac, p.image_path, p.marque
                FROM Produits p
                ORDER BY p.nom ASC
            """)
            products = cursor.fetchall()

            if product_filter_ids:
                # Filtrer et respecter l'ordre précis choisi par le vendeur
                id_to_order = {pid: idx for idx, pid in enumerate(product_filter_ids)}
                products = [p for p in products if p[0] in id_to_order]
                products.sort(key=lambda p: id_to_order.get(p[0], 9999))

            result = []
            for prod in products:
                prod_id = prod[0]
                prix = float(prod[3]) if prod[3] else 0.0
                prix_solde = float(prod[5]) if prod[5] else None

                # Stocks par taille
                cursor.execute("""
                    SELECT id, taille, quantite_actuelle FROM Stocks
                    WHERE id_produit=? ORDER BY taille ASC
                """, (prod_id,))
                stock_rows = cursor.fetchall()

                sizes = []
                for s in stock_rows:
                    taille = s[1] or "Taille Unique"
                    stock_reel = int(s[2]) if s[2] is not None else 0

                    # Claims actives (attribuées) pour cette taille dans la session
                    claims_attribuees = 0
                    if session_id:
                        cursor.execute("""
                            SELECT COUNT(*) FROM Live_Claims
                            WHERE session_id=? AND product_id=? AND taille=?
                              AND statut_attribution NOT IN ('annulé', 'encaissé')
                        """, (session_id, prod_id, taille))
                        claims_attribuees = cursor.fetchone()[0]

                    stock_live_dispo = max(0, stock_reel - claims_attribuees)

                    sizes.append({
                        "stock_id": s[0],
                        "taille": taille,
                        "stock_reel": stock_reel,
                        "claims_actives": claims_attribuees,
                        "stock_live_dispo": stock_live_dispo,
                        "disponible": stock_live_dispo > 0,
                    })

                if not sizes:
                    # Produit sans entrées de stock
                    cursor.execute("""
                        SELECT COALESCE(SUM(quantite_actuelle), 0) FROM Stocks WHERE id_produit=?
                    """, (prod_id,))
                    total_stock = int(cursor.fetchone()[0])
                    sizes.append({
                        "stock_id": None,
                        "taille": "Taille Unique",
                        "stock_reel": total_stock,
                        "claims_actives": 0,
                        "stock_live_dispo": total_stock,
                        "disponible": total_stock > 0,
                    })

                result.append({
                    "id": prod_id,
                    "nom": prod[1],
                    "categorie": prod[2] or "Général",
                    "prix": prix,
                    "prix_solde": prix_solde,
                    "en_solde": bool(prod[4]),
                    "image_path": prod[6] or "",
                    "marque": prod[7] or "",
                    "sizes": sizes,
                    "stock_total": sum(s["stock_reel"] for s in sizes),
                    "disponible": any(s["disponible"] for s in sizes),
                })

            return result
        finally:
            if should_close:
                conn.close()

    # -------------------------------------------------------------------------
    # 6. GÉNÉRATION MESSAGE RÉCAPITULATIF
    # -------------------------------------------------------------------------

    @classmethod
    def generate_summary_message(cls, buyer_id: int, session_id: int, conn=None) -> str:
        """
        Génère un message récapitulatif pour l'acheteur (Instagram DM / WhatsApp / SMS).
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT nom, prenom, pseudo_social, mode_reception, adresse_rue,
                       code_postal, ville, telephone
                FROM Live_Buyers WHERE id=?
            """, (buyer_id,))
            buyer = cursor.fetchone()
            if not buyer:
                return ""

            nom_complet = f"{buyer[1] or ''} {buyer[0]}".strip()
            pseudo = buyer[2] or ""
            mode = buyer[3] or "retrait_magasin"
            adresse = f"{buyer[4] or ''}, {buyer[5] or ''} {buyer[6] or ''}".strip(", ")
            telephone = buyer[7] or ""

            claims = cls.get_my_claims(buyer_id, session_id, conn=conn)
            if not claims:
                return ""

            lignes = []
            total = 0.0
            for claim in claims:
                emoji = "✅" if claim["statut_attribution"] == "attribué" else "⏳"
                taille_str = f" (Taille {claim['taille']})" if claim["taille"] and claim["taille"] != "Taille Unique" else ""
                prix_ligne = claim["prix_unitaire_tvac"] * claim["quantite"]
                total += prix_ligne
                rang = f" — Rang {claim['rang_file']}" if claim["statut_attribution"] == "file_attente" else ""
                paiement = {
                    "paye": " — ✅ Payé",
                    "non_paye": " — ⚠️ Paiement en attente",
                    "sur_place": " — 🏪 Retrait boutique (sur place)",
                    "virement": " — 🏦 Virement en attente"
                }.get(claim["statut_paiement"], "")
                lignes.append(
                    f"  {emoji} {claim['article_nom']}{taille_str} — {prix_ligne:.2f}€{rang}{paiement}"
                )

            mode_str = (
                "📦 Livraison à domicile" if mode == "livraison" else "🏪 Retrait en boutique"
            )
            adresse_str = f"\n📍 Adresse : {adresse}" if mode == "livraison" and adresse else ""

            prenom_display = buyer[1] or nom_complet.split()[0] if nom_complet else "Cher(e) client(e)"

            # Ajout des coordonnées bancaires si au moins un article est en attente de virement
            virement_info = ""
            has_virement = any(c.get("statut_paiement") == "virement" for c in claims)
            if has_virement:
                cursor.execute("SELECT valeur FROM Parametres WHERE cle='shop_iban'")
                iban_row = cursor.fetchone()
                shop_iban = iban_row[0] if iban_row and iban_row[0] else "BE68 0000 0000 0000"
                virement_ref = f"LIVE-{prenom_display.upper()}-{buyer_id}"
                virement_info = f"\n\n🏦 Coordonnées pour le virement bancaire :\n• IBAN : {shop_iban}\n• Communication : {virement_ref}\n• Montant : {total:.2f}€"

            message = (
                f"Bonjour {prenom_display} {'(' + pseudo + ') ' if pseudo else ''}👋\n\n"
                f"Voici le récapitulatif de tes réservations du Live 🛍️ :\n\n"
                + "\n".join(lignes) +
                f"\n\n💰 Total : {total:.2f}€\n"
                f"{mode_str}{adresse_str}"
                f"{virement_info}\n\n"
                f"Merci pour ta confiance ! 🙏\n"
                f"À très vite ! 💫"
            )
            return message
        finally:
            if should_close:
                conn.close()
