# -*- coding: utf-8 -*-
"""
Pont Live Shopping (échange de fichiers JSON) - Kōdo POS Core

Fait le lien avec l'application web autonome « Live Shopping » (hébergée à part) :
  - export du stock mis à disposition pour un live  (POS -> app Live Shopping)
  - import des ventes réalisées pendant le live      (app Live Shopping -> POS)

Aucune connexion réseau : tout passe par des fichiers JSON dont le format est décrit
dans le contrat partagé (session_reference / exported_at / articles | ventes).

Principes :
  - l'export est une photo : il ne modifie JAMAIS le stock réel ;
  - l'import réutilise `process_sale_transaction` (mêmes tables, même scellement NF525,
    même contrôle anti-survente qu'une vente en caisse) : aucun circuit parallèle ;
  - un même fichier (ou une même ligne de vente) ne peut jamais être importé deux fois ;
  - tous les calculs de montants utilisent `decimal.Decimal` (ROUND_HALF_UP, 2 décimales).
"""

import datetime
import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from kodo_core.db.connection import get_connection
from kodo_core.domain.sales.cart_engine import quantize_money, process_sale_transaction

MAX_LINES_PER_FILE = 5000
_SESSION_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,99}$")

# Contrat -> libellé de moyen de paiement POS (voir classification dans generer_bilan_z_journalier)
_PAYMENT_LABELS = {
    "especes": "Espèces",
    "espece": "Espèces",
    "cash": "Espèces",
    "carte": "CB",
    "cb": "CB",
    "carte_bancaire": "CB",
    "virement": "Virement",
    "mobile_money": "Mobile Money",
}

_UNIQUE_SIZES = {"", "TAILLE-UNIQUE", "UNIQUE", "TU"}


class LiveBridgeError(Exception):
    """Erreur métier/validation renvoyée telle quelle à l'utilisateur."""

    def __init__(self, message: str, details: Optional[List[str]] = None):
        super().__init__(message)
        self.details = details or []


# =============================================================================
# Helpers
# =============================================================================

def _strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn")


def _slug(value: Any) -> str:
    text = _strip_accents(str(value or "")).upper()
    return re.sub(r"[^A-Z0-9]+", "-", text).strip("-")


def _norm_key(value: Any) -> str:
    text = _strip_accents(str(value or "")).strip().lower()
    return re.sub(r"[\s\-]+", "_", text)


