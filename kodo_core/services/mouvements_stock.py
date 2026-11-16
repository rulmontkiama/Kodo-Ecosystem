# -*- coding: utf-8 -*-
"""
Mouvements de stock justifiés — Kōdo POS.

Ce que fait ce module
---------------------
1. ENREGISTRER un mouvement de stock avec un MOTIF : réception fournisseur, casse, vol, retour
   fournisseur, démarque, don, transfert, inventaire… La quantité de `Stocks` est modifiée et la
   trace (qui, quand, pourquoi, avant → après) est écrite dans la MÊME transaction, dans
   `Stock_Journal` (voir `kodo_core.db.stock_journal`).
2. LISTER l'historique : les mouvements saisis + les modifications faites depuis la fiche produit
   + les imports Shopify (tous dans `Stock_Journal`), et les VENTES / RETOURS relus à la volée
   depuis `Ventes_Details`. Les ventes ne sont jamais recopiées dans le journal : leurs tables
   (signées, chaînées) restent la seule source, et ce module n'écrit pas dans le circuit de vente.

Les motifs s'inspirent de ceux des logiciels de caisse et de gestion de stock courants
(Lightspeed, Odoo, Zoho Inventory, Shopify, SumUp…) : l'ensemble couvre ce qu'un commerçant doit
pouvoir justifier à son comptable (casse, vol, démarque inconnue, dons) et à ses fournisseurs.

Sens des mouvements
-------------------
- `entree`     : le stock augmente de `quantite`.
- `sortie`     : le stock diminue de `quantite` ; refusé s'il passerait sous zéro (règle de la
                 maison : un stock n'est jamais négatif).
- `recomptage` : `quantite` est le stock COMPTÉ ; le mouvement enregistré est l'écart. Un
                 recomptage solde aussi le drapeau « à auditer » posé par une survente.
"""
import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from kodo_core.db import shopify_alignement, stock_journal
from kodo_core.db.connection import get_connection
from kodo_core.domain.catalog.inventory_manager import _LIBELLES_TAILLE_UNIQUE, _norm_taille

logger = logging.getLogger("kodo_core.services.mouvements_stock")

SENS_ENTREE = "entree"
SENS_SORTIE = "sortie"
SENS_RECOMPTAGE = "recomptage"
SENS_VALIDES = (SENS_ENTREE, SENS_SORTIE, SENS_RECOMPTAGE)

# code -> (libellé, sens autorisé, note obligatoire)
MOTIFS: Dict[str, Dict[str, Any]] = {
    # --- Entrées ---
    "reception_fournisseur": {"libelle": "Réception fournisseur / réassort", "sens": SENS_ENTREE, "note": False},
    "retour_client_stock": {"libelle": "Retour client remis en stock", "sens": SENS_ENTREE, "note": False},
    "transfert_entrant": {"libelle": "Transfert entrant (autre boutique / dépôt)", "sens": SENS_ENTREE, "note": False},
    "article_retrouve": {"libelle": "Article retrouvé", "sens": SENS_ENTREE, "note": False},
    "stock_initial": {"libelle": "Stock initial / mise en place", "sens": SENS_ENTREE, "note": False},
    "correction_plus": {"libelle": "Correction d'une erreur de saisie (+)", "sens": SENS_ENTREE, "note": True},
    "autre_entree": {"libelle": "Autre entrée", "sens": SENS_ENTREE, "note": True},
    # --- Sorties ---
    "casse": {"libelle": "Casse / article abîmé", "sens": SENS_SORTIE, "note": False},
    "defectueux": {"libelle": "Défectueux / non vendable", "sens": SENS_SORTIE, "note": False},
    "perte": {"libelle": "Perte / démarque inconnue", "sens": SENS_SORTIE, "note": False},
    "vol": {"libelle": "Vol", "sens": SENS_SORTIE, "note": False},
    "retour_fournisseur": {"libelle": "Retour au fournisseur", "sens": SENS_SORTIE, "note": False},
    "transfert_sortant": {"libelle": "Transfert sortant (autre boutique / dépôt)", "sens": SENS_SORTIE, "note": False},
    "usage_interne": {"libelle": "Usage interne / vitrine / photos", "sens": SENS_SORTIE, "note": False},
    "don_marketing": {"libelle": "Don / cadeau / échantillon marketing", "sens": SENS_SORTIE, "note": False},
    "peremption": {"libelle": "Péremption / date dépassée", "sens": SENS_SORTIE, "note": False},
    "correction_moins": {"libelle": "Correction d'une erreur de saisie (−)", "sens": SENS_SORTIE, "note": True},
    "autre_sortie": {"libelle": "Autre sortie", "sens": SENS_SORTIE, "note": True},
    # --- Recomptage ---
    "inventaire": {"libelle": "Inventaire / recomptage physique", "sens": SENS_RECOMPTAGE, "note": False},
}

