"""
Module d'exportation comptable pour Mon Commerce.
Toutes les requêtes SQL utilisent des prepared statements.
"""
import csv
import datetime
import os
import json
import re
import unicodedata
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from database_manager import get_connection
from kodo_core.sync.shopify_sku import est_sans_taille as _est_sans_taille, sku_variante

def get_export_dir() -> str:
    """
    Retourne le chemin absolu du dossier d'exportation garanti inscriptible
    (support macOS App Bundle .app, Finder, Windows %APPDATA%, et Linux).
    """
    candidates = []
    try:
        from database_manager import data_path
        candidates.append(data_path("Exports"))
    except Exception:
        pass
    try:
        from kodo_core.config import ShopConfig
        candidates.append(os.path.join(ShopConfig.get_base_data_dir(), "Exports"))
    except Exception:
        pass
    candidates.append(os.path.expanduser("~/Documents/Kodo_POS/Exports"))
    candidates.append(os.path.expanduser("~/Library/Application Support/Kodo_POS/Exports"))
    candidates.append(os.path.join(tempfile.gettempdir(), "Kodo_POS_Exports"))
    candidates.append(tempfile.gettempdir())

    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            test_probe = os.path.join(path, f".write_test_{os.getpid()}")
            with open(test_probe, "wb") as f:
                f.write(b"1")
            if os.path.exists(test_probe):
                os.remove(test_probe)
            return path
        except Exception:
            continue

    return tempfile.gettempdir()


EXPORT_DIR = get_export_dir()

def _ensure_dir():
    global EXPORT_DIR
    EXPORT_DIR = get_export_dir()
    os.makedirs(EXPORT_DIR, exist_ok=True)
    return EXPORT_DIR

# ---------------------------------------------------------------------------
# Écritures comptables : règlements par moyen, TVA par taux, bons cadeaux
# ---------------------------------------------------------------------------

# Comptes par défaut (plan comptable belge usuel). Chacun est remplaçable par un paramètre
# `compta_compte_<nom>` (table Parametres) : l'expert-comptable de la boutique impose parfois les siens.
COMPTES_DEFAUT = {
    "caisse": "570000",  # règlements en espèces
    "banque": "580000",  # carte bancaire, QR, virement, commandes web
    "ventes": "700000",
    "tva": "451000",
    "bons": "460000",    # dette envers les clientes : bons cadeaux vendus et avoirs non utilisés
}
TAUX_TVA_USUELS = (Decimal("0"), Decimal("0.06"), Decimal("0.12"), Decimal("0.21"))
_DEUX_DECIMALES = Decimal("0.01")


def _comptes_export(c):
    """Comptes des écritures : valeurs par défaut, remplacées par les paramètres `compta_compte_*` valides."""
    comptes = dict(COMPTES_DEFAUT)
    try:
        c.execute("SELECT cle, valeur FROM Parametres WHERE cle LIKE 'compta_compte_%'")
        for cle, valeur in c.fetchall():
            nom = str(cle)[len("compta_compte_"):]
            numero = str(valeur or "").strip()
            if nom in comptes and re.fullmatch(r"[0-9]{3,10}", numero):
                comptes[nom] = numero
    except Exception:
        pass
    return comptes


def _arrondi(valeur):
    return Decimal(str(valeur or 0)).quantize(_DEUX_DECIMALES, rounding=ROUND_HALF_UP)


def _montant_csv(valeur):
    return f"{_arrondi(valeur):.2f}".replace(".", ",")