def normalize_phone(phone: Any) -> str:
    """Clé de rapprochement téléphone : chiffres seuls, 9 derniers si assez long
    (+33 6 12 34 56 78 et 06 12 34 56 78 désignent la même personne)."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 6:
        return ""
    return digits[-9:] if len(digits) >= 9 else digits


def payment_label(value: Any) -> Optional[str]:
    return _PAYMENT_LABELS.get(_norm_key(value)) if value not in (None, "") else None


def _dec(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation("bool")
    result = Decimal(str(value).strip())
    if not result.is_finite():
        raise InvalidOperation("non fini")
    return result


def _variante_label(taille: Any) -> str:
    t = str(taille or "").strip()
    if _slug(t) in _UNIQUE_SIZES:
        return "Taille Unique"
    return t if t.lower().startswith("taille") else f"Taille {t}"


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


class LiveBridge:
    """Export du stock live et import des ventes live (fichiers JSON)."""

    # -------------------------------------------------------------------------
    # 0. SCHÉMA
    # -------------------------------------------------------------------------

    @classmethod
    def ensure_schema(cls, conn) -> None:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Live_Bridge_Exports (
                session_reference TEXT PRIMARY KEY,
                exported_at TEXT NOT NULL,
                boutique TEXT,
                nb_articles INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Live_Bridge_Skus (
                session_reference TEXT NOT NULL,
                sku TEXT NOT NULL,
                stock_id INTEGER NOT NULL,
                quantite_exportee INTEGER NOT NULL,
                PRIMARY KEY (session_reference, sku)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Live_Bridge_Imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_reference TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                file_exported_at TEXT,
                imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
                cashier_name TEXT,
                nb_lines INTEGER DEFAULT 0,
                nb_imported INTEGER DEFAULT 0,
                nb_blocked INTEGER DEFAULT 0,
                total_tvac TEXT DEFAULT '0.00'
            )
        """)
        # `fingerprint` UNIQUE = garde anti double import au niveau de la LIGNE : un export
        # partiel puis un export complet de la même session ne recompteront jamais deux fois
        # la même vente.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Live_Bridge_Import_Lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                session_reference TEXT NOT NULL,
                import_id INTEGER NOT NULL,
                ticket_id INTEGER,
                sku TEXT,
                quantite INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS Live_Bridge_Orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER NOT NULL,
                session_reference TEXT NOT NULL,
                ticket_id INTEGER,
                numero_ticket TEXT,
                client_id INTEGER,
                client_nom TEXT,
                telephone TEXT,
                email TEXT,
                mode_recuperation TEXT,
                adresse TEXT,
                mode_paiement TEXT,
                reference_paiement TEXT,
                date_commande TEXT,
                total_tvac TEXT,
                lignes_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # `Clients` n'a pas de téléphone dans le schéma de base : indispensable au
        # rapprochement client et au bordereau de livraison.
        cur.execute("PRAGMA table_info(Clients)")
        cols = {row[1] for row in cur.fetchall()}
        for col in ("telephone", "adresse"):
            if col not in cols:
                cur.execute(f"ALTER TABLE Clients ADD COLUMN {col} TEXT")
        conn.commit()

    # -------------------------------------------------------------------------
    # 1. STOCK & SKU
    # -------------------------------------------------------------------------

    @classmethod
    def _load_stock(cls, cursor) -> List[Dict[str, Any]]:
        """Toutes les déclinaisons du stock avec un SKU déterministe et unique."""
        cursor.execute("""
            SELECT s.id, s.taille, s.quantite_actuelle, p.id, p.code_barre, p.nom,
                   p.categorie, p.prix_vente_tvac, p.prix_solde_tvac, p.en_solde, p.type_vente
            FROM Stocks s JOIN Produits p ON p.id = s.id_produit
            ORDER BY s.id
        """)
        rows, seen = [], set()
        for r in cursor.fetchall():
            base = _slug(r[4]) or f"P{r[3]}"
            size_slug = _slug(r[1])
            sku = base if size_slug in _UNIQUE_SIZES else f"{base}-{size_slug}"
            if sku in seen:
                sku = f"{sku}-{r[0]}"
            seen.add(sku)
            prix = r[8] if (r[9] and r[8] is not None) else r[7]
            rows.append({
                "stock_id": r[0], "taille": r[1], "quantite": int(r[2] or 0),
                "produit_id": r[3], "code_barre": r[4], "nom": r[5], "categorie": r[6],
                "prix": quantize_money(Decimal(str(prix or 0))),
                "type_vente": r[10] or "unite",
                "sku": sku, "variante": _variante_label(r[1]),
            })
        return rows

    @classmethod
    def list_exportable_stock(cls, conn=None) -> List[Dict[str, Any]]:
        """Déclinaisons vendables en live (en stock, vendues à l'unité), pour l'écran d'export."""
        own = conn is None
        conn = conn or get_connection()
        try:
            cls.ensure_schema(conn)
            items = [
                {
                    "stock_id": s["stock_id"], "produit_id": s["produit_id"], "sku": s["sku"],
                    "nom": s["nom"], "categorie": s["categorie"] or "", "variante": s["variante"],
                    "prix": float(s["prix"]), "quantite_reelle": s["quantite"],
                }
                for s in cls._load_stock(conn.cursor())
                if s["quantite"] > 0 and s["type_vente"] == "unite"
            ]
            return items
        finally:
            if own:
                conn.close()

    # -------------------------------------------------------------------------
    # 2. EXPORT DU STOCK (POS -> Live Shopping)
    # -------------------------------------------------------------------------

    @classmethod
    def export_stock(cls, items: Any, session_reference: Optional[str] = None,
                     boutique: Optional[str] = None, conn=None) -> Dict[str, Any]:
        """Construit le fichier de stock du live. Ne modifie PAS le stock réel."""
        if not isinstance(items, list) or not items:
            raise LiveBridgeError("Sélectionnez au moins un article à exporter.")

        ref = (session_reference or "").strip() or datetime.datetime.now().strftime("live-%Y-%m-%d-%H%M")
        if not _SESSION_REF_RE.match(ref):
            raise LiveBridgeError(
                "Référence de session invalide : 3 à 100 caractères (lettres, chiffres, . _ : -), "
                "sans espace."
            )

        own = conn is None
        conn = conn or get_connection()
        try:
            cls.ensure_schema(conn)
            cur = conn.cursor()
            stock = {s["stock_id"]: s for s in cls._load_stock(cur)}

            errors, chosen, seen_ids = [], [], set()
            for i, it in enumerate(items, 1):
                if not isinstance(it, dict):
                    errors.append(f"Ligne {i} : format invalide.")
                    continue
                try:
                    sid = int(it.get("stock_id"))
                    qty = int(it.get("quantite"))
                except (TypeError, ValueError):
                    errors.append(f"Ligne {i} : stock_id et quantite doivent être des entiers.")
                    continue
                if sid in seen_ids:
                    errors.append(f"Ligne {i} : déclinaison {sid} sélectionnée plusieurs fois.")
                    continue
                seen_ids.add(sid)
                s = stock.get(sid)
                if not s:
                    errors.append(f"Ligne {i} : déclinaison {sid} introuvable en stock.")
                    continue
                label = f"{s['nom']} ({s['variante']})"
                if qty <= 0:
                    errors.append(f"{label} : la quantité doit être supérieure à 0.")
                elif qty > s["quantite"]:
                    errors.append(f"{label} : {qty} demandé(s) pour {s['quantite']} en stock.")
                else:
                    chosen.append((s, qty))
            if errors:
                raise LiveBridgeError("Export impossible : la sélection contient des erreurs.", errors)

            if not boutique:
                cur.execute("SELECT valeur FROM Parametres WHERE cle='shop_name'")
                row = cur.fetchone()
                boutique = row[0] if row and row[0] else "Kōdo POS"

            exported_at = _utc_now_iso()
            payload = {
                "session_reference": ref,
                "exported_at": exported_at,
                "boutique": boutique,
                "articles": [
                    {
                        "sku": s["sku"], "nom": s["nom"], "variante": s["variante"],
                        "prix": float(s["prix"]), "quantite_disponible": qty,
                        # image_path est un chemin local, inutilisable par l'app web
                        "image_url": None,
                    }
                    for s, qty in chosen
                ],
            }

            # Mémorise sku -> déclinaison : l'import retrouve ainsi la bonne ligne de stock
            # même si un code-barres est modifié entre l'export et l'import.
            cur.execute("DELETE FROM Live_Bridge_Skus WHERE session_reference=?", (ref,))
            cur.executemany(
                "INSERT INTO Live_Bridge_Skus (session_reference, sku, stock_id, quantite_exportee) VALUES (?,?,?,?)",
                [(ref, s["sku"], s["stock_id"], qty) for s, qty in chosen],
            )
            cur.execute(
                "INSERT OR REPLACE INTO Live_Bridge_Exports (session_reference, exported_at, boutique, nb_articles) "
                "VALUES (?,?,?,?)", (ref, exported_at, boutique, len(chosen)),
            )
            conn.commit()
            return {"filename": f"{ref}-stock.json", "payload": payload}
        except Exception:
            conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    # -------------------------------------------------------------------------
    # 3. LECTURE DÉFENSIVE DU FICHIER DE VENTES
    # -------------------------------------------------------------------------

    @classmethod
    def _parse_file(cls, content: Any) -> Tuple[Dict[str, Any], str]:
        """Parse + valide la structure globale. Lève LiveBridgeError si inexploitable."""
        if isinstance(content, (bytes, bytearray)):
            try:
                content = bytes(content).decode("utf-8-sig")
            except UnicodeDecodeError:
                raise LiveBridgeError("Le fichier n'est pas encodé en UTF-8.")
        if isinstance(content, str):
            if not content.strip():
                raise LiveBridgeError("Le fichier est vide.")
            try:
                payload = json.loads(content.lstrip("﻿"))
            except json.JSONDecodeError as e:
                raise LiveBridgeError(
                    f"Fichier JSON invalide (ligne {e.lineno}, colonne {e.colno}) : {e.msg}."
                )
        else:
            payload = content

        if not isinstance(payload, dict):
            raise LiveBridgeError("Structure invalide : la racine du fichier doit être un objet JSON.")
        if "ventes" not in payload and isinstance(payload.get("articles"), list):
            raise LiveBridgeError(
                "Ce fichier est un export de STOCK (POS -> Live Shopping), pas un export de ventes."
            )

        problems = []
        ref = payload.get("session_reference")
        if not isinstance(ref, str) or not ref.strip():
            problems.append("Champ « session_reference » manquant ou vide.")
        if not isinstance(payload.get("exported_at"), str) or not _parse_iso(payload.get("exported_at")):
            problems.append("Champ « exported_at » manquant ou invalide (date ISO 8601 attendue).")
        ventes = payload.get("ventes")
        if not isinstance(ventes, list):
            problems.append("Champ « ventes » manquant ou invalide (liste attendue).")
        elif len(ventes) > MAX_LINES_PER_FILE:
            problems.append(f"Trop de lignes ({len(ventes)}) : maximum {MAX_LINES_PER_FILE} par fichier.")
        if problems:
            raise LiveBridgeError("Fichier de ventes invalide.", problems)

        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return payload, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _parse_line(cls, raw: Any) -> Dict[str, Any]:
        """Normalise une ligne de vente ; toute anomalie va dans `errors` (ligne bloquée)."""
        line: Dict[str, Any] = {
            "errors": [], "warnings": [],
            "client": {"prenom": "", "nom": "", "telephone": "", "email": "", "adresse": ""},
        }
        if not isinstance(raw, dict):
            line["errors"].append("Ligne invalide (objet attendu).")
            return line

        sku = raw.get("sku")
        if isinstance(sku, str) and sku.strip():
            line["sku"] = sku.strip()
        else:
            line["errors"].append("SKU manquant.")

        q = raw.get("quantite")
        if isinstance(q, bool) or not isinstance(q, (int, float)) or q != int(q) or int(q) <= 0:
            line["errors"].append("Quantité invalide (entier strictement positif attendu).")
        else:
            line["quantite"] = int(q)

        try:
            prix = _dec(raw.get("prix_unitaire"))
            if prix < 0:
                raise InvalidOperation("negatif")
            line["prix_unitaire"] = quantize_money(prix)
        except (InvalidOperation, ValueError, TypeError):
            line["errors"].append("Prix unitaire invalide.")

        client = raw.get("client")
        if not isinstance(client, dict):
            line["errors"].append("Fiche client manquante.")
            client = {}
        txt = lambda k: (str(client.get(k)).strip() if client.get(k) not in (None, "") else "")
        line["client"] = {
            "prenom": txt("prenom"), "nom": txt("nom"), "telephone": txt("telephone"),
            "email": txt("email"), "adresse": txt("adresse"),
        }
        if not (line["client"]["prenom"] or line["client"]["nom"] or line["client"]["telephone"]):
            line["errors"].append("Client sans nom ni téléphone.")

        mode_rec = _norm_key(client.get("mode_recuperation")) if client.get("mode_recuperation") else ""
        if mode_rec in ("livraison",):
            line["mode_recuperation"] = "livraison"
        elif mode_rec in ("retrait", "retrait_magasin", "retrait_boutique"):
            line["mode_recuperation"] = "retrait"
        else:
            line["mode_recuperation"] = ""
            if mode_rec:
                line["warnings"].append(f"Mode de récupération inconnu ({client.get('mode_recuperation')!r}).")

        if raw.get("mode_paiement") in (None, ""):
            line["mode_paiement"] = None  # choisi par l'opérateur (paiement physique en boutique)
        else:
            label = payment_label(raw.get("mode_paiement"))
            if label:
                line["mode_paiement"] = label
            else:
                line["errors"].append(f"Mode de paiement inconnu ({raw.get('mode_paiement')!r}).")

        line["reference_paiement"] = str(raw.get("reference_paiement") or "").strip()
        line["statut"] = _norm_key(raw.get("statut"))
        line["date_commande"] = str(raw.get("date_commande") or "").strip()
        if not _parse_iso(line["date_commande"]):
            line["warnings"].append("Date de commande absente ou invalide.")
        line["commande_id"] = str(raw.get("commande_id") or "").strip()
        return line

    # -------------------------------------------------------------------------
    # 4. PLAN D'IMPORT (lecture seule : sert à la prévisualisation ET à l'application)
    # -------------------------------------------------------------------------

    @classmethod
    def _plan(cls, cursor, payload: Dict[str, Any], file_hash: str,
              default_payment: str) -> Dict[str, Any]:
        ref = payload["session_reference"].strip()
        default_label = payment_label(default_payment)
        if not default_label:
            raise LiveBridgeError(f"Moyen de paiement par défaut inconnu : {default_payment!r}.")

        stock = cls._load_stock(cursor)
        by_id = {s["stock_id"]: s for s in stock}
        by_sku = {s["sku"]: s for s in stock}
        cursor.execute("SELECT sku, stock_id FROM Live_Bridge_Skus WHERE session_reference=?", (ref,))
        exported = {r[0]: r[1] for r in cursor.fetchall()}
        cursor.execute("SELECT fingerprint FROM Live_Bridge_Import_Lines WHERE session_reference=?", (ref,))
        already = {r[0] for r in cursor.fetchall()}
        cursor.execute("SELECT 1 FROM Live_Bridge_Exports WHERE session_reference=?", (ref,))
        known_session = cursor.fetchone() is not None

        file_warnings = []
        if not known_session:
            file_warnings.append(
                f"La session « {ref} » n'a pas été exportée depuis ce POS : les SKU sont recherchés "
                "dans le stock actuel."
            )
        cursor.execute("SELECT valeur FROM Parametres WHERE cle='shop_name'")
        row = cursor.fetchone()
        boutique_file = str(payload.get("boutique") or "").strip()
        if boutique_file and row and row[0] and _norm_key(boutique_file) != _norm_key(row[0]):
            file_warnings.append(
                f"Le fichier vient de la boutique « {boutique_file} » (ce POS : « {row[0]} »)."
            )

        remaining: Dict[int, int] = {}
        occurrences: Dict[str, int] = {}
        lines: List[Dict[str, Any]] = []
        orders: Dict[Tuple, Dict[str, Any]] = {}

        for idx, raw in enumerate(payload["ventes"], 1):
            ln = cls._parse_line(raw)
            ln["index"] = idx
            ln["status"], ln["reasons"] = "ready", list(ln["errors"])
            lines.append(ln)

            if ln["reasons"]:
                ln["status"] = "blocked"
                continue
            if ln["statut"] != "paye":
                ln["status"], ln["reasons"] = "ignored", [
                    f"Vente non payée (statut « {raw.get('statut')} ») : ignorée."]
                continue

            mode = ln["mode_paiement"] or default_label
            if ln["mode_paiement"] is None:
                ln["warnings"].append(f"Aucun mode de paiement dans le fichier : « {default_label} » appliqué.")
            ln["mode_paiement_final"] = mode
            phone_key = normalize_phone(ln["client"]["telephone"])
            ident = phone_key or _norm_key(f"{ln['client']['prenom']} {ln['client']['nom']}")

            base_fp = "|".join([
                ref, ln["commande_id"], ln["sku"], str(ln["quantite"]), str(ln["prix_unitaire"]),
                ident, ln["date_commande"], mode,
            ])
            n = occurrences.get(base_fp, 0)
            occurrences[base_fp] = n + 1
            ln["fingerprint"] = hashlib.sha256(f"{base_fp}#{n}".encode("utf-8")).hexdigest()
            if ln["fingerprint"] in already:
                ln["status"], ln["reasons"] = "duplicate", ["Déjà importée lors d'un import précédent."]
                continue

            stock_id = exported.get(ln["sku"], exported.get(ln["sku"].upper()))
            item = by_id.get(stock_id) if stock_id else (by_sku.get(ln["sku"]) or by_sku.get(ln["sku"].upper()))
            if not item:
                ln["status"], ln["reasons"] = "blocked", [f"SKU « {ln['sku']} » introuvable dans le stock du POS."]
                continue
            ln["stock_id"], ln["nom"], ln["variante"] = item["stock_id"], item["nom"], item["variante"]
            ln["prix_pos"] = item["prix"]

            dispo = remaining.setdefault(item["stock_id"], item["quantite"])
            if ln["quantite"] > dispo:
                ln["status"], ln["reasons"] = "blocked", [
                    f"Stock insuffisant : {ln['quantite']} vendu(s), {max(dispo, 0)} disponible(s)."]
                continue
            remaining[item["stock_id"]] = dispo - ln["quantite"]

            if ln["prix_unitaire"] != ln["prix_pos"]:
                ln["warnings"].append(
                    f"Prix du live ({ln['prix_unitaire']} €) différent du prix actuel du POS ({ln['prix_pos']} €).")

            key = (ln["commande_id"] or (ident, ln["date_commande"]), mode)
            order = orders.setdefault(key, {
                "client": ln["client"], "mode_recuperation": ln["mode_recuperation"],
                "mode_paiement": mode, "reference_paiement": ln["reference_paiement"],
                "date_commande": ln["date_commande"], "lines": [],
            })
            # Les infos de contact peuvent n'être complètes que sur une ligne du panier
            for k, v in ln["client"].items():
                if v and not order["client"].get(k):
                    order["client"][k] = v
            order["mode_recuperation"] = order["mode_recuperation"] or ln["mode_recuperation"]
            order["reference_paiement"] = order["reference_paiement"] or ln["reference_paiement"]
            order["lines"].append(ln)

        order_list = []
        for order in orders.values():
            live_total = sum((quantize_money(l["prix_unitaire"] * l["quantite"]) for l in order["lines"]), Decimal("0.00"))
            pos_total = sum((quantize_money(l["prix_pos"] * l["quantite"]) for l in order["lines"]), Decimal("0.00"))
            order["total_live"], order["total_pos"] = live_total, pos_total
            # Le ticket doit refléter l'argent réellement encaissé : si le live a vendu moins
            # cher que le prix POS, l'écart passe en remise (le moteur de vente relit toujours
            # le prix catalogue). Si le live a vendu plus cher, on ne peut pas facturer
            # au-dessus du catalogue : le ticket reste au prix POS et l'écart est signalé.
            if live_total < pos_total and pos_total > 0:
                order["discount_percent"] = ((Decimal("1") - live_total / pos_total) * 100).quantize(Decimal("0.0000000001"))
            else:
                order["discount_percent"] = Decimal("0")
            order["total_ticket"] = min(live_total, pos_total)
            order["price_gap"] = live_total != pos_total
            if order["mode_recuperation"] == "livraison" and not order["client"].get("adresse"):
                order["warnings"] = ["Livraison sans adresse : le bordereau sera incomplet."]
            else:
                order["warnings"] = []
            if live_total > pos_total:
                order["warnings"].append(
                    f"Encaissé {live_total} € au live pour {pos_total} € au prix POS : ticket au prix POS.")
            order_list.append(order)

        counts = {k: sum(1 for l in lines if l["status"] == k) for k in ("ready", "blocked", "duplicate", "ignored")}
        articles: Dict[int, Dict[str, Any]] = {}
        for l in lines:
            if l["status"] == "ready":
                a = articles.setdefault(l["stock_id"], {
                    "sku": l["sku"], "nom": l["nom"], "variante": l["variante"],
                    "quantite": 0, "stock_actuel": by_id[l["stock_id"]]["quantite"],
                })
                a["quantite"] += l["quantite"]
        for a in articles.values():
            a["stock_apres"] = a["stock_actuel"] - a["quantite"]

        return {
            "session_reference": ref, "file_hash": file_hash,
            "exported_at": payload.get("exported_at"), "boutique": boutique_file,
            "default_payment": default_label, "warnings": file_warnings,
            "lines": lines, "orders": order_list, "articles": list(articles.values()),
            "counts": counts,
            "total_tvac": sum((o["total_ticket"] for o in order_list), Decimal("0.00")),
        }

    @classmethod
    def _plan_to_json(cls, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Version JSON-sérialisable du plan (prévisualisation)."""
        def line(l):
            return {
                "index": l["index"], "sku": l.get("sku", ""), "nom": l.get("nom", ""),
                "variante": l.get("variante", ""), "quantite": l.get("quantite"),
                "prix_unitaire": float(l["prix_unitaire"]) if "prix_unitaire" in l else None,
                "client": " ".join(x for x in (l["client"]["prenom"], l["client"]["nom"]) if x) or l["client"]["telephone"],
                "mode_recuperation": l.get("mode_recuperation", ""),
                "status": l["status"], "reasons": l["reasons"], "warnings": l["warnings"],
            }

        def order(o):
            c = o["client"]
            return {
                "client": " ".join(x for x in (c["prenom"], c["nom"]) if x) or c["telephone"],
                "telephone": c["telephone"], "mode_recuperation": o["mode_recuperation"],
                "mode_paiement": o["mode_paiement"], "nb_lignes": len(o["lines"]),
                "total": float(o["total_ticket"]), "total_live": float(o["total_live"]),
                "total_pos": float(o["total_pos"]), "warnings": o["warnings"],
            }

        c = plan["counts"]
        return {
            "session_reference": plan["session_reference"], "file_hash": plan["file_hash"],
            "exported_at": plan["exported_at"], "boutique": plan["boutique"],
            "default_payment": plan["default_payment"], "warnings": plan["warnings"],
            "summary": {
                "nb_lignes": len(plan["lines"]), "nb_importables": c["ready"],
                "nb_bloquees": c["blocked"], "nb_deja_importees": c["duplicate"],
                "nb_ignorees": c["ignored"], "nb_tickets": len(plan["orders"]),
                "nb_livraisons": sum(1 for o in plan["orders"] if o["mode_recuperation"] == "livraison"),
                "total_tvac": float(plan["total_tvac"]),
                "nb_sans_mode_paiement": sum(
                    1 for l in plan["lines"] if l["status"] == "ready" and l.get("mode_paiement") is None),
            },
            # Rien de neuf à importer et tout ce qui était valide l'a déjà été
            "already_imported": c["duplicate"] > 0 and c["ready"] == 0 and c["blocked"] == 0,
            "lines": [line(l) for l in plan["lines"]],
            "orders": [order(o) for o in plan["orders"]],
            "articles": plan["articles"],
        }

    @classmethod
    def preview_import(cls, content: Any, default_payment: str = "especes", conn=None) -> Dict[str, Any]:
        """Analyse le fichier SANS rien écrire (hors création de tables vides)."""
        payload, file_hash = cls._parse_file(content)
        own = conn is None
        conn = conn or get_connection()
        try:
            cls.ensure_schema(conn)
            return cls._plan_to_json(cls._plan(conn.cursor(), payload, file_hash, default_payment))
        finally:
            if own:
                conn.close()

    # -------------------------------------------------------------------------
    # 5. APPLICATION DE L'IMPORT
    # -------------------------------------------------------------------------

    @classmethod
    def _upsert_client(cls, cursor, c: Dict[str, str]) -> int:
        """Rapproche par téléphone puis email ; crée sinon. Ne remplace jamais une info existante."""
        full_name = f"{c['prenom']} {c['nom']}".strip() or c["telephone"]
        phone_key = normalize_phone(c["telephone"])
        client_id = None
        if phone_key:
            cursor.execute("SELECT id, telephone FROM Clients WHERE telephone IS NOT NULL AND telephone != ''")
            for cid, tel in cursor.fetchall():
                if normalize_phone(tel) == phone_key:
                    client_id = cid
                    break
        if client_id is None and c["email"]:
            cursor.execute("SELECT id FROM Clients WHERE email=?", (c["email"],))
            row = cursor.fetchone()
            client_id = row[0] if row else None

        if client_id is not None:
            cursor.execute("SELECT email, telephone, adresse FROM Clients WHERE id=?", (client_id,))
            email, tel, adr = cursor.fetchone()
            if c["telephone"] and not tel:
                cursor.execute("UPDATE Clients SET telephone=? WHERE id=?", (c["telephone"], client_id))
            if c["adresse"] and not adr:
                cursor.execute("UPDATE Clients SET adresse=? WHERE id=?", (c["adresse"], client_id))
            if c["email"] and not email:
                cursor.execute("SELECT 1 FROM Clients WHERE email=? AND id!=?", (c["email"], client_id))
                if not cursor.fetchone():
                    cursor.execute("UPDATE Clients SET email=? WHERE id=?", (c["email"], client_id))
            return client_id

        email = c["email"] or None
        if email:  # Clients.email est UNIQUE
            cursor.execute("SELECT 1 FROM Clients WHERE email=?", (email,))
            if cursor.fetchone():
                email = None
        cursor.execute(
            "INSERT INTO Clients (nom, email, telephone, adresse) VALUES (?,?,?,?)",
            (full_name, email, c["telephone"] or None, c["adresse"] or None),
        )
        return cursor.lastrowid

    @classmethod
    def apply_import(cls, content: Any, default_payment: str = "especes",
                     cashier_name: str = "Live Shopping", conn=None) -> Dict[str, Any]:
        """Applique l'import. Ré-analyse le fichier (l'aperçu n'est jamais pris pour acquis).

        Chaque ticket est atomique : ses lignes anti-doublon, le client et la vente sont
        validés dans la même transaction (process_sale_transaction commite l'ensemble).
        """
        payload, file_hash = cls._parse_file(content)
        own = conn is None
        conn = conn or get_connection()
        try:
            cls.ensure_schema(conn)
            cur = conn.cursor()
            plan = cls._plan(cur, payload, file_hash, default_payment)
            ref = plan["session_reference"]

            tickets, failures = [], []
            if plan["orders"]:
                cur.execute(
                    "INSERT INTO Live_Bridge_Imports (session_reference, file_hash, file_exported_at, cashier_name, nb_lines) "
                    "VALUES (?,?,?,?,?)",
                    (ref, file_hash, plan["exported_at"], cashier_name, len(plan["lines"])),
                )
                import_id = cur.lastrowid
                conn.commit()
            else:
                import_id = None

            for order in plan["orders"]:
                label = " ".join(x for x in (order["client"]["prenom"], order["client"]["nom"]) if x) \
                    or order["client"]["telephone"]
                try:
                    client_id = cls._upsert_client(cur, order["client"])
                    cur.executemany(
                        "INSERT INTO Live_Bridge_Import_Lines (fingerprint, session_reference, import_id, sku, quantite) "
                        "VALUES (?,?,?,?,?)",
                        [(l["fingerprint"], ref, import_id, l["sku"], l["quantite"]) for l in order["lines"]],
                    )
                    cart = [{
                        "stock_id": l["stock_id"], "quantite": l["quantite"],
                        "nom": l["nom"], "prix_vente_tvac": float(l["prix_pos"]),
                    } for l in order["lines"]]
                    total = float(order["total_ticket"])
                    sale = process_sale_transaction(
                        cart_items=cart, total_tvac=total,
                        payments=[(order["mode_paiement"], total)],
                        client_id=client_id, cashier_name=cashier_name,
                        discount_percent=float(order["discount_percent"]), conn=conn,
                    )
                    ticket_id = sale["ticket_id"]
                    cur.execute("UPDATE Live_Bridge_Import_Lines SET ticket_id=? WHERE import_id=? AND ticket_id IS NULL "
                                "AND fingerprint IN (%s)" % ",".join("?" * len(order["lines"])),
                                [ticket_id, import_id] + [l["fingerprint"] for l in order["lines"]])
                    c = order["client"]
                    cur.execute(
                        "INSERT INTO Live_Bridge_Orders (import_id, session_reference, ticket_id, numero_ticket, client_id, "
                        "client_nom, telephone, email, mode_recuperation, adresse, mode_paiement, reference_paiement, "
                        "date_commande, total_tvac, lignes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (import_id, ref, ticket_id, sale["numero_ticket"], client_id, label, c["telephone"], c["email"],
                         order["mode_recuperation"], c["adresse"], order["mode_paiement"], order["reference_paiement"],
                         order["date_commande"], str(quantize_money(Decimal(str(sale["total_tvac"])))),
                         json.dumps([{"nom": l["nom"], "variante": l["variante"], "sku": l["sku"],
                                      "quantite": l["quantite"]} for l in order["lines"]], ensure_ascii=False)),
                    )
                    conn.commit()
                    tickets.append({
                        "numero_ticket": sale["numero_ticket"], "ticket_id": ticket_id, "client": label,
                        "total": sale["total_tvac"], "mode_recuperation": order["mode_recuperation"],
                    })
                except Exception as e:
                    conn.rollback()
                    failures.append({"client": label, "error": str(e),
                                     "skus": [l["sku"] for l in order["lines"]]})

            imported = sum(len(o["lines"]) for o in plan["orders"]) - sum(len(f["skus"]) for f in failures)
            total = sum((Decimal(str(t["total"])) for t in tickets), Decimal("0.00"))
            blocked = plan["counts"]["blocked"] + sum(len(f["skus"]) for f in failures)
            if import_id is not None:
                cur.execute(
                    "UPDATE Live_Bridge_Imports SET nb_imported=?, nb_blocked=?, total_tvac=? WHERE id=?",
                    (imported, blocked, str(total), import_id),
                )
                conn.commit()

            preview = cls._plan_to_json(plan)
            return {
                "success": not failures, "import_id": import_id, "session_reference": ref,
                "nb_importees": imported, "nb_bloquees": plan["counts"]["blocked"],
                "nb_deja_importees": plan["counts"]["duplicate"], "nb_ignorees": plan["counts"]["ignored"],
                "total_tvac": float(total), "tickets": tickets, "echecs": failures,
                "lines": preview["lines"],
            }
        finally:
            if own:
                conn.close()

    # -------------------------------------------------------------------------
    # 6. HISTORIQUE & DONNÉES DE BORDEREAU
    # -------------------------------------------------------------------------

    @classmethod
    def list_imports(cls, conn=None) -> List[Dict[str, Any]]:
        own = conn is None
        conn = conn or get_connection()
        try:
            cls.ensure_schema(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT i.id, i.session_reference, i.imported_at, i.nb_lines, i.nb_imported, i.nb_blocked, i.total_tvac,
                       (SELECT COUNT(*) FROM Live_Bridge_Orders o WHERE o.import_id = i.id AND o.mode_recuperation='livraison')
                FROM Live_Bridge_Imports i ORDER BY i.id DESC LIMIT 50
            """)
            return [
                {"id": r[0], "session_reference": r[1], "imported_at": r[2], "nb_lignes": r[3],
                 "nb_importees": r[4], "nb_bloquees": r[5], "total_tvac": float(Decimal(str(r[6] or "0"))),
                 "nb_livraisons": r[7]}
                for r in cur.fetchall()
            ]
        finally:
            if own:
                conn.close()

    @classmethod
    def get_delivery_orders(cls, order_id: Optional[int] = None, import_id: Optional[int] = None,
                            conn=None) -> List[Dict[str, Any]]:
        """Commandes en mode livraison, avec tout ce qu'il faut pour imprimer le bordereau."""
        own = conn is None
        conn = conn or get_connection()
        try:
            cls.ensure_schema(conn)
            cur = conn.cursor()
            sql = ("SELECT id, session_reference, numero_ticket, client_nom, telephone, email, adresse, "
                   "mode_paiement, reference_paiement, date_commande, total_tvac, lignes_json "
                   "FROM Live_Bridge_Orders WHERE mode_recuperation='livraison'")
            args: List[Any] = []
            if order_id is not None:
                sql += " AND id=?"
                args.append(order_id)
            if import_id is not None:
                sql += " AND import_id=?"
                args.append(import_id)
            cur.execute(sql + " ORDER BY id", args)
            return [
                {"id": r[0], "session_reference": r[1], "numero_ticket": r[2], "client_nom": r[3],
                 "telephone": r[4] or "", "email": r[5] or "", "adresse": r[6] or "",
                 "mode_paiement": r[7] or "", "reference_paiement": r[8] or "", "date_commande": r[9] or "",
                 "total_tvac": r[10], "lignes": json.loads(r[11] or "[]")}
                for r in cur.fetchall()
            ]
        finally:
            if own:
                conn.close()