# Libellés des lignes qui n'ont pas de motif saisi (anciennes traces et mouvements automatiques).
LIBELLES_ORIGINES = {
    stock_journal.ORIGINE_FICHE_PRODUIT: "Modification depuis la fiche produit",
    stock_journal.ORIGINE_IMPORT_SHOPIFY: "Import du catalogue Shopify",
}
MOTIF_VENTE = "vente"
MOTIF_RETOUR_VENTE = "retour_vente"
LIBELLE_VENTE = "Vente"
LIBELLE_RETOUR_VENTE = "Retour / remboursement client"


class MouvementError(Exception):
    """Refus métier d'un mouvement. `code` = code HTTP à renvoyer, `message` affichable tel quel."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def liste_motifs() -> List[Dict[str, Any]]:
    """Motifs proposés à l'écran, dans l'ordre de déclaration."""
    return [{"code": c, "libelle": m["libelle"], "sens": m["sens"], "note_obligatoire": m["note"]}
            for c, m in MOTIFS.items()]


def _entier(valeur) -> int:
    """Quantité saisie : un entier, virgule ou point acceptés tant qu'il n'y a pas de décimale."""
    try:
        if isinstance(valeur, bool):
            raise ValueError
        nombre = float(str(valeur).strip().replace(",", "."))
        if nombre != int(nombre):
            raise ValueError
        return int(nombre)
    except (TypeError, ValueError, OverflowError):
        raise MouvementError("Quantité invalide : saisissez un nombre entier.")


def _resoudre_ligne_stock(cursor, product_id, taille, stock_id) -> Tuple[int, int, str, Optional[str], int]:
    """Retourne (stock_id, produit_id, nom du produit, taille, quantité actuelle)."""
    if stock_id is not None:
        cursor.execute(
            "SELECT s.id, s.id_produit, p.nom, s.taille, s.quantite_actuelle "
            "FROM Stocks s LEFT JOIN Produits p ON p.id = s.id_produit WHERE s.id = ?", (stock_id,))
        ligne = cursor.fetchone()
        if not ligne:
            raise MouvementError("Cette ligne de stock n'existe pas.", 404)
        return ligne[0], ligne[1], ligne[2], ligne[3], int(ligne[4] or 0)

    if product_id is None:
        raise MouvementError("Précisez l'article concerné.")
    cursor.execute("SELECT nom FROM Produits WHERE id = ?", (product_id,))
    produit = cursor.fetchone()
    if not produit:
        raise MouvementError("Cet article n'existe pas.", 404)
    cursor.execute("SELECT id, taille, quantite_actuelle FROM Stocks WHERE id_produit = ? ORDER BY id", (product_id,))
    lignes = cursor.fetchall()
    if not lignes:
        raise MouvementError("Cet article n'a aucune ligne de stock : enregistrez d'abord ses quantités sur sa fiche.")

    cle = _norm_taille(taille)
    trouve = next((l for l in lignes if _norm_taille(l[1]) == cle), None)
    if trouve is None and cle in _LIBELLES_TAILLE_UNIQUE:
        trouve = next((l for l in lignes if _norm_taille(l[1]) in _LIBELLES_TAILLE_UNIQUE), None)
    if trouve is None:
        if taille in (None, "") and len(lignes) == 1:
            trouve = lignes[0]
        elif taille in (None, ""):
            raise MouvementError("Cet article a plusieurs tailles : précisez laquelle.")
        else:
            raise MouvementError(f"La taille « {taille} » n'existe pas pour cet article.", 404)
    return trouve[0], product_id, produit[0], trouve[1], int(trouve[2] or 0)


def enregistrer_mouvement(data: Dict[str, Any], conn=None) -> Dict[str, Any]:
    """Applique un mouvement et le journalise. Lève `MouvementError` en cas de refus métier."""
    sens = str(data.get("sens") or "").strip().lower()
    if sens not in SENS_VALIDES:
        raise MouvementError("Sens du mouvement invalide (entrée, sortie ou recomptage).")
    motif = str(data.get("motif") or "").strip()
    definition = MOTIFS.get(motif)
    if definition is None:
        raise MouvementError("Choisissez un motif pour ce mouvement.")
    if definition["sens"] != sens:
        raise MouvementError("Ce motif ne correspond pas au sens du mouvement.")
    note = str(data.get("note") or "").strip()
    if definition["note"] and len(note) < 3:
        raise MouvementError("Ce motif demande une précision (champ « Note »).")

    quantite = _entier(data.get("quantite"))
    if quantite < 0 or (sens != SENS_RECOMPTAGE and quantite == 0):
        raise MouvementError("La quantité doit être supérieure à zéro." if sens != SENS_RECOMPTAGE
                             else "Le stock compté ne peut pas être négatif.")

    # L'interface envoie l'identifiant sous forme de texte : on le ramène à un entier.
    for cle in ("product_id", "stock_id"):
        brut = data.get(cle)
        if isinstance(brut, str) and brut.strip().isdigit():
            data[cle] = int(brut.strip())

    utilisateur = str(data.get("utilisateur") or "").strip() or None
    reference = str(data.get("reference") or "").strip() or None

    doit_fermer = False
    if conn is None:
        conn = get_connection()
        doit_fermer = True
    try:
        cursor = conn.cursor()
        sid, produit_id, nom, taille_ligne, avant = _resoudre_ligne_stock(
            cursor, data.get("product_id"), data.get("taille"), data.get("stock_id"))

        if sens == SENS_ENTREE:
            apres = avant + quantite
        elif sens == SENS_SORTIE:
            if quantite > avant:
                raise MouvementError(
                    f"Stock insuffisant : {avant} en stock, {quantite} demandé(s). "
                    "Un stock ne peut pas passer sous zéro.", 409)
            apres = avant - quantite
        else:
            apres = quantite

        # `AND quantite_actuelle = ?` : si la quantité a bougé entre la lecture et l'écriture
        # (vente en cours sur une autre caisse), on refuse plutôt que d'écraser le mouvement.
        if sens == SENS_RECOMPTAGE:
            cursor.execute(
                "UPDATE Stocks SET quantite_actuelle = ?, requires_stock_audit = 0 "
                "WHERE id = ? AND quantite_actuelle = ?", (apres, sid, avant))
        else:
            cursor.execute(
                "UPDATE Stocks SET quantite_actuelle = ? WHERE id = ? AND quantite_actuelle = ?",
                (apres, sid, avant))
        if cursor.rowcount == 0:
            raise MouvementError("Le stock vient de changer (une vente a eu lieu ?). Réessayez.", 409)
        if sens == SENS_RECOMPTAGE:
            cursor.execute(
                "UPDATE Produits SET requires_stock_audit = "
                "COALESCE((SELECT MAX(requires_stock_audit) FROM Stocks WHERE id_produit = ?), 0) WHERE id = ?",
                (produit_id, produit_id))

        stock_journal.noter(
            cursor, sid, avant, apres, stock_journal.ORIGINE_MOUVEMENT,
            produit_id=produit_id, produit=nom, taille=taille_ligne, detail=note,
            motif=motif, utilisateur=utilisateur, reference=reference,
            forcer=(sens == SENS_RECOMPTAGE))
        if apres != avant:
            # Même règle que la fiche produit : la boutique en ligne rattrape la quantité à la
            # prochaine passe (jamais bloquant, sans effet si Shopify n'est pas relié).
            shopify_alignement.marquer(cursor, sid, produit_id=produit_id, produit=nom, taille=taille_ligne)

        cursor.execute("SELECT COALESCE(SUM(quantite_actuelle), 0) FROM Stocks WHERE id_produit = ?", (produit_id,))
        total_produit = int(cursor.fetchone()[0] or 0)
        conn.commit()
        return {"success": True, "stock_id": sid, "produit": nom, "taille": taille_ligne,
                "avant": avant, "apres": apres, "delta": apres - avant,
                "stock_total_produit": total_produit, "motif": motif}
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        if doit_fermer:
            conn.close()


# ---------------------------------------------------------------------------------------------
# Historique
# ---------------------------------------------------------------------------------------------

def _table_existe(cursor, nom: str) -> bool:
    cursor.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (nom,))
    return cursor.fetchone() is not None


def _colonnes(cursor, table: str) -> set:
    cursor.execute(f"PRAGMA table_info({table})")
    return {ligne[1] for ligne in cursor.fetchall()}


def _libelle_motif(code: Optional[str], origine: Optional[str], avant=None, apres=None) -> str:
    if code is None and origine == stock_journal.ORIGINE_FICHE_PRODUIT:
        if avant is None:
            return "Création de la taille sur la fiche produit"
        if apres is None:
            return "Taille retirée de la fiche produit"
    if code in MOTIFS:
        return MOTIFS[code]["libelle"]
    if code == MOTIF_VENTE:
        return LIBELLE_VENTE
    if code == MOTIF_RETOUR_VENTE:
        return LIBELLE_RETOUR_VENTE
    return LIBELLES_ORIGINES.get(origine or "", "Mouvement de stock")


def lister_mouvements(filtres: Dict[str, Any], conn=None) -> Dict[str, Any]:
    """Historique filtré (plus récent d'abord) + totaux sur l'ensemble filtré.

    Filtres : `q` (nom d'article ou référence), `product_id`, `motif`, `sens` (`entree`/`sortie`),
    `du` / `au` (AAAA-MM-JJ, inclus), `source` (`saisie` = journal, `vente` = ventes et retours),
    `limit` (≤ 5000), `offset`.
    """
    doit_fermer = False
    if conn is None:
        conn = get_connection()
        doit_fermer = True
    try:
        cursor = conn.cursor()
        blocs: List[str] = []
        params: List[Any] = []
        source = (filtres.get("source") or "").strip().lower()

        if source != "vente" and _table_existe(cursor, stock_journal.TABLE):
            colonnes = _colonnes(cursor, stock_journal.TABLE)
            col_delta = "j.delta" if "delta" in colonnes else "NULL"
            col_motif = "j.motif" if "motif" in colonnes else "NULL"
            col_user = "j.utilisateur" if "utilisateur" in colonnes else "NULL"
            col_ref = "j.reference" if "reference" in colonnes else "NULL"
            blocs.append(
                "SELECT 'J' || j.id AS uid, j.date_heure AS date_heure, j.produit_id AS produit_id, "
                "COALESCE(j.produit, p.nom, '(article supprimé)') AS produit, j.taille AS taille, "
                f"COALESCE({col_delta}, COALESCE(j.apres, 0) - COALESCE(j.avant, 0)) AS delta, "
                "j.avant AS avant, j.apres AS apres, j.origine AS origine, "
                f"{col_motif} AS motif, {col_user} AS utilisateur, {col_ref} AS reference, "
                "COALESCE(j.detail, '') AS note "
                f"FROM {stock_journal.TABLE} j LEFT JOIN Produits p ON p.id = j.produit_id")

        if source != "saisie" and _table_existe(cursor, "Ventes_Details"):
            blocs.append(
                "SELECT 'V' || vd.id AS uid, t.date_heure AS date_heure, s.id_produit AS produit_id, "
                "COALESCE(p.nom, '(article supprimé)') AS produit, s.taille AS taille, "
                "-vd.quantite AS delta, NULL AS avant, NULL AS apres, "
                f"CASE WHEN vd.quantite < 0 THEN '{MOTIF_RETOUR_VENTE}' ELSE '{MOTIF_VENTE}' END AS origine, "
                f"CASE WHEN vd.quantite < 0 THEN '{MOTIF_RETOUR_VENTE}' ELSE '{MOTIF_VENTE}' END AS motif, "
                "t.vendeur_nom AS utilisateur, t.numero_ticket AS reference, '' AS note "
                "FROM Ventes_Details vd JOIN Tickets t ON t.id = vd.id_ticket "
                "JOIN Stocks s ON s.id = vd.id_stock LEFT JOIN Produits p ON p.id = s.id_produit "
                "WHERE vd.id_stock IS NOT NULL")

        if not blocs:
            return {"mouvements": [], "totaux": {"nombre": 0, "entrees": 0, "sorties": 0}, "total": 0}

        union = " UNION ALL ".join(blocs)
        conditions: List[str] = []
        q = str(filtres.get("q") or "").strip()
        if q:
            conditions.append("(produit LIKE ? OR reference LIKE ? OR note LIKE ?)")
            params += [f"%{q}%"] * 3
        if filtres.get("product_id") not in (None, ""):
            conditions.append("produit_id = ?")
            params.append(int(filtres["product_id"]))
        motif = str(filtres.get("motif") or "").strip()
        if motif:
            conditions.append("(motif = ? OR (motif IS NULL AND origine = ?))")
            params += [motif, motif]
        sens = str(filtres.get("sens") or "").strip().lower()
        if sens == SENS_ENTREE:
            conditions.append("delta > 0")
        elif sens == SENS_SORTIE:
            conditions.append("delta < 0")
        du = str(filtres.get("du") or "").strip()
        au = str(filtres.get("au") or "").strip()
        try:
            if du:
                conditions.append("date_heure >= ?")
                params.append(datetime.datetime.strptime(du[:10], "%Y-%m-%d").strftime("%Y-%m-%d"))
            if au:
                conditions.append("date_heure < ?")
                params.append((datetime.datetime.strptime(au[:10], "%Y-%m-%d")
                               + datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
        except ValueError:
            raise MouvementError("Date invalide : utilisez le format AAAA-MM-JJ.")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0), "
            f"COALESCE(SUM(CASE WHEN delta < 0 THEN -delta ELSE 0 END), 0) FROM ({union}){where}", params)
        nombre, entrees, sorties = cursor.fetchone()

        try:
            limite = max(1, min(int(filtres.get("limit") or 200), 5000))
            decalage = max(0, int(filtres.get("offset") or 0))
        except (TypeError, ValueError):
            limite, decalage = 200, 0
        cursor.execute(
            f"SELECT uid, date_heure, produit_id, produit, taille, delta, avant, apres, origine, motif, "
            f"utilisateur, reference, note FROM ({union}){where} "
            "ORDER BY date_heure DESC, uid DESC LIMIT ? OFFSET ?", params + [limite, decalage])
        mouvements = []
        for r in cursor.fetchall():
            code = r[9] or (r[8] if r[8] in (MOTIF_VENTE, MOTIF_RETOUR_VENTE) else None)
            mouvements.append({
                "id": r[0], "date": r[1], "produit_id": r[2], "produit": r[3], "taille": r[4],
                "delta": int(r[5] or 0), "avant": r[6], "apres": r[7],
                "motif": code or r[8], "motif_libelle": _libelle_motif(code, r[8], r[6], r[7]),
                "utilisateur": r[10], "reference": r[11], "note": r[12],
                "automatique": code in (MOTIF_VENTE, MOTIF_RETOUR_VENTE),
            })
        return {"mouvements": mouvements, "total": nombre,
                "totaux": {"nombre": nombre, "entrees": int(entrees), "sorties": int(sorties)}}
    finally:
        if doit_fermer:
            conn.close()
