# -*- coding: utf-8 -*-
"""
Routes API Catalogue Produit & Gestion des Stocks - Kōdo POS Core
"""

import os
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Tuple, Optional
from kodo_core.domain.catalog.inventory_manager import InventoryManager


# ---------------------------------------------------------------------------------------------
# ÉTIQUETTES CODE-BARRES
#
# Un code-barres identifie un PRODUIT, jamais une déclinaison : la table `Stocks` n'a pas de colonne
# code-barres, le code vit sur `Produits`. La taille n'est donc qu'un libellé imprimé sur l'étiquette,
# elle n'est pas encodée dans les barres. Scanner l'étiquette d'un « M » ramène l'article, et la
# taille reste choisie en caisse.
#
# On n'invente JAMAIS de code-barres. Un article qui n'en a pas voit sa demande d'étiquette refusée,
# avec un message qui indique quoi faire. Imprimer un code fabriqué à la volée reviendrait à coller
# sur la marchandise une étiquette qui ne ramènera jamais rien au scan.
# ---------------------------------------------------------------------------------------------

# Garde-fou : un rouleau d'étiquettes parti à l'impression ne se rattrape pas.
MAX_ETIQUETTES_PAR_DEMANDE = 500

# Libellés qui désignent l'absence de déclinaison : aucune taille n'est alors imprimée sur
# l'étiquette (aligné sur cart_engine._LIBELLES_TAILLE_UNIQUE).
LIBELLES_SANS_TAILLE = {"", "unique", "taille unique", "taille_unique", "tu", "default title"}