def _code_tva(taux):
    """Taux (0.21) -> code de la ligne de vente (« 21 »). 0.06 -> « 06 », exonéré -> « 00 »."""
    pourcent = (Decimal(str(taux)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(pourcent):02d}"


def _methode_lue(methode):
    """« REMB (Avoir) » (ticket de remboursement) se lit « Avoir » ; toute autre valeur est gardée telle quelle."""
    texte = str(methode or "").strip()
    interieur = re.search(r"\(([^)]*)\)", texte)
    if texte.upper().startswith("REMB") and interieur:
        return interieur.group(1).strip()
    return texte


def _classe_reglement(methode):
    """'especes' | 'carte' | 'qr' | 'avoir' (même classification que la clôture Z)."""
    from database_manager import classer_moyen_paiement
    return classer_moyen_paiement(_methode_lue(methode))


def _compte_reglement(classe, comptes):
    if classe == "especes":
        return comptes["caisse"]
    if classe == "avoir":
        return comptes["bons"]
    return comptes["banque"]


def _lignes_reglement(c, numero_ticket, total_tvac, methode_ticket):
    """[(classe, libellé, montant)] : règlements d'un ticket, tous montants positifs.

    Relus dans le journal de caisse signé (un par moyen réellement utilisé), puis ramenés au total
    du ticket : le rendu de monnaie et l'arrondi légal de 5 centimes sont portés par les espèces,
    à défaut par le règlement le plus gros. L'export lisait seulement `Tickets.methode_paiement`,
    qui ne garde que le PREMIER moyen : un ticket réglé 30 € CB + 41,10 € espèces passait en entier
    en carte, et un règlement par bon (avoir) était posté sur la banque."""
    cible = _arrondi(abs(Decimal(str(total_tvac or 0))))
    c.execute(
        "SELECT methode_paiement, montant FROM Ledger_Caisse "
        "WHERE reference = ? AND type_mouvement IN ('VENTE', 'REMBOURSEMENT') ORDER BY id",
        (numero_ticket,),
    )
    lignes = []
    for methode, montant in c.fetchall():
        m = _arrondi(abs(Decimal(str(montant or 0))))
        if m > 0:
            lignes.append([_classe_reglement(methode), _methode_lue(methode) or "Inconnu", m])

    def _repli():
        return [[_classe_reglement(methode_ticket), _methode_lue(methode_ticket) or "Inconnu", cible]]

    if not lignes:
        lignes = _repli()
    ecart = cible - sum((m for _c, _l, m in lignes), Decimal("0.00"))
    if ecart != 0:
        indice = next((i for i, ligne in enumerate(lignes) if ligne[0] == "especes"), None)
        if indice is None:
            indice = max(range(len(lignes)), key=lambda i: lignes[i][2])
        lignes[indice][2] += ecart
        if lignes[indice][2] <= 0:
            lignes = _repli()

    regroupees = {}
    for classe, libelle, m in lignes:
        regroupees[(classe, libelle)] = regroupees.get((classe, libelle), Decimal("0.00")) + m
    return [(classe, libelle, m) for (classe, libelle), m in regroupees.items() if m > 0]


def _ventiler_tva_ticket(c, ticket_id, total_tvac, total_htva):
    """[(taux, htva, tva, tvac)] par taux de TVA ; la somme égale EXACTEMENT les totaux du ticket.

    `Ventes_Details.prix_unitaire_tvac` garde le prix AVANT remise globale : chaque taux est pondéré
    par le rapport total net / total brut du ticket (même méthode que la clôture Z), puis le résidu
    d'arrondi est reporté sur le taux le plus important, jamais l'inverse (le ticket scellé fait foi)."""
    total_tvac = Decimal(str(total_tvac or 0))
    total_htva = Decimal(str(total_htva or 0))
    c.execute(
        """
        SELECT p.taux_tva, SUM(vd.quantite * vd.prix_unitaire_tvac)
        FROM Ventes_Details vd
        LEFT JOIN Stocks s ON s.id = vd.id_stock
        LEFT JOIN Produits p ON p.id = s.id_produit
        WHERE vd.id_ticket = ?
        GROUP BY p.taux_tva
        """,
        (ticket_id,),
    )
    bruts = {}
    for taux, brut in c.fetchall():
        taux_dec = Decimal(str(taux)) if taux is not None else Decimal("0.21")
        bruts[taux_dec] = bruts.get(taux_dec, Decimal("0")) + Decimal(str(brut or 0))
    somme_brute = sum(bruts.values(), Decimal("0"))

    if not bruts or somme_brute == 0:
        # Ticket sans ligne exploitable : un seul groupe, au taux usuel le plus proche des totaux.
        taux = Decimal("0.21")
        if total_htva != 0:
            implicite = (total_tvac - total_htva) / total_htva
            taux = min(TAUX_TVA_USUELS, key=lambda t: abs(t - implicite))
        htva = _arrondi(total_htva) if total_htva != 0 else _arrondi(total_tvac / (Decimal("1") + taux))
        tvac = _arrondi(total_tvac)
        return [(taux, htva, tvac - htva, tvac)]

    rapport = total_tvac / somme_brute
    groupes = {taux: _arrondi(brut * rapport) for taux, brut in bruts.items()}
    plus_gros = max(groupes, key=lambda t: abs(groupes[t]))
    groupes[plus_gros] += _arrondi(total_tvac) - sum(groupes.values(), Decimal("0"))

    htvas = {taux: _arrondi(tvac / (Decimal("1") + taux)) for taux, tvac in groupes.items()}
    if total_htva != 0 or total_tvac == 0:
        plus_gros_h = max(htvas, key=lambda t: abs(groupes[t]))
        htvas[plus_gros_h] += _arrondi(total_htva) - sum(htvas.values(), Decimal("0"))
    return [(taux, htvas[taux], groupes[taux] - htvas[taux], groupes[taux]) for taux in sorted(groupes)]


def export_comptable_belge():
    """
    Exporte un CSV normé comptabilité belge, une ligne par article vendu ou remboursé.
    Colonnes : Date, Numéro Ticket, Code Article, Nom Article,
               Montant TVAC, Base HTVA, Montant TVA, Moyen de Paiement.
    Retourne le chemin du fichier créé ou lève une exception.

    Les montants sont ceux réellement payés : `prix_unitaire_tvac` garde le prix avant remise
    globale, chaque ligne est donc pondérée par le rapport total net / total brut de son ticket
    (un retour sur un ticket remisé se lit ainsi au prix rendu, pas au prix affiché)."""
    conn = get_connection()
    c = conn.cursor()
    # Jointure Tickets × Ventes_Details × Produits via Stocks
    c.execute("""
        SELECT
            t.date_heure,
            t.numero_ticket,
            p.code_barre,
            p.nom,
            COALESCE(vd.quantite, 1),
            vd.prix_unitaire_tvac,
            COALESCE(p.taux_tva, 0.21),
            t.methode_paiement,
            t.id,
            t.total_tvac
        FROM Tickets t
        JOIN Ventes_Details vd ON vd.id_ticket = t.id
        JOIN Stocks s          ON s.id = vd.id_stock
        JOIN Produits p        ON p.id = s.id_produit
        ORDER BY t.date_heure DESC
    """)
    rows = c.fetchall()

    brut_par_ticket = {}
    for _d, _n, _c, _nom, qte, pu_tvac, _taux, _m, t_id, _tot in rows:
        brut_par_ticket[t_id] = brut_par_ticket.get(t_id, Decimal("0")) + (
            Decimal(str(pu_tvac or 0)) * Decimal(str(qte or 1)))

    moyens = {}

    def _moyen_de_paiement(t_id, numero, total, methode):
        if numero not in moyens:
            libelles = [libelle for _classe, libelle, _m in _lignes_reglement(c, numero, total, methode)]
            texte = " + ".join(libelles) if libelles else str(methode or "")
            moyens[numero] = f"REMB ({texte})" if Decimal(str(total or 0)) < 0 else texte
        return moyens[numero]

    records = []
    for date_heure, ticket, code, nom, qte, pu_tvac, taux, methode, t_id, total_net in rows:
        qte_d  = Decimal(str(qte or 1))
        pu_d   = Decimal(str(pu_tvac or 0))
        brut   = brut_par_ticket.get(t_id, Decimal("0"))
        rapport = (Decimal(str(total_net)) / brut) if (total_net is not None and brut != 0) else Decimal("1")
        tvac_d = (pu_d * qte_d * rapport).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        taux_d = Decimal(str(taux or 0.21))
        htva   = (tvac_d / (Decimal("1") + taux_d)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tva    = (tvac_d - htva).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        records.append({
            "Date":              date_heure,
            "Numéro Ticket":     ticket,
            "Code Article":      code,
            "Nom Article":       nom,
            "Quantité":          str(int(qte_d)),
            "Prix Unitaire TVAC (€)": str(pu_d.quantize(Decimal("0.01"))).replace(".", ","),
            "Montant TVAC (€)":  str(tvac_d).replace(".", ","),
            "Base HTVA (€)":     str(htva).replace(".", ","),
            "Montant TVA (€)":   str(tva).replace(".", ","),
            "Taux TVA":          f"{float(taux_d)*100:.0f}%",
            "Moyen de Paiement": _moyen_de_paiement(t_id, ticket, total_net, methode),
        })
    conn.close()

    _ensure_dir()
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EXPORT_DIR, f"export_comptable_{ts}.csv")
    fieldnames = [
        "Date", "Numéro Ticket", "Code Article", "Nom Article", "Quantité",
        "Prix Unitaire TVAC (€)", "Montant TVAC (€)", "Base HTVA (€)", "Montant TVA (€)", "Taux TVA", "Moyen de Paiement"
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(records)
    return path


def export_winbooks_csv(mois=None, annee=None):
    """
    Exporte un CSV formaté pour les logiciels comptables belges (ex: WinBooks, Exact).
    Écritures en partie double (Débit/Crédit), comptes par défaut dans `COMPTES_DEFAUT`.

    - Vente : débit des règlements (espèces -> caisse, carte/QR/web -> banque, bon ou avoir utilisé ->
      dette « bons »), crédit des ventes HTVA et de la TVA, UNE ligne par taux (code TVA = le taux).
    - Retour / avoir : écriture inverse, en montants POSITIFS (débit des ventes et de la TVA à
      régulariser, crédit du moyen de remboursement : caisse, banque, ou dette « bons » pour un avoir).
    - Bon cadeau vendu : débit caisse/banque, crédit dette « bons », sans TVA (elle est due à
      l'utilisation du bon, sur le ticket qui le consomme).
    L'arrondi légal de 5 centimes n'est pas ventilé : les règlements sont ramenés au total du ticket."""
    conn = get_connection()
    c = conn.cursor()
    comptes = _comptes_export(c)

    filtre, params = "", []
    if mois and annee:
        filtre = "strftime('%m', date_heure) = ? AND strftime('%Y', date_heure) = ?"
        params = [f"{int(mois):02d}", str(annee)]

    c.execute(
        "SELECT id, numero_ticket, date_heure, total_tvac, total_htva, total_tva, methode_paiement FROM Tickets"
        + (" WHERE " + filtre if filtre else "") + " ORDER BY date_heure ASC, id ASC",
        params,
    )
    tickets = c.fetchall()

    c.execute(
        "SELECT id, date_heure, methode_paiement, montant, reference FROM Ledger_Caisse "
        "WHERE type_mouvement = 'VENTE_BON'" + (" AND " + filtre if filtre else "") + " ORDER BY date_heure ASC, id ASC",
        params,
    )
    bons = c.fetchall()

    ecritures = []  # (date_heure, ordre, enregistrement)

    def ecrire(ordre, date_heure, document, compte, libelle, sens, montant, code_tva=""):
        montant = _arrondi(montant)
        if montant == 0:
            return
        date_str = str(date_heure)[:10]
        ecritures.append((str(date_heure), ordre, {
            "Code Journal": "VEN",
            "Période": date_str[:7].replace("-", ""),
            "Date": date_str[8:10] + "/" + date_str[5:7] + "/" + date_str[:4],
            "Document": document,
            "Compte": compte,
            "Libellé": libelle,
            "Sens": sens,
            "Montant": _montant_csv(montant),
            "Code TVA": code_tva,
        }))

    for t_id, numero, date_heure, tvac, htva, tva, methode in tickets:
        total = Decimal(str(tvac or 0))
        if total == 0:
            continue
        ordre = (0, t_id)
        vente = total > 0
        for classe, libelle, montant in _lignes_reglement(c, numero, total, methode):
            compte = _compte_reglement(classe, comptes)
            if vente:
                texte = "Règlement par bon / avoir (dette)" if classe == "avoir" else f"Recette {libelle}"
                ecrire(ordre, date_heure, numero, compte, texte, "D", montant)
            else:
                texte = "Avoir remis à la cliente (dette)" if classe == "avoir" else f"Remboursement {libelle}"
                ecrire(ordre, date_heure, numero, compte, texte, "C", montant)
        for taux, htva_g, tva_g, _tvac_g in _ventiler_tva_ticket(c, t_id, total, htva):
            code = _code_tva(taux) if abs(tva_g) > 0 or abs(htva_g) > 0 else "00"
            pourcent = (Decimal(str(taux)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if vente:
                ecrire(ordre, date_heure, numero, comptes["ventes"], "Vente Marchandises", "C", abs(htva_g), code)
                ecrire(ordre, date_heure, numero, comptes["tva"], f"TVA à payer {pourcent}%", "C", abs(tva_g))
            else:
                ecrire(ordre, date_heure, numero, comptes["ventes"], "Retour marchandises (avoir client)",
                       "D", abs(htva_g), code)
                ecrire(ordre, date_heure, numero, comptes["tva"], f"TVA à régulariser (avoir) {pourcent}%",
                       "D", abs(tva_g))

    for b_id, date_heure, methode, montant, code_bon in bons:
        classe = _classe_reglement(methode)
        ordre = (1, b_id)
        ecrire(ordre, date_heure, code_bon, _compte_reglement(classe, comptes),
               f"Bon cadeau vendu ({_methode_lue(methode) or 'Inconnu'})", "D", abs(Decimal(str(montant or 0))))
        ecrire(ordre, date_heure, code_bon, comptes["bons"],
               "Bon cadeau vendu (dette envers la cliente)", "C", abs(Decimal(str(montant or 0))))

    conn.close()
    ecritures.sort(key=lambda e: (e[0], e[1]))
    records = [e[2] for e in ecritures]

    _ensure_dir()
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EXPORT_DIR, f"export_winbooks_{ts}.csv")
    fieldnames = [
        "Code Journal", "Période", "Date", "Document", "Compte", "Libellé", "Sens", "Montant", "Code TVA"
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(records)
    return path


def export_synthese_gerant(comptage_details=None):
    """
    Génère un fichier .xlsx de clôture journalière.
    Feuilles :
      1. Synthèse  — CA, répartition par méthode de paiement
      2. Top 5     — 5 articles les plus vendus du jour
      3. Détail    — Toutes les ventes du jour
    Retourne le chemin du fichier créé ou lève une exception.
    """
    today = datetime.date.today().isoformat()
    conn  = get_connection()
    c     = conn.cursor()

    # ── CA et répartition par méthode
    c.execute("""
        SELECT methode_paiement,
               COUNT(*)              AS nb_tickets,
               SUM(total_tvac)       AS total_tvac,
               SUM(total_htva)       AS total_htva,
               SUM(total_tva)        AS total_tva
        FROM Tickets
        WHERE date(date_heure) = ?
        GROUP BY methode_paiement
    """, (today,))
    repartition = c.fetchall()

    # ── Top 5 articles du jour
    c.execute("""
        SELECT p.nom, p.categorie, SUM(vd.quantite) AS qte_vendue,
               SUM(vd.prix_unitaire_tvac * vd.quantite) AS ca_genere
        FROM Ventes_Details vd
        JOIN Stocks s   ON s.id = vd.id_stock
        JOIN Produits p ON p.id = s.id_produit
        JOIN Tickets t  ON t.id = vd.id_ticket
        WHERE date(t.date_heure) = ?
        GROUP BY p.id
        ORDER BY qte_vendue DESC
        LIMIT 5
    """, (today,))
    top5 = c.fetchall()

    # ── Détail complet du jour
    c.execute("""
        SELECT t.date_heure, t.numero_ticket, p.nom, p.code_barre,
               vd.prix_unitaire_tvac, t.methode_paiement
        FROM Tickets t
        JOIN Ventes_Details vd ON vd.id_ticket = t.id
        JOIN Stocks s          ON s.id = vd.id_stock
        JOIN Produits p        ON p.id = s.id_produit
        WHERE date(t.date_heure) = ?
        ORDER BY t.date_heure
    """, (today,))
    detail = c.fetchall()
    conn.close()

    _ensure_dir()
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(EXPORT_DIR, f"cloture_journaliere_{ts}.xlsx")

    import openpyxl
    wb = openpyxl.Workbook()

    # Feuille 1 — Synthèse
    ws1 = wb.active
    ws1.title = "Synthèse"
    ws1.append(["CLÔTURE DE CAISSE — KŌDO POS"])
    ws1.append([f"Date : {datetime.date.today().strftime('%d/%m/%Y')}"])
    ws1.append([])
    headers1 = ["Méthode", "Nb Tickets", "Total TVAC (€)", "Total HTVA (€)", "Total TVA (€)"]
    ws1.append(headers1)

    tot_nb = 0
    tot_tvac = Decimal("0.00")
    tot_htva = Decimal("0.00")
    tot_tva = Decimal("0.00")
    for row in repartition:
        meth = row[0] or "Autre"
        nb = row[1] or 0
        tvac = Decimal(str(row[2] or 0))
        htva = Decimal(str(row[3] or 0))
        tva = Decimal(str(row[4] or 0))
        tot_nb += nb
        tot_tvac += tvac
        tot_htva += htva
        tot_tva += tva
        ws1.append([meth, nb, float(tvac), float(htva), float(tva)])
    ws1.append(["TOTAL", tot_nb, float(tot_tvac), float(tot_htva), float(tot_tva)])
    _style_header(ws1, row=4, ncols=5)

    # Feuille 2 — Top 5
    ws2 = wb.create_sheet(title="Top 5 Articles")
    ws2.append(["Article", "Catégorie", "Qté Vendue", "CA Généré (€)"])
    for row in top5:
        ws2.append([row[0], row[1], row[2], float(Decimal(str(row[3] or 0)))])
    _style_header(ws2, row=1, ncols=4)

    # Feuille 3 — Détail
    ws3 = wb.create_sheet(title="Détail Ventes")
    ws3.append(["Date/Heure", "Ticket", "Article", "Code Barre", "Prix TVAC (€)", "Paiement"])
    for row in detail:
        ws3.append([row[0], row[1], row[2], row[3], float(Decimal(str(row[4] or 0))), row[5]])
    _style_header(ws3, row=1, ncols=6)

    # Feuille 4 — Comptage
    if comptage_details:
        records_comptage = []
        for val_str, qte in comptage_details.items():
            if qte > 0:
                val_num = float(val_str)
                records_comptage.append((val_num, f"{val_num:.2f} €" if val_num < 5 else f"{int(val_num)} €", qte, qte * val_num))
        if records_comptage:
            records_comptage.sort(key=lambda x: x[0], reverse=True)
            ws4 = wb.create_sheet(title="Comptage Caisse")
            ws4.append(["Dénomination", "Quantité", "Sous-Total (€)"])
            tot_c = 0.0
            for r in records_comptage:
                ws4.append([r[1], r[2], r[3]])
                tot_c += r[3]
            ws4.append(["TOTAL COMPTÉ", "", tot_c])
            _style_header(ws4, row=1, ncols=3)

    wb.save(path)
    return path


def _style_header(ws, row, ncols):
    """Applique un style minimal à la ligne d'en-tête."""
    try:
        from openpyxl.styles import Font, PatternFill, Alignment
        fill = PatternFill("solid", fgColor="1A1A1A")
        font = Font(bold=True, color="D4AF37")
        for col in range(1, ncols + 1):
            cell = ws.cell(row=row, column=col)
            cell.font  = fill and font
            cell.fill  = fill
            cell.alignment = Alignment(horizontal="center")
        # Largeur auto
        for col in ws.columns:
            max_len = max((len(str(c.value)) for c in col if c.value), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)
    except Exception:
        pass  # Style non critique


def sauvegarder_rapport_z_journalier(comptage_details=None):
    """
    Génère et stocke le rapport Z consolidé de la journée en cours
    dans la table immuable Rapports_Z au format JSON.
    """
    conn = get_connection()
    c = conn.cursor()
    
    # Date du jour (ou de la session courante)
    today = datetime.date.today().isoformat()
    
    # Informations de session
    c.execute("SELECT fond_caisse_matin, montant_compté_soir, montant_theorique_soir, ecart_caisse FROM Sessions_Caisse ORDER BY id DESC LIMIT 1")
    session_row = c.fetchone()
    fond_initial = float(session_row[0]) if session_row and session_row[0] is not None else 0.0
    solde_reel = float(session_row[1]) if session_row and session_row[1] is not None else 0.0
    solde_theo = float(session_row[2]) if session_row and session_row[2] is not None else 0.0
    ecart = float(session_row[3]) if session_row and session_row[3] is not None else 0.0
    
    # Sorties manuelles
    c.execute("SELECT COALESCE(SUM(montant), 0) FROM Depenses_Caisse WHERE date(date_heure) = ?", (today,))
    sorties_manuelles = float(c.fetchone()[0])
    
    # Récupération des tickets
    c.execute("SELECT id, numero_ticket, total_tvac, total_htva, total_tva, methode_paiement FROM Tickets WHERE date(date_heure) = ? ORDER BY id ASC", (today,))
    tickets = c.fetchall()
    
    premier_ticket = tickets[0][1] if tickets else ""
    dernier_ticket = tickets[-1][1] if tickets else ""
    
    ca_ttc_total = Decimal("0.00")
    ventilation_tva = {
        "0.21": {"base_htva": Decimal("0.00"), "montant_tva": Decimal("0.00"), "ttc": Decimal("0.00")},
        "0.06": {"base_htva": Decimal("0.00"), "montant_tva": Decimal("0.00"), "ttc": Decimal("0.00")}
    }
    
    repartition_paiements = {
        "Bancontact/Visa": Decimal("0.00"),
        "Espèces": Decimal("0.00"),
        "Cartes Cadeaux / Autres": Decimal("0.00"),
        "Remboursements": Decimal("0.00")
    }
    
    for t_id, num, tvac_val, htva_val, tva_val, methode in tickets:
        tvac_d = Decimal(str(tvac_val)) if tvac_val is not None else Decimal("0.00")
        ca_ttc_total += tvac_d
        
        # Mapping méthode de paiement
        m_str = str(methode)
        if "REMB" in m_str or tvac_d < 0:
            repartition_paiements["Remboursements"] += tvac_d
        elif "Bancontact" in m_str or "Visa" in m_str:
            repartition_paiements["Bancontact/Visa"] += tvac_d
        elif "Espèces" in m_str:
            repartition_paiements["Espèces"] += tvac_d
        else:
            repartition_paiements["Cartes Cadeaux / Autres"] += tvac_d
            
        # Détails des lignes pour ventiler la TVA avec précision
        c.execute("""
            SELECT vd.prix_unitaire_tvac, vd.quantite, p.taux_tva
            FROM Ventes_Details vd
            JOIN Stocks s ON s.id = vd.id_stock
            JOIN Produits p ON p.id = s.id_produit
            WHERE vd.id_ticket = ?
        """, (t_id,))
        lignes = c.fetchall()
        
        if lignes:
            total_brut_lignes = sum(Decimal(str(p)) * Decimal(str(q)) for p, q, _ in lignes)
            prorata = (tvac_d / total_brut_lignes) if (total_brut_lignes > Decimal("0.00") and tvac_d > Decimal("0.00") and total_brut_lignes != tvac_d) else Decimal("1.0")

            for p_tvac, qte, taux in lignes:
                t_taux = str(taux) if taux is not None else "0.21"
                if t_taux not in ventilation_tva:
                    ventilation_tva[t_taux] = {"base_htva": Decimal("0.00"), "montant_tva": Decimal("0.00"), "ttc": Decimal("0.00")}
                
                l_ttc = (Decimal(str(p_tvac)) * Decimal(str(qte)) * prorata).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                l_htva = (l_ttc / (Decimal("1") + Decimal(t_taux))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                l_tva = l_ttc - l_htva

                ventilation_tva[t_taux]["ttc"] += l_ttc
                ventilation_tva[t_taux]["base_htva"] += l_htva
                ventilation_tva[t_taux]["montant_tva"] += l_tva
        else:
            # Ticket sans lignes (ex. Remboursement direct)
            # On utilise le taux par défaut de 21%
            t_taux = "0.21"
            l_ttc = tvac_d
            l_htva = (l_ttc / Decimal("1.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            l_tva = l_ttc - l_htva
            
            ventilation_tva[t_taux]["ttc"] += l_ttc
            ventilation_tva[t_taux]["base_htva"] += l_htva
            ventilation_tva[t_taux]["montant_tva"] += l_tva
            
    # Construction de l'objet JSON Z
    objet_z = {
        "date": today,
        "sequence": {"premier_ticket": premier_ticket, "dernier_ticket": dernier_ticket},
        "financier": {
            "ca_ttc": float(ca_ttc_total),
            "tva": {k: {"base_htva": float(v["base_htva"]), "montant_tva": float(v["montant_tva"]), "ttc": float(v["ttc"])} for k, v in ventilation_tva.items()}
        },
        "paiements": {k: float(v) for k, v in repartition_paiements.items()},
        "mouvements_caisse": {
            "fond_initial": fond_initial,
            "sorties_manuelles": sorties_manuelles,
            "solde_theorique": solde_theo,
            "solde_reel": solde_reel,
            "ecart": ecart
        },
        "statut": {
            "caisse_juste": (abs(ecart) < 0.01)
        },
        "comptage_details": comptage_details or {}
    }
    
    donnees_json = json.dumps(objet_z, ensure_ascii=False)
    
    from database_manager import signer_rapport_z
    sig_z, hash_z = signer_rapport_z(c, today, donnees_json)
    
    # Ajoute les colonnes de signature dans la table
    c.execute("INSERT OR REPLACE INTO Rapports_Z (date, donnees_json, signature, hash_precedent) VALUES (?, ?, ?, ?)", (today, donnees_json, sig_z, hash_z))
    conn.commit()
    conn.close()
    return objet_z



def calculer_rapport_z_virtuel(date_str, c):
    c.execute("SELECT fond_caisse_matin FROM Sessions_Caisse WHERE date(date_ouverture) <= ? ORDER BY id DESC LIMIT 1", (date_str,))
    session_row = c.fetchone()
    fond_initial = float(session_row[0]) if session_row and session_row[0] is not None else 0.0
    
    c.execute("SELECT COALESCE(SUM(montant), 0) FROM Depenses_Caisse WHERE date(date_heure) = ?", (date_str,))
    sorties_manuelles = float(c.fetchone()[0])
    
    c.execute("SELECT id, numero_ticket, total_tvac, total_htva, total_tva, methode_paiement FROM Tickets WHERE date(date_heure) = ? ORDER BY id ASC", (date_str,))
    tickets = c.fetchall()
    
    if not tickets and sorties_manuelles == 0:
        return None
        
    premier_ticket = tickets[0][1] if tickets else ""
    dernier_ticket = tickets[-1][1] if tickets else ""
    
    from decimal import Decimal, ROUND_HALF_UP
    ca_ttc_total = Decimal("0.00")
    ventilation_tva = {
        "0.21": {"base_htva": Decimal("0.00"), "montant_tva": Decimal("0.00"), "ttc": Decimal("0.00")},
        "0.06": {"base_htva": Decimal("0.00"), "montant_tva": Decimal("0.00"), "ttc": Decimal("0.00")}
    }
    
    repartition_paiements = {
        "Bancontact/Visa": Decimal("0.00"),
        "Espèces": Decimal("0.00"),
        "Cartes Cadeaux / Autres": Decimal("0.00"),
        "Remboursements": Decimal("0.00")
    }
    
    for t_id, num, tvac_val, htva_val, tva_val, methode in tickets:
        tvac_d = Decimal(str(tvac_val)) if tvac_val is not None else Decimal("0.00")
        ca_ttc_total += tvac_d
        
        m_str = str(methode)
        if "REMB" in m_str or tvac_d < 0:
            repartition_paiements["Remboursements"] += tvac_d
        elif "Bancontact" in m_str or "Visa" in m_str or "QR" in m_str:
            repartition_paiements["Bancontact/Visa"] += tvac_d
        elif "Espèces" in m_str:
            repartition_paiements["Espèces"] += tvac_d
        else:
            repartition_paiements["Cartes Cadeaux / Autres"] += tvac_d
            
        c.execute("""
            SELECT vd.prix_unitaire_tvac, vd.quantite, p.taux_tva
            FROM Ventes_Details vd
            JOIN Stocks s ON s.id = vd.id_stock
            JOIN Produits p ON p.id = s.id_produit
            WHERE vd.id_ticket = ?
        """, (t_id,))
        lignes = c.fetchall()
        
        if lignes:
            for p_tvac, qte, taux in lignes:
                t_taux = str(taux) if taux is not None else "0.21"
                if t_taux not in ventilation_tva:
                    ventilation_tva[t_taux] = {"base_htva": Decimal("0.00"), "montant_tva": Decimal("0.00"), "ttc": Decimal("0.00")}
                
                l_ttc = Decimal(str(p_tvac)) * Decimal(str(qte))
                l_htva = (l_ttc / (Decimal("1") + Decimal(t_taux))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                l_tva = l_ttc - l_htva

                ventilation_tva[t_taux]["ttc"] += l_ttc
                ventilation_tva[t_taux]["base_htva"] += l_htva
                ventilation_tva[t_taux]["montant_tva"] += l_tva
        else:
            t_taux = "0.21"
            l_ttc = tvac_d
            l_htva = (l_ttc / Decimal("1.21")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            l_tva = l_ttc - l_htva
            
            ventilation_tva[t_taux]["ttc"] += l_ttc
            ventilation_tva[t_taux]["base_htva"] += l_htva
            ventilation_tva[t_taux]["montant_tva"] += l_tva
            
    return {
        "date": date_str,
        "sequence": {"premier_ticket": premier_ticket, "dernier_ticket": dernier_ticket},
        "financier": {
            "ca_ttc": float(ca_ttc_total),
            "tva": {k: {"base_htva": float(v["base_htva"]), "montant_tva": float(v["montant_tva"]), "ttc": float(v["ttc"])} for k, v in ventilation_tva.items()}
        },
        "paiements": {k: float(v) for k, v in repartition_paiements.items()},
        "mouvements_caisse": {
            "fond_initial": fond_initial,
            "sorties_manuelles": sorties_manuelles,
            "solde_theorique": float(Decimal(str(fond_initial)) + repartition_paiements["Espèces"] - Decimal(str(sorties_manuelles))),
            "solde_reel": float(Decimal(str(fond_initial)) + repartition_paiements["Espèces"] - Decimal(str(sorties_manuelles))),
            "ecart": 0.0
        },
        "statut": {"caisse_juste": True},
        "comptage_details": {}
    }


def export_comptable_mensuel(mois, annee, format_type="excel"):
    """
    Génère l'export mensuel agrégé pour l'expert-comptable
    en consommant les rapports Z archivés et en y ajoutant les jours en temps réel (virtuels)
    non encore clôturés.
    format_type: 'excel', 'csv' ou 'json'
    """
    conn = get_connection()
    c = conn.cursor()
    # Format mois sur 2 chiffres
    mois_str = f"{int(mois):02d}"
    annee_str = str(annee)
    
    c.execute("SELECT date, donnees_json FROM Rapports_Z WHERE strftime('%m', date) = ? AND strftime('%Y', date) = ? ORDER BY date ASC", (mois_str, annee_str))
    lignes_z = c.fetchall()
    
    # Check for unclosed tickets and expenses
    c.execute("SELECT DISTINCT date(date_heure) FROM Tickets WHERE strftime('%m', date_heure) = ? AND strftime('%Y', date_heure) = ?", (mois_str, annee_str))
    jours_tickets = [r[0] for r in c.fetchall()]
    
    c.execute("SELECT DISTINCT date(date_heure) FROM Depenses_Caisse WHERE strftime('%m', date_heure) = ? AND strftime('%Y', date_heure) = ?", (mois_str, annee_str))
    jours_depenses = [r[0] for r in c.fetchall()]
    
    jours_actifs = set(jours_tickets + jours_depenses)
    jours_avec_z = {date_z for date_z, _ in lignes_z}
    
    import json
    for jour in jours_actifs:
        if jour not in jours_avec_z:
            virtual_z = calculer_rapport_z_virtuel(jour, c)
            if virtual_z:
                lignes_z.append((jour, json.dumps(virtual_z)))
    
    lignes_z.sort(key=lambda x: x[0])
    conn.close()
    
    if not lignes_z:
        # Permettre l'exportation d'un rapport comptable à zéro même si aucun ticket n'a été émis
        virtual_z = {
            "financier": {"ca_ttc": 0.0, "tva": {}},
            "paiements": {},
            "mouvements_caisse": {"ecart": 0.0},
            "sequence": {"premier_ticket": "-", "dernier_ticket": "-"},
            "statut": {"caisse_juste": True}
        }
        lignes_z = [(f"{annee_str}-{mois_str}-01", json.dumps(virtual_z))]

        
    records = []
    totaux = {
        "ca_ttc": Decimal("0.00"),
        "htva_21": Decimal("0.00"),
        "tva_21": Decimal("0.00"),
        "htva_06": Decimal("0.00"),
        "tva_06": Decimal("0.00"),
        "bc_visa": Decimal("0.00"),
        "especes": Decimal("0.00"),
        "autres": Decimal("0.00"),
        "remb": Decimal("0.00"),
        "ecart": Decimal("0.00")
    }
    
    for date_z, json_str in lignes_z:
        data = json.loads(json_str)
        fin = data.get("financier", {})
        tva_dict = fin.get("tva", {})
        paiements = data.get("paiements", {})
        mouv = data.get("mouvements_caisse", {})
        
        t21 = tva_dict.get("0.21", {"base_htva": 0.0, "montant_tva": 0.0})
        t06 = tva_dict.get("0.06", {"base_htva": 0.0, "montant_tva": 0.0})
        
        c_ttc = Decimal(str(fin.get("ca_ttc", 0.0)))
        h21 = Decimal(str(t21["base_htva"]))
        tv21 = Decimal(str(t21["montant_tva"]))
        h06 = Decimal(str(t06["base_htva"]))
        tv06 = Decimal(str(t06["montant_tva"]))
        
        bc = Decimal(str(paiements.get("Bancontact/Visa", 0.0)))
        esp = Decimal(str(paiements.get("Espèces", 0.0)))
        aut = Decimal(str(paiements.get("Cartes Cadeaux / Autres", 0.0)))
        remb = Decimal(str(paiements.get("Remboursements", 0.0)))
        ec = Decimal(str(mouv.get("ecart", 0.0)))
        
        totaux["ca_ttc"] += c_ttc
        totaux["htva_21"] += h21
        totaux["tva_21"] += tv21
        totaux["htva_06"] += h06
        totaux["tva_06"] += tv06
        totaux["bc_visa"] += bc
        totaux["especes"] += esp
        totaux["autres"] += aut
        totaux["remb"] += remb
        totaux["ecart"] += ec
        
        seq = data.get("sequence", {})
        records.append({
            "Date": date_z,
            "Séquence Début": seq.get("premier_ticket", ""),
            "Séquence Fin": seq.get("dernier_ticket", ""),
            "CA Total TTC (€)": float(c_ttc),
            "Base HTVA 21% (€)": float(h21),
            "TVA 21% (€)": float(tv21),
            "Base HTVA 6% (€)": float(h06),
            "TVA 6% (€)": float(tv06),
            "Bancontact/Visa (€)": float(bc),
            "Espèces (€)": float(esp),
            "Autres Paiements (€)": float(aut),
            "Remboursements (€)": float(remb),
            "Écart de Caisse (€)": float(ec),
            "Statut": "Juste" if data.get("statut", {}).get("caisse_juste", False) else "Écart"
        })
        
    if format_type == "dict":
        return {
            "boutique": "Mon Commerce",
            "periode": f"{mois_str}/{annee_str}",
            "totaux": {k: float(v) for k, v in totaux.items()},
            "rapports_journaliers": records
        }
        
    if format_type == "json":
        res_obj = {
            "boutique": "Mon Commerce",
            "periode": f"{mois_str}/{annee_str}",
            "totaux": {k: float(v) for k, v in totaux.items()},
            "rapports_journaliers": records
        }
        _ensure_dir()
        path = os.path.join(EXPORT_DIR, f"export_comptable_{annee_str}_{mois_str}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res_obj, f, ensure_ascii=False, indent=2)
        return path
        
    # Ligne de total
    total_row = {
        "Date": "TOTAL MENSUEL",
        "Séquence Début": "",
        "Séquence Fin": "",
        "CA Total TTC (€)": float(totaux["ca_ttc"]),
        "Base HTVA 21% (€)": float(totaux["htva_21"]),
        "TVA 21% (€)": float(totaux["tva_21"]),
        "Base HTVA 6% (€)": float(totaux["htva_06"]),
        "TVA 6% (€)": float(totaux["tva_06"]),
        "Bancontact/Visa (€)": float(totaux["bc_visa"]),
        "Espèces (€)": float(totaux["especes"]),
        "Autres Paiements (€)": float(totaux["autres"]),
        "Remboursements (€)": float(totaux["remb"]),
        "Écart de Caisse (€)": float(totaux["ecart"]),
        "Statut": ""
    }
    
    _ensure_dir()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    headers = [
        "Date", "Séquence Début", "Séquence Fin", "CA Total TTC (€)", "Base HTVA 21% (€)",
        "TVA 21% (€)", "Base HTVA 6% (€)", "TVA 6% (€)", "Bancontact/Visa (€)", "Espèces (€)",
        "Autres Paiements (€)", "Remboursements (€)", "Écart de Caisse (€)", "Statut"
    ]
    
    if format_type == "csv":
        path = os.path.join(EXPORT_DIR, f"export_mensuel_{annee_str}_{mois_str}_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=headers, delimiter=";")
            writer.writeheader()
            for r in records:
                r_copy = {k: str(v).replace(".", ",") if isinstance(v, float) else v for k, v in r.items()}
                writer.writerow(r_copy)
            t_copy = {k: str(v).replace(".", ",") if isinstance(v, float) else v for k, v in total_row.items()}
            writer.writerow(t_copy)
        return path
    else:
        path = os.path.join(EXPORT_DIR, f"export_mensuel_{annee_str}_{mois_str}_{ts}.xlsx")
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Rapport Mensuel"
        ws.append(["RAPPORT COMPTABLE MENSUEL — KŌDO POS"])
        ws.append([f"Période : {mois_str}/{annee_str}"])
        ws.append([])
        ws.append(headers)
        for r in records:
            ws.append([r[h] for h in headers])
        ws.append([total_row[h] for h in headers])
        _style_header(ws, row=4, ncols=len(headers))
        wb.save(path)
        return path


# ---------------------------------------------------------------------------------------------
# EXPORT DU CATALOGUE POUR SHOPIFY (Format officiel CSV)
# ---------------------------------------------------------------------------------------------

SHOPIFY_PRODUCT_HEADERS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags",
    "Published", "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value",
    "Option3 Name", "Option3 Value", "Variant SKU", "Variant Grams",
    "Variant Inventory Tracker", "Variant Inventory Qty", "Variant Inventory Policy",
    "Variant Fulfillment Service", "Variant Price", "Variant Compare At Price",
    "Variant Requires Shipping", "Variant Taxable", "Variant Barcode", "Image Src",
    "Image Position", "Image Alt Text", "Gift Card", "SEO Title", "SEO Description",
    "Google Shopping / Google Product Category", "Google Shopping / Gender",
    "Google Shopping / Age Group", "Google Shopping / MPN", "Google Shopping / Condition",
    "Google Shopping / Custom Product", "Google Shopping / Custom Label 0",
    "Google Shopping / Custom Label 1", "Google Shopping / Custom Label 2",
    "Google Shopping / Custom Label 3", "Google Shopping / Custom Label 4",
    "Variant Image", "Variant Weight Unit", "Variant Tax Code", "Cost per item",
    "Included / United States", "Price / United States", "Compare At Price / United States",
    "Included / International", "Price / International", "Compare At Price / International",
    "Status"
]

def _slugify_shopify(text: str) -> str:
    """Transforme un titre en handle Shopify conforme (minuscules, tirets, sans caractères accentués)."""
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('utf-8')
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    return text.strip('-')

def export_shopify_catalog_csv(status: str = "active", output_path: str = None, conn=None) -> str:
    """
    Génère un fichier CSV 100% conforme au format officiel d'import produit de Shopify
    (Shopify Admin > Produits > Importer).
    
    Règles :
      - Un seul Handle par produit pour regrouper toutes ses déclinaisons (tailles).
      - Quantités en stock réelles avec suivi Shopify activé (Variant Inventory Tracker = 'shopify').
      - Gestion fine des prix : prix soldé dans 'Variant Price' et ancien prix barré dans 'Variant Compare At Price'.
      - Coût d'achat HTVA dans 'Cost per item' pour le calcul des marges sur Shopify.
      - Unicité des codes-barres : seul le premier variant ou variant unique porte le code-barres produit
        pour éviter le rejet Shopify 'Barcode has already been taken'.
      - Séparateur virgule et encodage UTF-8 avec BOM (utf-8-sig) pour compatibilité Excel et Shopify.
    """
    if status not in ("active", "draft"):
        status = "active"

    should_close = False
    if conn is None:
        conn = get_connection()
        should_close = True

    try:
        c = conn.cursor()
        c.execute("PRAGMA table_info(Produits)")
        cols_produits = {r[1].lower(): True for r in c.fetchall()}

        col_marque = "p.marque" if "marque" in cols_produits else "NULL as marque"
        col_achat = "p.prix_achat_htva" if "prix_achat_htva" in cols_produits else "NULL as prix_achat_htva"
        col_img = "p.image_path" if "image_path" in cols_produits else "NULL as image_path"
        col_solde = "p.en_solde" if "en_solde" in cols_produits else "0 as en_solde"
        col_prix_solde = "p.prix_solde_tvac" if "prix_solde_tvac" in cols_produits else "NULL as prix_solde_tvac"
        col_taux_tva = "p.taux_tva" if "taux_tva" in cols_produits else "0.21 as taux_tva"

        c.execute(f"""
            SELECT 
                p.id, 
                p.code_barre, 
                p.nom, 
                p.categorie, 
                {col_marque},
                p.prix_vente_tvac, 
                {col_achat}, 
                {col_taux_tva}, 
                {col_solde}, 
                {col_prix_solde},
                {col_img}
            FROM Produits p
            ORDER BY p.id ASC
        """)
        produits = c.fetchall()

        c.execute("""
            SELECT id_produit, taille, quantite_actuelle
            FROM Stocks
            ORDER BY id ASC
        """)
        stock_rows = c.fetchall()
        stocks_by_prod = {}
        for pid, taille, qte in stock_rows:
            if pid not in stocks_by_prod:
                stocks_by_prod[pid] = []
            stocks_by_prod[pid].append((taille, qte))

        rows = []
        for p_id, barcode, nom, cat, marque, prix, achat, taux_tva, en_solde, prix_solde, img_path in produits:
            nom_propre = (nom or f"Article #{p_id}").strip()
            handle_slug = _slugify_shopify(nom_propre)
            handle = f"{handle_slug}-{p_id}" if handle_slug else f"produit-{p_id}"

            variantes = stocks_by_prod.get(p_id, [])
            if not variantes:
                variantes = [("Taille Unique", 0)]

            # Calcul des prix décimaux avec point
            d_prix = None
            if prix is not None and str(prix).strip() != "":
                try:
                    d_prix = Decimal(str(prix).strip().replace(",", "."))
                except Exception:
                    d_prix = Decimal("0.00")
            else:
                d_prix = Decimal("0.00")

            d_solde = None
            if prix_solde is not None and str(prix_solde).strip() != "":
                try:
                    d_solde = Decimal(str(prix_solde).strip().replace(",", "."))
                except Exception:
                    d_solde = None

            d_achat = None
            if achat is not None and str(achat).strip() != "":
                try:
                    d_achat = Decimal(str(achat).strip().replace(",", "."))
                except Exception:
                    d_achat = None

            is_sale = bool(en_solde) and (d_solde is not None) and (d_solde < d_prix)
            if is_sale:
                price_str = f"{d_solde:.2f}"
                compare_str = f"{d_prix:.2f}"
            else:
                price_str = f"{d_prix:.2f}"
                compare_str = ""

            cost_str = f"{d_achat:.2f}" if (d_achat is not None and d_achat > Decimal("0.00")) else ""
            barcode_clean = str(barcode).strip() if barcode else ""

            # Image : uniquement si URL web valide http/https
            image_src = str(img_path).strip() if img_path and str(img_path).startswith(("http://", "https://")) else ""

            # Tags (Catégorie, Marque)
            tags_parts = []
            if cat and str(cat).strip():
                tags_parts.append(str(cat).strip())
            if marque and str(marque).strip():
                tags_parts.append(str(marque).strip())
            tags_str = ", ".join(tags_parts)

            # Règle partagée avec la synchro : c'est par ces SKU qu'une vente en caisse retrouve
            # SA taille sur Shopify (voir kodo_core/sync/shopify_sku.py).
            est_sans_taille = _est_sans_taille([t for t, _ in variantes])

            d_tva = None
            if taux_tva is not None and str(taux_tva).strip() != "":
                try:
                    d_tva = Decimal(str(taux_tva).strip().replace(",", "."))
                except Exception:
                    d_tva = None
            is_taxable = True if (d_tva is None or d_tva > Decimal("0.00")) else False

            for idx, (taille, qte) in enumerate(variantes):
                t_label = (taille or "Taille Unique").strip()
                if not t_label:
                    t_label = "Taille Unique"

                row = {h: "" for h in SHOPIFY_PRODUCT_HEADERS}
                row["Handle"] = handle

                # Colonnes produit (sur la première ligne uniquement pour accélérer l'import Shopify)
                if idx == 0:
                    row["Title"] = nom_propre
                    row["Body (HTML)"] = f"<p>{nom_propre}</p>"
                    row["Vendor"] = str(marque).strip() if marque else ""
                    row["Type"] = str(cat).strip() if cat else ""
                    row["Tags"] = tags_str
                    row["Published"] = "TRUE" if status == "active" else "FALSE"
                    row["Status"] = status
                    row["Image Src"] = image_src

                # Déclinaison / Option
                if est_sans_taille:
                    row["Option1 Name"] = "Title"
                    row["Option1 Value"] = "Default Title"
                else:
                    row["Option1 Name"] = "Taille"
                    row["Option1 Value"] = t_label

                # SKU unique et sans espace, un par taille
                row["Variant SKU"] = sku_variante(barcode_clean, p_id, taille, idx, est_sans_taille)

                # Code-barres : pour éviter le rejet Shopify 'Barcode has already been taken',
                # seul le premier variant reçoit le code-barres si le catalogue ne différencie pas par taille
                if idx == 0:
                    row["Variant Barcode"] = barcode_clean
                else:
                    row["Variant Barcode"] = ""

                try:
                    qty_int = int(qte) if qte is not None else 0
                except (ValueError, TypeError):
                    qty_int = 0

                row["Variant Grams"] = "0"
                row["Variant Inventory Tracker"] = "shopify"
                row["Variant Inventory Qty"] = str(qty_int)
                row["Variant Inventory Policy"] = "deny"
                row["Variant Fulfillment Service"] = "manual"
                row["Variant Price"] = price_str
                row["Variant Compare At Price"] = compare_str
                row["Variant Requires Shipping"] = "TRUE"
                row["Variant Taxable"] = "TRUE" if is_taxable else "FALSE"
                row["Cost per item"] = cost_str

                rows.append(row)

        if output_path is None:
            export_directory = _ensure_dir()
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(export_directory, f"export_shopify_produits_{ts}.csv")
        else:
            out_dir = os.path.dirname(os.path.abspath(output_path))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)

        # Sauvegarde avec séparateur virgule et encodage utf-8-sig
        with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=SHOPIFY_PRODUCT_HEADERS, delimiter=",")
            writer.writeheader()
            writer.writerows(rows)

        # Le fichier fige le stock de Kōdo : les ventes déjà faites y sont comptées et ne devront
        # pas être repoussées vers Shopify quand la synchro sera branchée (seules les suivantes).
        try:
            from kodo_core.sync.shopify import noter_alignement_stock
            noter_alignement_stock(conn)
            conn.commit()
        except Exception as e:
            print(f"⚠️ [EXPORT SHOPIFY] Point d'alignement du stock non enregistré : {e}")

        return output_path

    finally:
        if should_close and conn:
            conn.close()

