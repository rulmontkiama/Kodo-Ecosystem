# -*- coding: utf-8 -*-
"""
Gestionnaire de Catalogue et de Stock - Kōdo POS Core
Gère les produits, catégories, marques, la mise à jour des stocks multi-tailles,
les codes-barres et les alertes de stock bas.
"""

import random
import re
import sqlite3
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple

from kodo_core.db.connection import get_connection


# Caractères de contrôle C0/C1 : une douchette termine presque toujours sa trame par un
# retour chariot, et les lecteurs configurés en GS1-128 insèrent un séparateur \x1d.
# Ces octets ne font jamais partie du code lui-même : on les retire avant tout stockage.
_BARCODE_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Un code-barres imprimable tient dans l'ASCII imprimable (le jeu couvert par Code128).
_BARCODE_PRINTABLE_RE = re.compile(r"^[\x20-\x7e]+$")

# Un EAN-13 : 13 chiffres, ni plus ni moins.
_EAN13_RE = re.compile(r"^[0-9]{13}$")


class BarcodeConflictError(Exception):
    """
    Conflit d'attribution d'un code-barres.

    Le message est destiné à être affiché tel quel à la commerçante : il nomme
    l'article en cause, en français, sans jargon SQL.

    Attributs :
        code    : "BARCODE_ALREADY_SET" (l'article a déjà un code, remplacement non demandé)
                  ou "BARCODE_TAKEN"    (le code est déjà porté par un autre article).
        details : identifiant et nom de l'article détenteur, plus le code concerné.
    """

    def __init__(self, message: str, code: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class StockHistoriqueError(Exception):
    """
    Opération refusée parce qu'elle effacerait une ancre de l'historique de vente.

    Une ligne de `Stocks` est référencée par `Ventes_Details.id_stock` : la supprimer
    ferait disparaître la vente des états (Z, journal, statistiques) alors que la
    ligne de vente elle-même est scellée. Le message est destiné à être affiché tel
    quel à la commerçante, en français, sans jargon SQL.
    """


# Libellés qui désignent l'absence de déclinaison. Même jeu que
# `kodo_core.domain.sales.cart_engine._LIBELLES_TAILLE_UNIQUE` : l'écran Stocks écrit
# « Taille Unique », l'import Shopify « Unique », d'anciennes versions une taille vide.
# Dupliqué plutôt qu'importé pour ne pas faire dépendre le catalogue du moteur de vente.
_LIBELLES_TAILLE_UNIQUE = {"", "unique", "taille unique", "taille_unique", "default title", "__no_size__"}


def _norm_taille(taille) -> str:
    """Clé de comparaison d'une taille : « M », « m » et «  M  » sont la MÊME déclinaison.

    Règle identique à celle du moteur de vente (`cart_engine._norm_taille`). Sans elle,
    l'enregistrement d'un article créait une ligne de stock par graphie alors que la vente
    n'en retrouvait qu'une : les unités des autres graphies devenaient invendables.
    """
    return str(taille or "").strip().casefold()


class InventoryManager:
    """
    Gestionnaire du catalogue de produits et des mouvements de stock.
    """

    DEFAULT_ALERT_THRESHOLD_KEY = "default_seuil_alerte"
    FALLBACK_ALERT_THRESHOLD = 5

    # Préfixe GS1 réservé à la distribution restreinte (usage interne au magasin) :
    # aucun EAN-13 fournisseur légitime ne commence par 20-29.
    INTERNAL_BARCODE_PREFIX = "200"
    # 9 chiffres libres = 10^9 codes possibles ; 12 tentatives suffisent très largement
    # (échouer 12 fois sur un catalogue de 50 000 codes est de l'ordre de 10^-53).
    BARCODE_MAX_ATTEMPTS = 12
    # Au-delà, l'étiquette 6 x 3,5 cm ne peut plus rendre le code de façon lisible.
    BARCODE_MAX_LENGTH = 48

    @classmethod
    def get_default_alert_threshold(cls, conn=None) -> int:
        """Seuil d'alerte global appliqué aux articles sans seuil personnalisé."""
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT valeur FROM Parametres WHERE cle=?", (cls.DEFAULT_ALERT_THRESHOLD_KEY,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                try:
                    return int(row[0])
                except (ValueError, TypeError):
                    pass
            return cls.FALLBACK_ALERT_THRESHOLD
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def set_default_alert_threshold(cls, value: int, conn=None) -> None:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            threshold = max(0, int(value))
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO Parametres (cle, valeur) VALUES (?, ?)",
                (cls.DEFAULT_ALERT_THRESHOLD_KEY, str(threshold))
            )
            conn.commit()
        finally:
            if should_close and conn:
                conn.close()

    @staticmethod
    def clean_barcode(barcode: Optional[Any]) -> Optional[str]:
        """
        Formatte et nettoie un code-barres (retourne None si vide).

        Accepte une valeur non textuelle : une charge JSON transmet volontiers le code
        comme un nombre, et un `.strip()` direct sur un entier faisait remonter une
        AttributeError jusqu'à l'écran de la commerçante sous forme d'erreur 500.

        Retire ensuite les caractères de contrôle (retour chariot de douchette,
        séparateur GS1) avant de rogner les espaces de bord.
        """
        if not barcode:
            return None

        if isinstance(barcode, bool):
            return None
        if isinstance(barcode, str):
            text = barcode
        elif isinstance(barcode, int):
            text = str(barcode)
        elif isinstance(barcode, Decimal):
            text = format(barcode.normalize(), "f")
        elif isinstance(barcode, float):
            text = format(Decimal(str(barcode)).normalize(), "f")
        else:
            return None

        cleaned = _BARCODE_CONTROL_CHARS_RE.sub("", text).strip()
        return cleaned if cleaned else None

    @staticmethod
    def ean13_check_digit(base12: str) -> str:
        """
        Clé de contrôle EAN-13 des 12 premiers chiffres.

        Pondération GS1 : poids 1 sur le premier chiffre, 3 sur le deuxième, en
        alternance. Les données faisant 12 chiffres (longueur paire), pondérer depuis
        la gauche ou depuis la droite donne rigoureusement le même résultat.
        """
        odd_sum = sum(int(base12[i]) for i in range(0, 12, 2))
        even_sum = sum(int(base12[i]) for i in range(1, 12, 2))
        total = odd_sum + (even_sum * 3)
        return str((10 - (total % 10)) % 10)

    @classmethod
    def is_valid_ean13(cls, code: Optional[str]) -> bool:
        """Vrai si `code` est un EAN-13 de 13 chiffres dont la clé de contrôle est juste."""
        if not code or not _EAN13_RE.match(code):
            return False
        return cls.ean13_check_digit(code[:12]) == code[12]

    @classmethod
    def is_printable_barcode(cls, code: Optional[Any]) -> bool:
        """
        Vrai si le code peut être imprimé sans être réécrit en silence.

        Garde-fou contre le piège du moteur d'étiquettes : présenté à 13 chiffres dont
        la clé est fausse, un générateur EAN-13 recalcule la clé et imprime un code
        DIFFÉRENT de celui enregistré en base. L'étiquette collée en rayon ne
        correspondrait alors plus à l'article. On refuse donc ce cas-là plutôt que de
        laisser passer une divergence invisible.

        Les autres codes sont acceptés s'ils tiennent dans l'ASCII imprimable (jeu
        couvert par Code128) et dans la largeur de l'étiquette.
        """
        cleaned = cls.clean_barcode(code)
        if not cleaned or len(cleaned) > cls.BARCODE_MAX_LENGTH:
            return False
        if _EAN13_RE.match(cleaned):
            return cls.is_valid_ean13(cleaned)
        return bool(_BARCODE_PRINTABLE_RE.match(cleaned))

    @classmethod
    def generate_internal_barcode(cls) -> str:
        """Génère un code-barres interne valide à 13 chiffres (EAN-13)."""
        base = cls.INTERNAL_BARCODE_PREFIX + "".join([str(random.randint(0, 9)) for _ in range(9)])
        return base + cls.ean13_check_digit(base)

    @classmethod
    def generate_unique_internal_barcode(cls, conn) -> str:
        """
        Tire un code-barres interne qui n'existe pas encore dans le catalogue.

        `conn` est obligatoire, et ce n'est pas une commodité : le tirage doit avoir
        lieu dans la MÊME transaction que l'écriture. Contrôler l'unicité sur une
        connexion puis écrire sur une autre laisserait la fenêtre de course ouverte,
        et la contrainte UNIQUE de Produits.code_barre remonterait l'erreur à l'écran.

        La boucle ne remplace pas la contrainte, elle lui évite de se déclencher :
        l'arbitre final reste l'index unique, côté appelant.
        """
        if conn is None:
            raise ValueError(
                "generate_unique_internal_barcode exige une connexion ouverte : le tirage "
                "doit avoir lieu dans la même transaction que l'écriture."
            )

        cursor = conn.cursor()
        for _ in range(cls.BARCODE_MAX_ATTEMPTS):
            candidate = cls.generate_internal_barcode()
            cursor.execute("SELECT 1 FROM Produits WHERE code_barre = ? LIMIT 1", (candidate,))
            if cursor.fetchone() is None:
                return candidate

        raise RuntimeError(
            "Impossible de générer un code-barres interne libre après "
            f"{cls.BARCODE_MAX_ATTEMPTS} tentatives."
        )

    @classmethod
    def _validate_imposed_barcode(cls, barcode: Any) -> str:
        """
        Valide un code-barres saisi par la commerçante. Lève ValueError, en français,
        en nommant précisément ce qui ne va pas.
        """
        cleaned = cls.clean_barcode(barcode)
        if not cleaned:
            raise ValueError("Le code-barres est vide.")

        if len(cleaned) > cls.BARCODE_MAX_LENGTH:
            raise ValueError(
                f"Le code-barres « {cleaned} » est trop long "
                f"({len(cleaned)} caractères, maximum {cls.BARCODE_MAX_LENGTH})."
            )

        if _EAN13_RE.match(cleaned) and not cls.is_valid_ean13(cleaned):
            attendue = cls.ean13_check_digit(cleaned[:12])
            raise ValueError(
                f"Le code-barres « {cleaned} » n'est pas un EAN-13 valide : "
                f"son dernier chiffre devrait être {attendue} et non {cleaned[12]}."
            )

        if not _BARCODE_PRINTABLE_RE.match(cleaned):
            raise ValueError(
                f"Le code-barres « {cleaned} » contient des caractères qui ne peuvent pas être imprimés."
            )

        return cleaned

    @classmethod
    def assign_barcode(
        cls,
        product_id: int,
        barcode: Optional[str] = None,
        overwrite: bool = False,
        conn=None
    ) -> Dict[str, Any]:
        """
        Attribue un code-barres à un article existant du stock.

        `barcode=None`  : un code interne EAN-13 est généré, garanti libre.
        `barcode="..."` : code imposé par la commerçante, validé puis vérifié libre.

        Un code déjà attribué n'est JAMAIS remplacé sans `overwrite=True` : il peut
        déjà être imprimé sur des étiquettes collées en rayon, que le remplacement
        rendrait orphelines.

        Retourne {"barcode": str, "generated": bool, "previous_barcode": str | None}.

        Lève :
            LookupError            si l'article n'existe pas,
            ValueError             si le code imposé est invalide,
            BarcodeConflictError   si l'article a déjà un code (sans overwrite)
                                   ou si le code est porté par un autre article.
        """
        try:
            pid = int(product_id)
        except (ValueError, TypeError):
            raise LookupError(f"Identifiant d'article invalide : {product_id!r}.")

        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        opened_transaction = False
        try:
            # Réservation immédiate du verrou d'écriture : deux postes qui attribuent
            # un code au même instant ne doivent pas pouvoir tirer le même.
            if not getattr(conn, "in_transaction", False):
                conn.execute("BEGIN IMMEDIATE")
                opened_transaction = True

            cursor = conn.cursor()
            cursor.execute("SELECT id, nom, code_barre FROM Produits WHERE id = ?", (pid,))
            row = cursor.fetchone()
            if row is None:
                raise LookupError(f"Aucun article ne porte l'identifiant {pid}.")

            product_name = row[1]
            previous_barcode = cls.clean_barcode(row[2])

            if previous_barcode and not overwrite:
                raise BarcodeConflictError(
                    f"L'article « {product_name} » porte déjà le code-barres {previous_barcode}.",
                    code="BARCODE_ALREADY_SET",
                    details={
                        "product_id": pid,
                        "product_name": product_name,
                        "barcode": previous_barcode
                    }
                )

            imposed = barcode is not None
            wanted = cls._validate_imposed_barcode(barcode) if imposed else None

            if imposed:
                cursor.execute(
                    "SELECT id, nom FROM Produits WHERE code_barre = ? AND id <> ? LIMIT 1",
                    (wanted, pid)
                )
                holder = cursor.fetchone()
                if holder is not None:
                    raise BarcodeConflictError(
                        f"Le code-barres {wanted} est déjà utilisé par l'article « {holder[1]} ».",
                        code="BARCODE_TAKEN",
                        details={
                            "product_id": int(holder[0]),
                            "product_name": holder[1],
                            "barcode": wanted
                        }
                    )

            if overwrite:
                update_sql = "UPDATE Produits SET code_barre=? WHERE id=?"
            else:
                # Garde côté SQL : si un autre poste a attribué un code entre notre
                # lecture et notre écriture, rowcount vaut 0 et l'on refuse au lieu
                # d'écraser ce qu'il vient d'attribuer.
                update_sql = (
                    "UPDATE Produits SET code_barre=? "
                    "WHERE id=? AND (code_barre IS NULL OR TRIM(code_barre) = '')"
                )

            attempts = 1 if imposed else cls.BARCODE_MAX_ATTEMPTS
            for _ in range(attempts):
                code = wanted if imposed else cls.generate_unique_internal_barcode(conn)
                try:
                    cursor.execute(update_sql, (code, pid))
                except sqlite3.IntegrityError:
                    if imposed:
                        # Un code imposé qui collisionne n'est pas de la malchance :
                        # un autre article vient de le prendre.
                        cursor.execute(
                            "SELECT id, nom FROM Produits WHERE code_barre = ? AND id <> ? LIMIT 1",
                            (code, pid)
                        )
                        holder = cursor.fetchone()
                        nom_detenteur = holder[1] if holder is not None else "un autre article"
                        raise BarcodeConflictError(
                            f"Le code-barres {code} est déjà utilisé par l'article « {nom_detenteur} ».",
                            code="BARCODE_TAKEN",
                            details={
                                "product_id": int(holder[0]) if holder is not None else None,
                                "product_name": holder[1] if holder is not None else None,
                                "barcode": code
                            }
                        )
                    # Code interne : on retire simplement au tour suivant.
                    continue

                if cursor.rowcount != 1:
                    cursor.execute("SELECT nom, code_barre FROM Produits WHERE id = ?", (pid,))
                    concurrent = cursor.fetchone()
                    code_concurrent = cls.clean_barcode(concurrent[1]) if concurrent else None
                    raise BarcodeConflictError(
                        f"L'article « {product_name} » porte déjà le code-barres {code_concurrent}.",
                        code="BARCODE_ALREADY_SET",
                        details={
                            "product_id": pid,
                            "product_name": product_name,
                            "barcode": code_concurrent
                        }
                    )

                conn.commit()
                return {
                    "barcode": code,
                    "generated": not imposed,
                    "previous_barcode": previous_barcode
                }

            raise RuntimeError(
                "Impossible d'attribuer un code-barres interne libre après "
                f"{cls.BARCODE_MAX_ATTEMPTS} tentatives."
            )

        except Exception:
            if opened_transaction or should_close:
                conn.rollback()
            raise
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_all_products(
        cls,
        category: Optional[str] = None,
        brand: Optional[str] = None,
        search: Optional[str] = None,
        conn=None
    ) -> List[Dict[str, Any]]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            default_alert = cls.get_default_alert_threshold(conn=conn)

            query = """
                SELECT p.id, p.code_barre, p.nom, p.categorie, p.prix_achat_htva,
                       p.prix_vente_tvac, p.taux_tva, p.image_path, p.en_solde,
                       p.prix_solde_tvac, p.type_vente, p.unite_mesure, p.marque, p.attributs_json,
                       COALESCE(SUM(s.quantite_actuelle), 0) as stock_total,
                       p.seuil_alerte,
                       COALESCE(p.requires_stock_audit, 0) as requires_stock_audit
                FROM Produits p
                LEFT JOIN Stocks s ON p.id = s.id_produit
                WHERE 1=1
            """
            params: List[Any] = []

            if category:
                query += " AND LOWER(p.categorie) = LOWER(?)"
                params.append(category)

            if brand:
                query += " AND LOWER(p.marque) = LOWER(?)"
                params.append(brand)

            if search:
                query += " AND (LOWER(p.nom) LIKE LOWER(?) OR p.code_barre LIKE ?)"
                search_param = f"%{search}%"
                params.extend([search_param, search_param])

            query += " GROUP BY p.id ORDER BY p.nom ASC"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            products = []
            for r in rows:
                prod_id = r[0]

                px_tvac = float(r[5]) if r[5] is not None else 0.0
                px_solde = float(r[9]) if r[9] is not None else None

                custom_threshold = r[15] if len(r) > 15 else None
                if custom_threshold is not None:
                    try:
                        alert_val = int(custom_threshold)
                    except (ValueError, TypeError):
                        alert_val = default_alert
                else:
                    alert_val = default_alert

                cursor.execute("""
                    SELECT id, taille, quantite_actuelle, seuil_alerte,
                           COALESCE(requires_stock_audit, 0)
                    FROM Stocks
                    WHERE id_produit = ?
                """, (prod_id,))
                stock_rows = cursor.fetchall()
                stocks_detail = [
                    {
                        "stock_id": s[0],
                        "size": s[1] or "Taille Unique",
                        "quantity": int(s[2]),
                        # Même chaîne de repli que le moteur d'alertes (get_low_stock_alerts) :
                        # seuil de la ligne, puis seuil du produit, puis seuil global. Retomber
                        # directement sur le seuil global affichait « 5 » sous une ligne que le
                        # moteur alertait à 20 — deux seuils pour la même taille.
                        "alert_threshold": int(s[3]) if s[3] is not None else alert_val,
                        "requires_stock_audit": bool(s[4])
                    }
                    for s in stock_rows
                ]

                sizes_str = "|".join([f"{s['size']}:{s['quantity']}" for s in stocks_detail])

                products.append({
                    "id": str(r[0]),
                    "product_id": r[0],
                    "barcode": r[1] or "",
                    "name": r[2],
                    "category": r[3] or "Général",
                    "purchase_price_htva": float(r[4]) if r[4] is not None else 0.0,
                    "price": px_tvac,
                    "price_tvac": px_tvac,
                    "vat_rate": float(r[6]) if r[6] is not None else 0.21,
                    "image_path": r[7] or "",
                    "en_solde": bool(r[8]),
                    "prix_solde_tvac": px_solde,
                    "type": "service" if r[10] == "service" else "product",
                    "type_vente": r[10] or "unite",
                    "unite_mesure": r[11] or "pce",
                    "brand": r[12] or "",
                    "attributes_json": r[13] or "",
                    "sizes": sizes_str,
                    "stock": int(r[14]),
                    "stocks": stocks_detail,
                    "alertStock": alert_val,
                    "alert_stock": alert_val,
                    "alert_threshold": alert_val,
                    "seuil_alerte": alert_val,
                    "has_custom_alert_threshold": custom_threshold is not None,
                    # Survente détectée lors d'un rejeu hors-ligne : la ligne demande un
                    # recomptage. Sans cette exposition, le drapeau restait invisible et la
                    # commerçante n'avait aucun moyen de savoir quoi recompter.
                    "requires_stock_audit": bool(r[16]) if len(r) > 16 else False
                })

            return products
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_product_by_id(cls, product_id: int, conn=None) -> Optional[Dict[str, Any]]:
        prods = cls.get_all_products(conn=conn)
        for p in prods:
            if int(p["product_id"]) == int(product_id):
                return p
        return None

    @classmethod
    def get_product_by_barcode(cls, barcode: str, conn=None) -> Optional[Dict[str, Any]]:
        """
        Résout un article par son code-barres.

        La passe exacte utilise l'index unique de Produits.code_barre. L'ancienne
        implémentation passait par un LIKE '%code%' : le joker de tête interdisait
        l'index, imposait un balayage complet du catalogue à chaque coup de douchette,
        et le filtre Python final était sensible à la casse — un code alphanumérique
        scanné en minuscules ne retrouvait pas son article.
        """
        cleaned = cls.clean_barcode(barcode)
        if not cleaned:
            return None

        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM Produits WHERE code_barre = ? LIMIT 1", (cleaned,))
            row = cursor.fetchone()
            if row is None:
                # Repli insensible à la casse, réservé aux codes alphanumériques
                # (SKU, Code128). Non indexé, mais seulement emprunté quand la passe
                # exacte a échoué.
                cursor.execute(
                    "SELECT id FROM Produits WHERE code_barre = ? COLLATE NOCASE LIMIT 1",
                    (cleaned,)
                )
                row = cursor.fetchone()
            if row is None:
                return None

            return cls.get_product_by_id(int(row[0]), conn=conn)
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def save_product(cls, data: Dict[str, Any], conn=None) -> Dict[str, Any]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()

            prod_id = data.get("id") or data.get("product_id")
            name = data.get("name") or data.get("nom")
            category = data.get("category") or data.get("categorie") or "Général"
            brand = data.get("brand") or data.get("marque") or ""
            # Un champ code-barres ABSENT de la charge ne doit pas effacer le code
            # existant : sans cette distinction, un code généré puis imprimé sur une
            # étiquette disparaissait de la base à la première modification de prix.
            # Un champ PRÉSENT mais vide reste un effacement volontaire.
            barcode_provided = ("barcode" in data) or ("code_barre" in data)
            barcode = cls.clean_barcode(data.get("barcode") or data.get("code_barre"))
            price = Decimal(str(data.get("price") or data.get("prix_vente_tvac") or 0.0))
            purchase_price = Decimal(str(data.get("purchase_price_htva") or data.get("prix_achat_htva") or 0.0))
            vat_rate = Decimal(str(data.get("vat_rate") or data.get("taux_tva") or 0.21))
            sizes_str = data.get("sizes") or ""
            stock_default = int(data.get("stock") or 0)
            is_sale = 1 if data.get("en_solde") else 0
            prix_solde = Decimal(str(data.get("prix_solde_tvac"))) if data.get("prix_solde_tvac") is not None else None

            raw_alert = data.get("alertStock") if data.get("alertStock") is not None else (
                data.get("alert_stock") if data.get("alert_stock") is not None else (
                    data.get("alert_threshold") if data.get("alert_threshold") is not None else data.get("seuil_alerte")
                )
            )
            if isinstance(raw_alert, str) and not raw_alert.strip():
                raw_alert = None
            # None => pas de seuil personnalisé : le produit suivra le seuil global par défaut.
            try:
                alert_threshold = int(raw_alert) if raw_alert is not None else None
            except (ValueError, TypeError):
                alert_threshold = None

            if not name:
                raise ValueError("Le nom du produit est obligatoire")

            if category:
                cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (category,))
            if brand:
                cursor.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (brand,))

            existing_id = None
            if prod_id is not None:
                try:
                    pid_int = int(prod_id)
                    cursor.execute("SELECT id FROM Produits WHERE id = ?", (pid_int,))
                    row = cursor.fetchone()
                    if row:
                        existing_id = row[0]
                except (ValueError, TypeError):
                    existing_id = None

            if not existing_id and barcode:
                # Un code déjà porté par un autre article n'est PAS une invitation à
                # basculer en mise à jour : cela écrasait silencieusement le produit
                # existant (nom, prix, stocks) et l'API répondait « succès ».
                cursor.execute("SELECT id, nom FROM Produits WHERE code_barre = ? LIMIT 1", (barcode,))
                row = cursor.fetchone()
                if row:
                    raise BarcodeConflictError(
                        f"Le code-barres {barcode} est déjà utilisé par l'article « {row[1]} ».",
                        code="BARCODE_TAKEN",
                        details={
                            "product_id": int(row[0]),
                            "product_name": row[1],
                            "barcode": barcode
                        }
                    )

            if existing_id:
                if barcode_provided:
                    try:
                        cursor.execute("UPDATE Produits SET code_barre=? WHERE id=?", (barcode, existing_id))
                    except sqlite3.IntegrityError:
                        # Même conflit que ci-dessus, vu depuis l'édition d'un article
                        # déjà connu : à renvoyer nommément, pas en erreur 500 SQL.
                        cursor.execute(
                            "SELECT id, nom FROM Produits WHERE code_barre = ? AND id <> ? LIMIT 1",
                            (barcode, existing_id)
                        )
                        holder = cursor.fetchone()
                        nom_detenteur = holder[1] if holder is not None else "un autre article"
                        raise BarcodeConflictError(
                            f"Le code-barres {barcode} est déjà utilisé par l'article « {nom_detenteur} ».",
                            code="BARCODE_TAKEN",
                            details={
                                "product_id": int(holder[0]) if holder is not None else None,
                                "product_name": holder[1] if holder is not None else None,
                                "barcode": barcode
                            }
                        )
                cursor.execute("""
                    UPDATE Produits
                    SET nom=?, categorie=?, prix_achat_htva=?, 
                        prix_vente_tvac=?, taux_tva=?, en_solde=?, prix_solde_tvac=?, marque=?, seuil_alerte=?
                    WHERE id=?
                """, (name, category, float(purchase_price), float(price), float(vat_rate), is_sale, float(prix_solde) if prix_solde else None, brand, alert_threshold, existing_id))
                prod_id = existing_id
            else:
                cursor.execute("""
                    INSERT INTO Produits (code_barre, nom, categorie, prix_achat_htva, prix_vente_tvac, taux_tva, en_solde, prix_solde_tvac, marque, seuil_alerte)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (barcode, name, category, float(purchase_price), float(price), float(vat_rate), is_sale, float(prix_solde) if prix_solde else None, brand, alert_threshold))
                prod_id = cursor.lastrowid

            if sizes_str:
                # Les graphies équivalentes (« M », « m », «  M  ») désignent UNE seule
                # déclinaison, comme au moment de la vente. L'ancienne comparaison exacte
                # en créait une par graphie puis n'en conservait qu'une : les unités des
                # autres disparaissaient du compteur sans message. On cumule donc les
                # quantités des graphies équivalentes, aucune unité déclarée n'est perdue.
                declarees: Dict[str, List[Any]] = {}
                for part in sizes_str.split('|'):
                    if ':' not in part:
                        continue
                    sz, qty_str = part.split(':', 1)
                    sz = sz.strip()
                    try:
                        qty = int(qty_str.strip())
                    except ValueError:
                        qty = 0
                    cle = _norm_taille(sz)
                    if cle in declarees:
                        declarees[cle][1] += qty
                    else:
                        declarees[cle] = [sz, qty]
            else:
                declarees = {_norm_taille("Taille Unique"): ["Taille Unique", stock_default]}

            cls._ecrire_lignes_de_stock(cursor, prod_id, declarees, alert_threshold)

            conn.commit()
            return {"success": True, "product_id": str(prod_id)}

        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def _ecrire_lignes_de_stock(cls, cursor, prod_id, declarees: Dict[str, List[Any]], alert_threshold) -> None:
        """Aligne les lignes de `Stocks` d'un article sur les déclinaisons déclarées.

        `declarees` associe une clé de taille normalisée à [libellé retenu, quantité comptée].

        Trois règles, chacune pour un défaut constaté :
        - appariement par clé normalisée, et non par égalité SQL : un simple changement de
          casse recréait une ligne et détachait l'historique de vente de l'ancienne ;
        - « Unique » (import Shopify) et « Taille Unique » (écran Stocks) désignent la même
          absence de déclinaison : sans cette équivalence, réenregistrer un article importé
          créait une SECONDE ligne et doublait son stock total ;
        - une ligne retirée de la déclaration n'est supprimée que si elle n'a jamais été
          vendue ; sinon elle est conservée à 0, car `Ventes_Details.id_stock` y est accroché
          (la supprimer remontait à l'écran en HTTP 500 « FOREIGN KEY constraint failed »).
        """
        cursor.execute("SELECT id, taille FROM Stocks WHERE id_produit=? ORDER BY id", (prod_id,))
        libres = [(r[0], r[1]) for r in cursor.fetchall()]

        apparies: Dict[str, int] = {}
        for cle in declarees:
            trouve = next((e for e in libres if _norm_taille(e[1]) == cle), None)
            if trouve is None and cle in _LIBELLES_TAILLE_UNIQUE:
                trouve = next((e for e in libres if _norm_taille(e[1]) in _LIBELLES_TAILLE_UNIQUE), None)
            if trouve is not None:
                libres.remove(trouve)
                apparies[cle] = trouve[0]

        for cle, (libelle, qty) in declarees.items():
            # « Le stock ne peut pas être négatif » est la règle de la maison (déclencheur
            # prevent_negative_stock). Une quantité négative saisie à l'écran passait
            # pourtant jusqu'en base et faussait ensuite tous les totaux du catalogue :
            # on la refuse nommément plutôt que de la ramener à 0 en silence, ce qui
            # effacerait la saisie de la commerçante sans qu'elle le sache.
            if qty < 0:
                raise ValueError(
                    f"Quantité négative ({qty}) pour la taille « {libelle} » : "
                    "un stock ne peut pas être négatif."
                )

            sid = apparies.get(cle)
            if sid is None:
                cursor.execute(
                    "INSERT INTO Stocks (id_produit, taille, quantite_actuelle, seuil_alerte, requires_stock_audit) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (prod_id, libelle, qty, alert_threshold)
                )
            else:
                # La quantité saisie ici EST le recomptage physique : elle solde la demande
                # d'audit posée par une survente hors-ligne. Sans ce retour à 0, le drapeau
                # `requires_stock_audit` ne pouvait plus jamais être éteint.
                cursor.execute(
                    "UPDATE Stocks SET taille=?, quantite_actuelle=?, seuil_alerte=?, requires_stock_audit=0 WHERE id=?",
                    (libelle, qty, alert_threshold, sid)
                )

        for sid, _taille in libres:
            cursor.execute("SELECT 1 FROM Ventes_Details WHERE id_stock=? LIMIT 1", (sid,))
            if cursor.fetchone():
                cursor.execute("UPDATE Stocks SET quantite_actuelle=0 WHERE id=?", (sid,))
            else:
                cursor.execute("DELETE FROM Stocks WHERE id=?", (sid,))

        # Le produit ne reste « à auditer » que s'il lui reste au moins une ligne non recomptée
        # (typiquement une taille retirée de la vente mais conservée pour l'historique).
        cursor.execute(
            "UPDATE Produits SET requires_stock_audit = "
            "COALESCE((SELECT MAX(requires_stock_audit) FROM Stocks WHERE id_produit=?), 0) WHERE id=?",
            (prod_id, prod_id)
        )

    @classmethod
    def delete_product(cls, product_id: int, conn=None) -> bool:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True

        try:
            cursor = conn.cursor()
            # Supprimer le produit emporte ses lignes de Stocks en cascade, or chacune sert
            # d'ancre à `Ventes_Details.id_stock`. SQLite refusait donc la suppression d'un
            # article déjà vendu par un « FOREIGN KEY constraint failed » remonté tel quel
            # jusqu'à un HTTP 500. On pose la question nous-mêmes pour répondre en français
            # sans toucher à la piste d'audit : une vente passée ne se supprime pas.
            cursor.execute("""
                SELECT p.nom, COUNT(vd.id)
                FROM Produits p
                LEFT JOIN Stocks s ON s.id_produit = p.id
                LEFT JOIN Ventes_Details vd ON vd.id_stock = s.id
                WHERE p.id = ?
            """, (product_id,))
            row = cursor.fetchone()
            nom = row[0] if row else None
            nb_lignes = int(row[1]) if row and row[1] else 0

            if nb_lignes:
                raise StockHistoriqueError(
                    f"« {nom or product_id} » a déjà été vendu ({nb_lignes} ligne(s) de vente) : "
                    "le supprimer effacerait ces ventes des états de caisse. "
                    "Mettez son stock à 0 pour le retirer de la vente."
                )

            # Stocks d'abord : l'ordre inverse dépendait du ON DELETE CASCADE, donc de
            # PRAGMA foreign_keys, et laissait des lignes de stock orphelines quand une
            # connexion sans ce pragma appelait cette méthode.
            cursor.execute("DELETE FROM Stocks WHERE id_produit=?", (product_id,))
            cursor.execute("DELETE FROM Produits WHERE id=?", (product_id,))
            conn.commit()
            return True
        except Exception:
            # Sans ce rollback, une suppression refusée laissait une transaction ouverte sur
            # une connexion partagée : l'écriture suivante la validait avec un état partiel.
            if conn:
                conn.rollback()
            raise
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def bulk_update_alert_threshold(cls, product_ids: List[int], threshold: Optional[int], conn=None) -> bool:
        """Met à jour en masse le seuil d'alerte pour plusieurs produits (None pour rétablir le seuil global)."""
        if not product_ids:
            return True
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            placeholders = ",".join("?" * len(product_ids))
            params = [threshold] + list(product_ids)
            cursor.execute(f"UPDATE Produits SET seuil_alerte=? WHERE id IN ({placeholders})", params)
            cursor.execute(f"UPDATE Stocks SET seuil_alerte=? WHERE id_produit IN ({placeholders})", params)
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    # Catégories et Marques

    @classmethod
    def get_categories(cls, conn=None) -> List[str]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT nom FROM Categories ORDER BY nom ASC")
            return [r[0] for r in cursor.fetchall()]
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def add_category(cls, name: str, conn=None) -> bool:
        if not name or not name.strip():
            return False
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO Categories (nom) VALUES (?)", (name.strip(),))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def delete_category(cls, name: str, conn=None) -> bool:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM Categories WHERE nom=?", (name,))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_brands(cls, conn=None) -> List[str]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT nom FROM Marques ORDER BY nom ASC")
            return [r[0] for r in cursor.fetchall()]
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def add_brand(cls, name: str, conn=None) -> bool:
        if not name or not name.strip():
            return False
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO Marques (nom) VALUES (?)", (name.strip(),))
            conn.commit()
            return True
        finally:
            if should_close and conn:
                conn.close()

    @classmethod
    def get_low_stock_alerts(cls, conn=None) -> List[Dict[str, Any]]:
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        try:
            cursor = conn.cursor()
            default_alert = cls.get_default_alert_threshold(conn=conn)
            cursor.execute("""
                SELECT p.id, p.nom, p.code_barre, s.taille, s.quantite_actuelle,
                       COALESCE(s.seuil_alerte, p.seuil_alerte, ?) as seuil_effectif,
                       COALESCE(s.requires_stock_audit, 0) as requires_stock_audit
                FROM Stocks s
                JOIN Produits p ON s.id_produit = p.id
                WHERE s.quantite_actuelle <= COALESCE(s.seuil_alerte, p.seuil_alerte, ?)
                ORDER BY s.quantite_actuelle ASC
            """, (default_alert, default_alert))
            rows = cursor.fetchall()
            alerts = []
            for r in rows:
                alerts.append({
                    "product_id": r[0],
                    "product_name": r[1],
                    "barcode": r[2] or "",
                    "size": r[3] or "Unique",
                    "current_stock": int(r[4]),
                    "alert_threshold": int(r[5]),
                    "status": "RUPTURE" if r[4] <= 0 else "BAS",
                    # Une rupture signalée « à auditer » vient d'une survente hors-ligne, pas
                    # d'un réassort oublié : c'est un recomptage physique qu'il faut, pas une
                    # commande fournisseur. L'écran ne pouvait pas faire la différence.
                    "requires_stock_audit": bool(r[6])
                })
            return alerts
        finally:
            if should_close and conn:
                conn.close()