def _prix_etiquette(valeur) -> Decimal:
    """Montant d'étiquette en Decimal arrondi au centime : jamais de float sur un prix."""
    try:
        return Decimal(str(valeur if valeur is not None else 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except Exception:
        return Decimal("0.00")


def _resoudre_lignes_etiquettes(items: Any) -> List[Dict[str, Any]]:
    """
    Transforme la demande de l'écran en lignes d'étiquettes vérifiées en base.

    `items` : [{"product_id": 42, "size": "M", "quantity": 6}, ...]
      - `size` absent     -> une ligne par déclinaison en stock de l'article
      - `quantity` absent -> la quantité réellement en stock pour cette déclinaison

    La taille imprimée est relue dans `Stocks` avec sa casse exacte, jamais reprise telle quelle de
    l'écran : on n'imprime pas d'étiquette pour une déclinaison qui n'existe pas.

    Lève ValueError avec un message directement affichable à la commerçante.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("Sélectionnez au moins un article à étiqueter.")

    verificateur = getattr(InventoryManager, "is_printable_barcode", None)
    if verificateur is None:
        # Échec fermé, volontairement : sans le contrôle de validité du code, une étiquette peut
        # partir avec une clé de contrôle fausse. Le symbole imprimé encode alors un code différent
        # de celui enregistré en base, et l'article devient introuvable au scan, définitivement.
        raise ValueError(
            "La vérification des codes-barres n'est pas disponible dans cette version de "
            "l'application : impression refusée pour ne pas produire d'étiquette illisible."
        )

    lignes: List[Dict[str, Any]] = []
    total = 0

    for rang, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Ligne {rang} : format de demande invalide.")

        try:
            pid = int(item.get("product_id") or item.get("productId") or item.get("id"))
        except (TypeError, ValueError):
            raise ValueError(f"Ligne {rang} : article non identifié.")

        produit = InventoryManager.get_product_by_id(pid)
        if not produit:
            raise ValueError(f"Ligne {rang} : cet article n'existe plus dans le catalogue.")

        nom = produit.get("name") or "Article"
        code = str(produit.get("barcode") or "").strip()
        if not code:
            raise ValueError(
                f"« {nom} » n'a pas encore de code-barres. Attribuez-lui un code depuis la fiche "
                f"article avant d'imprimer son étiquette."
            )
        if not verificateur(code):
            raise ValueError(
                f"Le code-barres « {code} » de « {nom} » est invalide : l'étiquette imprimée ne "
                f"correspondrait pas à la fiche article. Corrigez le code avant d'imprimer."
            )

        declinaisons = produit.get("stocks") or []
        taille_demandee = item.get("size") or item.get("taille")
        if taille_demandee:
            voulu = str(taille_demandee).strip().casefold()
            retenues = [
                s for s in declinaisons
                if str(s.get("size") or "").strip().casefold() == voulu
            ]
            if not retenues:
                disponibles = ", ".join(str(s.get("size")) for s in declinaisons) or "aucune"
                raise ValueError(
                    f"Taille « {taille_demandee} » inconnue pour « {nom} » (tailles : {disponibles})."
                )
        else:
            retenues = declinaisons or [{"size": "", "quantity": 1}]

        quantite_demandee = item.get("quantity")
        if quantite_demandee is None:
            quantite_demandee = item.get("quantite")

        for declinaison in retenues:
            if quantite_demandee is not None:
                try:
                    quantite = int(quantite_demandee)
                except (TypeError, ValueError):
                    raise ValueError(f"Ligne {rang} : nombre d'étiquettes invalide.")
            else:
                try:
                    quantite = int(declinaison.get("quantity") or 0)
                except (TypeError, ValueError):
                    quantite = 0

            if quantite <= 0:
                continue

            total += quantite
            if total > MAX_ETIQUETTES_PAR_DEMANDE:
                raise ValueError(
                    f"Demande trop volumineuse : {total} étiquettes. "
                    f"Imprimez-en au maximum {MAX_ETIQUETTES_PAR_DEMANDE} à la fois."
                )

            libelle_taille = str(declinaison.get("size") or "").strip()
            if libelle_taille.casefold() in LIBELLES_SANS_TAILLE:
                libelle_taille = ""

            en_solde = bool(produit.get("en_solde")) and produit.get("prix_solde_tvac") is not None
            lignes.append({
                "product_id": pid,
                "name": nom,
                "barcode": code,
                "size": libelle_taille,
                "price": _prix_etiquette(produit.get("price")),
                "price_sale": _prix_etiquette(produit.get("prix_solde_tvac")) if en_solde else None,
                "quantity": quantite
            })

    if not lignes:
        raise ValueError("Aucune étiquette à imprimer : toutes les quantités demandées sont nulles.")

    return lignes


def _parametres_moteur_etiquette(reglages: Dict[str, Any]) -> Dict[str, Any]:
    """
    Traduit les réglages stockés (du texte, dans `Parametres`) en arguments du moteur d'étiquettes.

    Un réglage laissé vide n'est PAS transmis : le moteur applique alors sa propre valeur, ce qui
    reste un choix documenté. On ne fabrique jamais ici une dimension d'étiquette de substitution.
    """
    def _nombre(cle):
        texte = str(reglages.get(cle) or "").strip()
        if not texte:
            return None
        try:
            return float(Decimal(texte))
        except Exception:
            return None

    params: Dict[str, Any] = {}
    for cle_param, cle_reglage in (("largeur_mm", "label_width_mm"),
                                   ("hauteur_mm", "label_height_mm"),
                                   ("marge_mm", "label_margin_mm")):
        valeur = _nombre(cle_reglage)
        if valeur is not None:
            params[cle_param] = valeur

    dpi = str(reglages.get("label_dpi") or "").strip()
    if dpi:
        try:
            params["dpi"] = int(dpi)
        except (TypeError, ValueError):
            pass

    orientation = str(reglages.get("label_orientation") or "").strip()
    if orientation:
        params["orientation"] = orientation

    afficher_prix = str(reglages.get("label_show_price") or "").strip()
    if afficher_prix:
        params["show_price"] = (afficher_prix == "1")

    return params


def _media_cups_etiquette(reglages: Dict[str, Any], params: Dict[str, Any]) -> Optional[str]:
    """
    Nom de format CUPS à imposer à l'impression.

    Indispensable : sans `media`, CUPS applique le format par défaut de la file et dessine la page
    sans mise à l'échelle — une page plus large que le support est rognée, code-barres compris.

    On privilégie le format choisi par la commerçante, dont l'identifiant provient du PPD de SA
    machine. À défaut, on décrit le support par ses propres dimensions, dans l'ordre RÉEL de la page :
    l'orientation paysage permute largeur et hauteur.
    """
    format_id = str(reglages.get("label_format_id") or "").strip()
    if format_id:
        try:
            from kodo_core.hardware.pdf import formats_etiquette_disponibles
            for fmt in formats_etiquette_disponibles(reglages.get("label_printer_name") or None):
                if str(fmt.get("id")) == format_id:
                    return fmt.get("media") or format_id
        except Exception as e:
            print(f"[ETIQUETTE FORMAT WARNING] format « {format_id} » non résolu : {e}")
        return format_id

    largeur = params.get("largeur_mm")
    hauteur = params.get("hauteur_mm")
    if largeur is None or hauteur is None:
        return None
    if str(params.get("orientation") or "portrait") == "paysage":
        largeur, hauteur = hauteur, largeur
    return f"Custom.{largeur:g}x{hauteur:g}mm"


def _fichier_pdf_etiquettes() -> str:
    """Chemin d'un PDF d'étiquettes temporaire, après purge des planches de plus d'une heure."""
    import tempfile
    import time

    dossier = os.path.join(tempfile.gettempdir(), "kodo_etiquettes")
    os.makedirs(dossier, exist_ok=True)

    # Le PDF envoyé au spouleur est lu de façon ASYNCHRONE par le worker : il ne peut pas être
    # supprimé à la fin de la requête. On purge donc les planches de la session précédente.
    limite = time.time() - 3600
    try:
        for nom in os.listdir(dossier):
            chemin = os.path.join(dossier, nom)
            try:
                if os.path.isfile(chemin) and os.path.getmtime(chemin) < limite:
                    os.remove(chemin)
            except OSError:
                pass
    except OSError:
        pass

    return os.path.join(dossier, f"etiquettes_{uuid.uuid4().hex}.pdf")


def _generer_pdf_etiquettes(lignes: List[Dict[str, Any]], reglages: Dict[str, Any],
                            shop_name: Optional[str]) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Produit le PDF d'étiquettes et le nom de format CUPS correspondant.

    Propage `BarcodeTropEtroitError` : quand le support est trop étroit pour un EAN-13 conforme,
    mieux vaut refuser que livrer à la commerçante des étiquettes que sa douchette ne lira pas.
    """
    from kodo_core.hardware.pdf import generer_etiquettes_lot_pdf

    params = _parametres_moteur_etiquette(reglages)
    rendu = generer_etiquettes_lot_pdf(
        lignes,
        _fichier_pdf_etiquettes(),
        shop_name=shop_name,
        **params
    )
    return rendu, _media_cups_etiquette(reglages, params)


def _nom_boutique() -> Optional[str]:
    """Nom de la boutique imprimé en tête d'étiquette, ou None s'il n'est pas renseigné."""
    try:
        from kodo_core.db.connection import get_connection
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT valeur FROM Parametres WHERE cle = 'shop_name'")
            row = cursor.fetchone()
        finally:
            conn.close()
        return (row[0].strip() or None) if row and row[0] else None
    except Exception:
        return None


def handle_products_request(method: str, path: str, query: Dict[str, Any], data: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, Any]]:
    """
    Gestionnaire de requêtes pour le catalogue, catégories, marques et stocks.
    """

    # 1. Liste des produits
    if method == "GET" and path == "/api/products":
        cat = query.get("category", [None])[0]
        brand = query.get("brand", [None])[0]
        search = query.get("search", [None])[0]
        products = InventoryManager.get_all_products(category=cat, brand=brand, search=search)
        return 200, products

    # 2. Ajout / Édition Produit
    elif method == "POST" and path == "/api/products":
        res = InventoryManager.save_product(data)
        return 200, {"success": True, "productId": res["product_id"]}

    # 3. Suppression Produit
    elif method == "DELETE" and path == "/api/products":
        prod_ids = query.get('id', [])
        if not prod_ids and 'id' in data:
            prod_ids = [str(data['id'])]
        if not prod_ids:
            return 400, {"error": "ID produit manquant"}

        InventoryManager.delete_product(int(prod_ids[0]))
        return 200, {"success": True}

    # 3b. Mise à jour en masse des seuils d'alerte
    elif method == "POST" and path == "/api/products/bulk-alert":
        raw_ids = data.get("product_ids") or data.get("ids") or []
        pids = []
        for rid in raw_ids:
            try:
                pids.append(int(rid))
            except (ValueError, TypeError):
                pass

        raw_thresh = data.get("alertStock") if data.get("alertStock") is not None else (
            data.get("alert_stock") if data.get("alert_stock") is not None else data.get("alert_threshold")
        )
        if isinstance(raw_thresh, str) and not raw_thresh.strip():
            raw_thresh = None
        try:
            thresh = int(raw_thresh) if raw_thresh is not None else None
        except (ValueError, TypeError):
            thresh = None

        InventoryManager.bulk_update_alert_threshold(pids, thresh)
        return 200, {"success": True, "updated": len(pids)}

    # 3c. Attribution d'un code-barres à un article (génération interne ou code imposé)
    elif method == "POST" and path == "/api/products/barcode":
        raw_pid = data.get("product_id") or data.get("productId") or data.get("id")
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            return 400, {"error": "Aucun article n'a été désigné pour recevoir un code-barres."}

        attribuer = getattr(InventoryManager, "assign_barcode", None)
        try:
            from kodo_core.domain.catalog.inventory_manager import BarcodeConflictError
        except ImportError:
            attribuer = None
        if attribuer is None:
            return 503, {
                "error": "La génération de codes-barres n'est pas disponible dans cette version de l'application.",
                "code": "BARCODE_ENGINE_UNAVAILABLE"
            }

        try:
            res = attribuer(
                pid,
                barcode=data.get("barcode") or data.get("code_barre"),
                overwrite=bool(data.get("overwrite"))
            )
        except BarcodeConflictError as ce:
            # 409 et non 400 : l'écran doit pouvoir proposer « remplacer quand même » sans avoir à
            # deviner la cause. Un code déjà imprimé et collé sur la marchandise ne se remplace pas
            # à la légère.
            return 409, {
                "error": str(ce),
                "code": getattr(ce, "code", "BARCODE_CONFLICT"),
                "conflict": getattr(ce, "details", None)
            }
        except LookupError:
            return 404, {"error": "Cet article n'existe plus dans le catalogue."}
        except ValueError as ve:
            return 422, {"error": str(ve), "code": "BARCODE_INVALID"}
        except Exception as e:
            # Filet de sécurité : une panne technique (base verrouillée, table absente) ne doit
            # jamais remonter un message SQL brut sur l'écran de la commerçante.
            print(f"[CODE-BARRES ATTRIBUTION ERREUR] produit={pid} : {e}")
            return 500, {
                "error": "Le code-barres n'a pas pu être enregistré. Réessayez dans un instant.",
                "detail": str(e)
            }

        return 200, {
            "success": True,
            "product_id": pid,
            "barcode": res.get("barcode"),
            "generated": res.get("generated"),
            "previous_barcode": res.get("previous_barcode")
        }

    # 3d. Résolution d'un code-barres scanné en article (source d'autorité serveur)
    elif method == "GET" and path == "/api/products/barcode/resolve":
        code = (query.get("code") or query.get("barcode") or [None])[0]
        if not code or not str(code).strip():
            return 400, {"error": "Aucun code-barres à rechercher."}

        try:
            produit = InventoryManager.get_product_by_barcode(str(code).strip())
        except Exception as e:
            print(f"[CODE-BARRES RESOLUTION ERREUR] code={code!r} : {e}")
            return 500, {"error": "La recherche par code-barres est momentanément indisponible."}

        if not produit:
            return 404, {
                "error": f"Aucun article ne porte le code-barres {str(code).strip()}.",
                "code": "BARCODE_UNKNOWN"
            }
        return 200, {"success": True, "product": produit}

    # 3e. Aperçu / téléchargement des étiquettes en PDF (synchrone : aucun matériel n'est sollicité).
    #     Le format du support est celui des réglages, même sans étiqueteuse branchée : une planche
    #     produite « au format par défaut » puis imprimée ailleurs donnerait des codes illisibles.
    elif method == "POST" and path == "/api/products/labels/pdf":
        from kodo_core.api.routes.system_routes import lire_reglages_etiquette
        from kodo_core.hardware.pdf import BarcodeTropEtroitError

        reglages = lire_reglages_etiquette()
        if not str(reglages.get("label_width_mm") or "").strip() or \
           not str(reglages.get("label_height_mm") or "").strip():
            return 409, {
                "success": False,
                "error": "Indiquez la taille de l'étiquette (largeur et hauteur en millimètres) "
                         "dans les réglages avant de générer les étiquettes.",
                "code": "LABEL_FORMAT_NOT_CONFIGURED"
            }

        try:
            lignes = _resoudre_lignes_etiquettes(data.get("items") or [])
        except ValueError as ve:
            return 422, {"success": False, "error": str(ve), "code": "LABEL_REQUEST_INVALID"}
        except Exception as e:
            print(f"[ETIQUETTE PREPARATION ERREUR] {e}")
            return 500, {
                "success": False,
                "error": "La liste des étiquettes n'a pas pu être préparée. Réessayez dans un instant."
            }

        try:
            rendu, _media = _generer_pdf_etiquettes(lignes, reglages, _nom_boutique())
        except BarcodeTropEtroitError as be:
            # 400 et non 422 : la demande est légitime, c'est le réglage d'étiquette qui est
            # à corriger. Le message du moteur est déjà rédigé pour la commerçante.
            return 400, {"success": False, "error": str(be), "code": "LABEL_TOO_NARROW"}
        except Exception as e:
            print(f"[ETIQUETTE PDF ERREUR] {e}")
            return 500, {
                "success": False,
                "error": "Les étiquettes n'ont pas pu être générées.",
                "detail": str(e)
            }

        chemin = rendu.get("path")
        try:
            with open(chemin, "rb") as fichier:
                pdf_bytes = fichier.read()
        finally:
            try:
                os.remove(chemin)
            except OSError:
                pass

        return 200, pdf_bytes, {
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="Etiquettes_Kodo.pdf"',
            "Content-Length": str(len(pdf_bytes))
        }

    # 3f. Impression des étiquettes code-barres (asynchrone via le spouleur, comme la réimpression
    #     d'un ticket de vente : un rouleau de 40 étiquettes ne doit jamais figer le comptoir).
    elif method == "POST" and path == "/api/products/labels/print":
        from kodo_core.api.routes.system_routes import lire_reglages_etiquette
        from kodo_core.hardware.pdf import BarcodeTropEtroitError

        reglages = lire_reglages_etiquette()
        if not reglages["est_configuree"]:
            return 409, {
                "success": False,
                "error": reglages["message"],
                "code": "LABEL_PRINTER_NOT_CONFIGURED"
            }

        try:
            lignes = _resoudre_lignes_etiquettes(data.get("items") or [])
        except ValueError as ve:
            return 422, {"success": False, "error": str(ve), "code": "LABEL_REQUEST_INVALID"}
        except Exception as e:
            print(f"[ETIQUETTE PREPARATION ERREUR] {e}")
            return 500, {
                "success": False,
                "error": "La liste des étiquettes n'a pas pu être préparée. Réessayez dans un instant."
            }

        try:
            rendu, media = _generer_pdf_etiquettes(lignes, reglages, _nom_boutique())
        except BarcodeTropEtroitError as be:
            return 400, {"success": False, "error": str(be), "code": "LABEL_TOO_NARROW"}
        except Exception as e:
            print(f"[ETIQUETTE PDF ERREUR] {e}")
            return 500, {
                "success": False,
                "error": "Les étiquettes n'ont pas pu être générées.",
                "detail": str(e)
            }

        try:
            from kodo_core.hardware.print_worker import get_print_worker
            worker = get_print_worker()
            # Jamais la file CUPS par défaut : sur un poste de vente, c'est l'imprimante à tickets.
            job = worker.enqueue_label_print(
                rendu.get("path"),
                printer_name=reglages["label_printer_name"],
                media=media,
                copies=1
            )
            etat = worker.get_label_circuit_status()
            return 200, {
                "success": True,
                "print_job_id": job.job_id,
                "status": job.status,
                "labels_count": sum(ligne["quantity"] for ligne in lignes),
                "pages": rendu.get("pages"),
                "warnings": rendu.get("avertissements") or [],
                "printer_available": etat.get("is_available", True),
                "printer_state": etat.get("state")
            }
        except Exception as pe:
            print(f"[ETIQUETTE PRINT WARNING] {pe}")
            return 500, {
                "success": False,
                "error": "Les étiquettes n'ont pas pu être envoyées à l'étiqueteuse.",
                "detail": str(pe)
            }

    # 4. Liste des catégories
    elif method == "GET" and path == "/api/categories":
        cats = InventoryManager.get_categories()
        return 200, cats

    # 5. Ajouter Catégorie
    elif method == "POST" and path == "/api/categories":
        name = data.get('name') or data.get('nom')
        if name:
            InventoryManager.add_category(name)
        return 200, {"success": True}

    # 6. Supprimer Catégorie
    elif method == "DELETE" and path == "/api/categories":
        names = query.get('name', [])
        if not names and 'name' in data:
            names = [str(data['name'])]
        if not names:
            return 400, {"error": "Nom catégorie manquant"}

        InventoryManager.delete_category(names[0])
        return 200, {"success": True}

    # 7. Liste des marques
    elif method == "GET" and path == "/api/brands":
        brands = InventoryManager.get_brands()
        return 200, brands

    # 8. Ajouter Marque
    elif method == "POST" and path == "/api/brands":
        name = data.get('name') or data.get('nom')
        if name:
            InventoryManager.add_brand(name)
        return 200, {"success": True}

    # 9. Alertes Stock Bas
    elif method == "GET" and path == "/api/stock/alerts":
        alerts = InventoryManager.get_low_stock_alerts()
        return 200, alerts

    return None
