# -*- coding: utf-8 -*-
"""
Contrôle d'intégrité au lancement — Kōdo POS.

Pourquoi ce module existe
-------------------------
Un défaut qui dort dans la base ou dans l'installation d'un client (clé étrangère orpheline,
migration jamais appliquée, file Shopify bloquée depuis huit jours, interface sans son bundle…)
ne se voit pas : rien ne le cherche. Ce module le cherche, à chaque lancement, sans jamais gêner
la caisse.

Ce qu'il garantit
-----------------
- LECTURE SEULE. La base est ouverte en LECTURE SEULE au niveau du fichier (`mode=ro`) ET avec
  `PRAGMA query_only=ON` : aucune écriture, aucune réparation, pas même l'en-tête du fichier ni le
  point de contrôle du journal WAL. Le journal signé n'est jamais modifié (règle Kōdo). Les seuls
  fichiers écrits sont le rapport JSON (et son temporaire) dans `<données>/diagnostics/`, plus une
  sonde d'écriture éphémère `.kodo_controle_*` dans le dossier de la base (supprimée aussitôt).
- JAMAIS BLOQUANT. Il tourne dans un thread démon, après le démarrage du serveur, et chaque contrôle
  est isolé : une exception devient un constat « erreur » (le contrôle lui-même a échoué), jamais un
  plantage. Un contrôle en erreur ou non exécuté (délai) n'est JAMAIS présenté comme « OK » : le
  statut global passe à ATTENTION.
- TRANSPARENT. Un contrôle qui ne peut pas conclure le dit (`erreur`, `info`) au lieu d'inventer un
  résultat plausible.
- SANS EFFET DE BORD. Il ne crée ni clé de scellement, ni sauvegarde, ni snapshot de migration, ne
  démarre aucune synchronisation et n'ouvre aucune connexion réseau.

Deux niveaux
------------
- `rapide`  : moins d'une seconde — fichier, intégrité SQLite, clés étrangères, migrations, dérive du
              schéma, disque, interface.
- `complet` : le rapide + chaîne cryptographique NF525, jours non clôturés, rattachement des Z,
              numérotation, stock, catalogue, file de synchronisation Shopify.

Le rapport (`dernier_controle.json`) garde la comparaison avec le contrôle précédent : ce qui est
NOUVEAU depuis la dernière fois est ce qui mérite l'attention du support.

Règle de croissance : chaque défaut réel trouvé un jour doit devenir un contrôle ici (une fonction
`_c_*` + une ligne dans `CONTROLES` + un test). C'est ce qui fait reculer l'inconnu.
"""
import datetime
import hashlib
import json
import logging
import os
import pathlib
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("kodo.startup_check")

SCHEMA_RAPPORT = 1
NIVEAU_RAPIDE = "rapide"
NIVEAU_COMPLET = "complet"

OK, INFO, ATTENTION, CRITIQUE, ERREUR = "ok", "info", "attention", "critique", "erreur"
_RANG = {OK: 0, INFO: 0, ERREUR: 1, ATTENTION: 2, CRITIQUE: 3}

# Tables sans lesquelles la caisse ne peut pas fonctionner.
TABLES_ATTENDUES = (
    "Produits", "Stocks", "Tickets", "Ventes_Details", "Ledger_Caisse", "Rapports_Z",
    "Clotures_Caisse", "Audit_Trail", "Parametres", "Clients", "Vendeurs", "schema_version",
)

# Au-delà de cette durée, les contrôles restants ne sont pas lancés (signalé dans le rapport).
BUDGET_SECONDES = 30.0
# Plancher d'espace disque : en dessous, SQLite ne peut plus garantir une écriture complète.
DISQUE_CRITIQUE_MO = 50
DISQUE_ATTENTION_MO = 500
# Une écriture Shopify en attente depuis plus longtemps est considérée bloquée.
FILE_SHOPIFY_BLOQUEE_HEURES = 24

NOM_RAPPORT = "dernier_controle.json"


# --------------------------------------------------------------------------------------------
# Constats
# --------------------------------------------------------------------------------------------

def _constat(id_: str, titre: str, statut: str, detail: str = "", donnees: Optional[dict] = None,
             action: str = "", alerter_si_nouveau: bool = False) -> Dict[str, Any]:
    """`alerter_si_nouveau` : reste `info` tant que le constat est ancien ; passe `attention` s'il est
    NOUVEAU depuis le contrôle précédent (ex. un trou de numérotation hérité d'une vieille version
    n'alarme pas la commerçante, un trou apparu depuis hier oui)."""
    return {
        "id": id_, "titre": titre, "statut": statut, "detail": detail,
        "donnees": donnees or {}, "action": action, "alerter_si_nouveau": alerter_si_nouveau,
    }


def _cacher_domicile(texte: Any) -> str:
    """Le rapport circule vers le support : le nom de session macOS n'a rien à y faire."""
    home = os.path.expanduser("~")
    return str(texte).replace(home, "~") if home and home != "~" else str(texte)


class _Contexte:
    def __init__(self, db_path: str, dist_dir: Optional[str], niveau: str):
        self.db_path = db_path
        self.dist_dir = dist_dir
        self.niveau = niveau
        self.conn: Optional[sqlite3.Connection] = None

    def existe(self, table: str) -> bool:
        c = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,))
        return c.fetchone() is not None

    def colonnes(self, table: str) -> set:
        return {r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


# --------------------------------------------------------------------------------------------
# Contrôles « rapides »
# --------------------------------------------------------------------------------------------

_FICHIERS_VERSION = (
    "~/Library/Caches/KodoPOS/version.json",
    "~/.kodo_pos/version.json",
    "~/Documents/Kodo_POS/version.json",
)


def _version_installee(conn: Optional[sqlite3.Connection] = None) -> str:
    """
    Même résolution que `updater.get_installed_version`, SANS son effet de bord : cette fonction-là
    écrit `app_version` dans Parametres quand la valeur manque, et ouvre la base par défaut. Un
    contrôle en lecture seule ne l'appelle donc pas (test : la base reste identique à l'octet près).
    """
    from kodo_core.services import updater
    installee = updater.CURRENT_VERSION
    candidats: List[str] = []
    for fichier in _FICHIERS_VERSION:
        data = _lire_json(os.path.expanduser(fichier))
        if data and data.get("version"):
            candidats.append(str(data["version"]).lstrip("v"))
    if conn is not None:
        try:
            row = conn.execute("SELECT valeur FROM Parametres WHERE cle = 'app_version'").fetchone()
            if row and row[0]:
                candidats.append(str(row[0]).lstrip("v"))
        except sqlite3.Error:
            pass
    for v in candidats:
        try:
            if updater.parse_version(v) >= updater.parse_version(installee):
                installee = v
        except Exception:  # noqa: BLE001
            continue
    return installee


def _c_versions(ctx: _Contexte):
    donnees: Dict[str, Any] = {"python": platform.python_version(), "plateforme": platform.platform()}
    try:
        donnees["application"] = _version_installee(ctx.conn)
    except Exception as e:  # noqa: BLE001
        donnees["application"] = f"illisible ({e})"
    try:
        import kodo_base
        donnees["base_dmg"] = kodo_base.BASE_VERSION
    except Exception:  # noqa: BLE001
        pass
    try:
        import patch_loader
        etat = patch_loader.status()
        donnees["correctif_backend"] = etat.get("loaded_version") or "aucun (code du DMG)"
    except Exception:  # noqa: BLE001
        pass
    donnees["frozen"] = bool(getattr(sys, "frozen", False))
    return _constat("env.versions", "Versions installées", INFO,
                    f"Application {donnees.get('application')} · DMG {donnees.get('base_dmg', '?')} · "
                    f"correctif backend : {donnees.get('correctif_backend', '?')}", donnees)


def _c_disque(ctx: _Contexte):
    dossier = os.path.dirname(os.path.abspath(ctx.db_path)) if ctx.db_path else "."
    if not os.path.isdir(dossier):
        dossier = tempfile.gettempdir()
    libre_mo = shutil.disk_usage(dossier).free // (1024 * 1024)
    ecriture_ok, erreur = True, ""
    try:
        with tempfile.NamedTemporaryFile(dir=dossier, prefix=".kodo_controle_", delete=True) as f:
            f.write(b"ok")
            f.flush()
    except Exception as e:  # noqa: BLE001
        ecriture_ok, erreur = False, str(e)
    donnees = {"libre_mo": libre_mo, "ecriture_ok": ecriture_ok}
    if not ecriture_ok:
        return _constat("env.disque", "Disque et droits d'écriture", CRITIQUE,
                        f"Impossible d'écrire dans le dossier de la base : {_cacher_domicile(erreur)}",
                        donnees, "Vérifier les droits du dossier Documents/Kodo_POS.")
    if libre_mo < DISQUE_CRITIQUE_MO:
        return _constat("env.disque", "Disque et droits d'écriture", CRITIQUE,
                        f"Espace disque presque épuisé : {libre_mo} Mo libres.", donnees,
                        "Libérer de l'espace avant de continuer à vendre.")
    if libre_mo < DISQUE_ATTENTION_MO:
        return _constat("env.disque", "Disque et droits d'écriture", ATTENTION,
                        f"Espace disque faible : {libre_mo} Mo libres.", donnees,
                        "Libérer de l'espace disque.")
    return _constat("env.disque", "Disque et droits d'écriture", OK,
                    f"{libre_mo} Mo libres, écriture possible.", {"ecriture_ok": True})


def _c_fichier_base(ctx: _Contexte):
    taille = os.path.getsize(ctx.db_path)
    wal = ctx.db_path + "-wal"
    donnees = {"taille_octets": taille, "wal_octets": os.path.getsize(wal) if os.path.exists(wal) else 0}
    return _constat("base.fichier", "Fichier de la base", OK, f"{taille // 1024} Ko.", donnees)


def _c_integrite_sqlite(ctx: _Contexte):
    try:
        lignes = [r[0] for r in ctx.conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.DatabaseError as e:
        # Une page illisible fait LEVER l'exception au lieu de renvoyer des lignes : c'est aussi une corruption.
        lignes = [f"SQLite ne peut pas lire la base : {e}"]
    if lignes == ["ok"]:
        return _constat("base.integrite", "Intégrité SQLite", OK, "La structure du fichier est saine (quick_check).")
    return _constat("base.integrite", "Intégrité SQLite", CRITIQUE,
                    "SQLite signale une corruption : " + " | ".join(str(x) for x in lignes[:3]),
                    {"lignes": [str(x) for x in lignes[:10]]},
                    "Ne pas réparer à la main : restaurer une sauvegarde (Paramètres › Sauvegardes) avec le support.")


def _c_cles_etrangeres(ctx: _Contexte):
    lignes = ctx.conn.execute("PRAGMA foreign_key_check").fetchall()
    if not lignes:
        return _constat("base.cles_etrangeres", "Références entre tables", OK, "Aucune ligne orpheline.")
    groupes: Counter = Counter()
    exemples: Dict[str, list] = defaultdict(list)
    for table, rowid, parent, _fk in lignes:
        cle = f"{table} → {parent}"
        groupes[cle] += 1
        if len(exemples[cle]) < 5:
            exemples[cle].append(rowid)
    resume = " ; ".join(f"{n} ligne(s) {cle}" for cle, n in groupes.most_common())
    return _constat("base.cles_etrangeres", "Références entre tables", ATTENTION,
                    f"Lignes qui pointent vers un enregistrement disparu : {resume}.",
                    {"groupes": dict(groupes), "exemples_rowid": dict(exemples)},
                    "À examiner : les clés étrangères n'étant pas imposées, la ligne reste mais son article/ticket est introuvable.")


def _c_tables(ctx: _Contexte):
    manquantes = [t for t in TABLES_ATTENDUES if not ctx.existe(t)]
    if manquantes:
        return _constat("base.tables", "Tables essentielles", CRITIQUE,
                        "Table(s) absente(s) : " + ", ".join(manquantes), {"manquantes": manquantes},
                        "Relancer l'application ; si persistant, restaurer une sauvegarde avec le support.")
    return _constat("base.tables", "Tables essentielles", OK, f"{len(TABLES_ATTENDUES)} tables présentes.")


def _versions_migrations() -> List[str]:
    from kodo_core.db.migrations import MigrationManager
    return [m["version"] for m in MigrationManager.MIGRATIONS]


def _c_migrations(ctx: _Contexte):
    attendues = _versions_migrations()
    if not ctx.existe("schema_version"):
        return _constat("base.migrations", "Migrations du schéma", CRITIQUE,
                        "La table schema_version est absente : l'état des migrations est inconnu.")
    appliquees = [r[0] for r in ctx.conn.execute("SELECT version FROM schema_version").fetchall()]
    manquantes = [v for v in attendues if v not in appliquees]
    en_avance = [v for v in appliquees if v not in attendues]
    donnees = {"attendues": len(attendues), "appliquees": len(appliquees),
               "manquantes": manquantes, "inconnues_de_cette_version": en_avance}
    if manquantes:
        return _constat("base.migrations", "Migrations du schéma", CRITIQUE,
                        "Migration(s) jamais appliquée(s) : " + ", ".join(manquantes)
                        + ". Un échec de migration est silencieux au démarrage.", donnees,
                        "Signaler au support avec ce rapport avant de poursuivre.")
    if en_avance:
        return _constat("base.migrations", "Migrations du schéma", ATTENTION,
                        "La base porte des migrations que cette version ne connaît pas : "
                        + ", ".join(en_avance) + " (réinstallation d'une version plus ancienne ?).", donnees,
                        "Réinstaller la version la plus récente de Kōdo POS.")
    return _constat("base.migrations", "Migrations du schéma", OK,
                    f"{len(appliquees)} migration(s) appliquée(s), aucune manquante.", donnees)


def _decrire_schema(conn) -> Dict[str, Any]:
    tables: Dict[str, set] = {}
    for (nom,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        tables[nom] = {r[1] for r in conn.execute(f'PRAGMA table_info("{nom}")').fetchall()}
    declencheurs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    index = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'").fetchall()}
    return {"tables": tables, "declencheurs": declencheurs, "index": index}


def schema_de_reference() -> Dict[str, Any]:
    """
    Schéma qu'une installation neuve de CE code produit, construit dans une base EN MÉMOIRE.

    On rejoue ce que fait `initialiser_db` (migrations puis création brute), sans passer par
    `MigrationManager.run_migrations` : celui-ci prend un instantané de la base à chaque exécution, et
    un contrôle de lancement n'a pas à écrire dans les sauvegardes du commerçant pour se comparer.
    """
    import database_manager
    from kodo_core.db.migrations import MigrationManager

    ref = sqlite3.connect(":memory:")
    try:
        cur = ref.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        for migration in MigrationManager.MIGRATIONS:
            for instruction in migration.get("sql", []):
                try:
                    cur.execute(instruction)
                except sqlite3.OperationalError as oe:
                    if "duplicate column name" not in str(oe).lower():
                        raise
            routine = migration.get("python")
            if callable(routine):
                routine(ref)
        ref.commit()
        database_manager._initialiser_db_raw(ref)  # noqa: SLF001 — même séquence que initialiser_db
        ref.commit()
        return _decrire_schema(ref)
    finally:
        ref.close()


def _c_derive_schema(ctx: _Contexte):
    """Compare la base du client à une installation neuve : détecte les ALTER/CREATE avalés en silence."""
    ref = schema_de_reference()
    reel = _decrire_schema(ctx.conn)

    tables_manquantes = sorted(set(ref["tables"]) - set(reel["tables"]))
    colonnes_manquantes: Dict[str, list] = {}
    for table, colonnes in ref["tables"].items():
        if table in reel["tables"]:
            manque = sorted(colonnes - reel["tables"][table])
            if manque:
                colonnes_manquantes[table] = manque
    declencheurs_manquants = sorted(ref["declencheurs"] - reel["declencheurs"])
    index_manquants = sorted(ref["index"] - reel["index"])

    donnees = {"tables_manquantes": tables_manquantes, "colonnes_manquantes": colonnes_manquantes,
               "declencheurs_manquants": declencheurs_manquants, "index_manquants": index_manquants}
    if tables_manquantes or colonnes_manquantes:
        morceaux = []
        if tables_manquantes:
            morceaux.append("tables absentes : " + ", ".join(tables_manquantes))
        for table, colonnes in colonnes_manquantes.items():
            morceaux.append(f"{table} sans {', '.join(colonnes)}")
        return _constat("base.derive_schema", "Structure de la base", CRITIQUE,
                        "La base ne correspond pas à cette version : " + " ; ".join(morceaux) + ".", donnees,
                        "Une requête de l'application échouera sur ces éléments : signaler au support.")
    if declencheurs_manquants or index_manquants:
        morceaux = []
        if declencheurs_manquants:
            morceaux.append("déclencheur(s) absent(s) : " + ", ".join(declencheurs_manquants))
        if index_manquants:
            morceaux.append("index absent(s) : " + ", ".join(index_manquants))
        return _constat("base.derive_schema", "Structure de la base", ATTENTION,
                        "Protections ou index manquants : " + " ; ".join(morceaux) + ".", donnees,
                        "Les déclencheurs protègent les données scellées : à signaler au support.")
    return _constat("base.derive_schema", "Structure de la base", OK,
                    f"Conforme à une installation neuve ({len(ref['tables'])} tables, "
                    f"{len(ref['declencheurs'])} déclencheurs).", donnees)


_RE_BUNDLE = re.compile(r'src=["\']([^"\']+\.js)["\']')


def _c_interface(ctx: _Contexte):
    if not ctx.dist_dir:
        return _constat("interface.dist", "Interface servie", INFO, "Dossier d'interface non communiqué au contrôle.")
    index = os.path.join(ctx.dist_dir, "index.html")
    if not os.path.isfile(index):
        return _constat("interface.dist", "Interface servie", CRITIQUE,
                        f"index.html introuvable dans {_cacher_domicile(ctx.dist_dir)} : écran blanc probable.",
                        {"dossier": _cacher_domicile(ctx.dist_dir)}, "Réinstaller l'application.")
    with open(index, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    manquants = []
    for rel in _RE_BUNDLE.findall(html):
        chemin = os.path.join(ctx.dist_dir, rel.lstrip("/"))
        if not os.path.isfile(chemin) or os.path.getsize(chemin) == 0:
            manquants.append(rel)
    if manquants:
        return _constat("interface.dist", "Interface servie", CRITIQUE,
                        "Fichier(s) de l'interface absent(s) ou vide(s) : " + ", ".join(manquants)
                        + " — écran blanc probable.", {"manquants": manquants},
                        "Réinstaller l'application ou relancer la mise à jour.")
    return _constat("interface.dist", "Interface servie", OK, "index.html et son bundle JavaScript sont présents.")


# --------------------------------------------------------------------------------------------
# Contrôles « complets »
# --------------------------------------------------------------------------------------------

def _chemin_cle_scellement() -> str:
    return os.path.join(os.path.expanduser("~/.kodo_signing"), "audit_hmac.key")


def _cle_scellement_utilisable() -> bool:
    """
    Même critère que `get_audit_machine_secret` (≥ 16 octets une fois nettoyée), mais SANS jamais écrire :
    cette fonction-là FABRIQUE une nouvelle clé dès que la clé existante est absente ou trop courte, ce
    qui rendrait ensuite illisibles les tickets déjà scellés. Le contrôle ne l'appelle donc qu'après
    avoir constaté que la clé est utilisable.
    """
    try:
        with open(_chemin_cle_scellement(), "rb") as f:
            return len(f.read().strip()) >= 16
    except OSError:
        return False


def _c_chaine_nf525(ctx: _Contexte):
    if not ctx.existe("Tickets"):
        return _constat("ventes.chaine_nf525", "Chaîne de signatures NF525", INFO, "Table Tickets absente.")
    n = ctx.conn.execute("SELECT COUNT(*) FROM Tickets").fetchone()[0]
    from kodo_core.db import audit_trail

    # nom lisible -> True (intacte) / False (rupture ou altération) / None (non vérifiable ici)
    resultats: Dict[str, Optional[bool]] = {}
    pourquoi: Dict[str, str] = {}

    def verifier(nom: str, table: str, fonction: Callable[[], Any]) -> None:
        if not ctx.existe(table):
            resultats[nom] = True  # table absente d'une très ancienne base : aucune chaîne à rompre
            return
        try:
            resultats[nom] = bool(fonction())
        except ValueError:
            resultats[nom] = False  # violation d'intégrité constatée (verify_database_integrity)
        except Exception as e:  # noqa: BLE001 — contenu ou schéma imprévu : pas vérifiable, mais dit
            resultats[nom] = None
            pourquoi[nom] = f"{type(e).__name__}: {_cacher_domicile(e)[:80]}"

    # Les tickets sont scellés par HMAC avec la clé de CETTE machine : sans clé utilisable (base restaurée
    # sur un autre poste, fichier tronqué), ils ne peuvent pas être vérifiés — et on ne fabrique rien.
    cle_ok = n == 0 or _cle_scellement_utilisable()
    if cle_ok:
        verifier("tickets", "Tickets", lambda: audit_trail.verify_database_integrity(ctx.conn))
    else:
        resultats["tickets"] = None
    verifier("journal de caisse", "Ledger_Caisse", lambda: audit_trail.verifier_chainage(
        "Ledger_Caisse", compute_hash_func=audit_trail.hash_ledger_row, conn=ctx.conn))
    verifier("journal des Z", "Rapports_Z", lambda: audit_trail.verifier_chainage(
        "Rapports_Z", compute_hash_func=audit_trail.hash_z_row, conn=ctx.conn))
    # Les vrais Z de caisse (Clotures_Caisse) : chaîne distincte du journal Rapports_Z.
    verifier("clôtures Z", "Clotures_Caisse", lambda: audit_trail.verifier_clotures(ctx.conn))
    verifier("piste d'audit", "Audit_Trail", lambda: audit_trail.verifier_chainage("Audit_Trail", conn=ctx.conn))

    cles = {"tickets": "tickets_ok", "journal de caisse": "ledger_ok", "journal des Z": "z_ok",
            "clôtures Z": "clotures_ok", "piste d'audit": "audit_ok"}
    donnees: Dict[str, Any] = {"tickets": n, **{cles[nom]: ok for nom, ok in resultats.items()}}
    cassees = [nom for nom, ok in resultats.items() if ok is False]
    inverifiees = [nom for nom, ok in resultats.items() if ok is None]
    if pourquoi:
        donnees["inverifiable"] = pourquoi

    if cassees:
        detail = "Rupture ou altération détectée sur : " + ", ".join(cassees) + "."
        if inverifiees:
            detail += " Non vérifié(s) : " + ", ".join(inverifiees) + "."
        return _constat("ventes.chaine_nf525", "Chaîne de signatures NF525", CRITIQUE, detail, donnees,
                        "Ne rien modifier : transmettre ce rapport au support.")
    if inverifiees:
        morceaux = []
        if "tickets" in inverifiees and not cle_ok:
            morceaux.append(f"{n} ticket(s) mais la clé de scellement de cette machine est absente ou illisible : "
                            "les tickets ne peuvent pas être vérifiés ici (base restaurée sur un autre poste ?)")
        autres = [x for x in inverifiees if not (x == "tickets" and not cle_ok)]
        if autres:
            morceaux.append("vérification impossible pour : " + ", ".join(autres))
        return _constat("ventes.chaine_nf525", "Chaîne de signatures NF525", ATTENTION,
                        " ; ".join(morceaux) + ".", donnees,
                        "Signaler au support : ne pas régénérer la clé à la main.")
    return _constat("ventes.chaine_nf525", "Chaîne de signatures NF525", OK,
                    f"Chaînage intact sur les tables scellées ({n} ticket(s)).", donnees)


def _c_jours_non_clotures(ctx: _Contexte):
    if not ctx.existe("Tickets") or "z_id" not in ctx.colonnes("Tickets"):
        return _constat("ventes.jours_non_clotures", "Journées non clôturées", INFO, "Colonne z_id absente.")
    aujourdhui = datetime.date.today().isoformat()
    lignes = ctx.conn.execute(
        "SELECT substr(date_heure, 1, 10) AS jour, COUNT(*) FROM Tickets "
        "WHERE z_id IS NULL AND substr(date_heure, 1, 10) < ? GROUP BY jour ORDER BY jour ASC", (aujourdhui,)
    ).fetchall()
    if not lignes:
        return _constat("ventes.jours_non_clotures", "Journées non clôturées", OK, "Toutes les journées passées sont clôturées.")
    jours = [{"jour": j, "tickets": n} for j, n in lignes]
    return _constat("ventes.jours_non_clotures", "Journées non clôturées", ATTENTION,
                    f"{len(jours)} journée(s) antérieure(s) sans clôture Z (la plus ancienne : {jours[0]['jour']}).",
                    {"jours": jours[:15], "total_jours": len(jours)},
                    "Clôturer les journées une par une, de la plus ancienne à la plus récente.")


def _c_z_orphelins(ctx: _Contexte):
    if not (ctx.existe("Tickets") and ctx.existe("Clotures_Caisse")):
        return _constat("ventes.z_orphelins", "Rattachement aux clôtures Z", INFO, "Tables absentes.")
    donnees = {}
    for table in ("Tickets", "Ledger_Caisse"):
        if ctx.existe(table) and "z_id" in ctx.colonnes(table):
            donnees[table] = ctx.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE z_id IS NOT NULL AND z_id NOT IN (SELECT id FROM Clotures_Caisse)"
            ).fetchone()[0]
    total = sum(donnees.values())
    if total == 0:
        return _constat("ventes.z_orphelins", "Rattachement aux clôtures Z", OK, "Tout ce qui est clôturé pointe vers un Z existant.")
    return _constat("ventes.z_orphelins", "Rattachement aux clôtures Z", ATTENTION,
                    f"{total} ligne(s) rattachée(s) à une clôture Z qui n'existe pas "
                    f"({', '.join(f'{t} : {n}' for t, n in donnees.items() if n)}).", donnees,
                    "Les totaux d'un Z ne se reconstitueraient pas : à signaler au support.")


def _c_registre_fiscal(ctx: _Contexte):
    """Ventes ou retours dont le scellement fiscal a échoué et qui attendent d'être rescellés.

    `fiscal_unsealed_sales` est alimentée quand `seal_sale` échoue APRÈS le commit de la vente. Le serveur
    les rescelle à chaque lancement : si des lignes y restent, le rescellement échoue encore (revue du
    2026-10-14 : la reprise n'était branchée nulle part, les lignes s'accumulaient en silence)."""
    titre = "Registre fiscal (ventes à sceller)"
    if not ctx.existe("fiscal_unsealed_sales"):
        return _constat("ventes.registre_fiscal", titre, OK, "Aucune vente en attente de scellement.")
    n = ctx.conn.execute("SELECT COUNT(*) FROM fiscal_unsealed_sales").fetchone()[0]
    if n == 0:
        return _constat("ventes.registre_fiscal", titre, OK, "Aucune vente en attente de scellement.", {"en_attente": 0})
    derniere = ctx.conn.execute(
        "SELECT failure_reason FROM fiscal_unsealed_sales ORDER BY recorded_at DESC LIMIT 1").fetchone()
    return _constat("ventes.registre_fiscal", titre, ATTENTION,
                    f"{n} vente(s) ou retour(s) enregistrés mais pas encore scellés au registre fiscal "
                    f"(dernière cause : {str(derniere[0])[:120]}).", {"en_attente": n},
                    "Relancer l'application retente le scellement ; si cela persiste, le signaler au support.")


_RE_NUMERO = re.compile(r"^TCK-(\d{4})-(\d+)$")


def _c_numerotation(ctx: _Contexte):
    if not ctx.existe("Tickets"):
        return _constat("ventes.numerotation", "Numérotation des tickets", INFO, "Table Tickets absente.")
    par_annee: Dict[str, set] = defaultdict(set)
    for (num,) in ctx.conn.execute("SELECT numero_ticket FROM Tickets WHERE numero_ticket LIKE 'TCK-%'").fetchall():
        m = _RE_NUMERO.match(str(num or ""))
        if m:
            par_annee[m.group(1)].add(int(m.group(2)))
    # Les trous sont calculés en PLAGES (jamais `range(1, max)` en mémoire : un numéro aberrant comme
    # TCK-2026-99999999 y mettrait des gigaoctets). L'empreinte du constat porte sur TOUTES les plages :
    # un trou nouveau change l'empreinte même quand les 20 premiers numéros affichés sont inchangés.
    manquants: Dict[str, list] = {}
    nombre: Dict[str, int] = {}
    plages_par_annee: Dict[str, list] = {}
    empreintes: Dict[str, str] = {}
    for annee, nums in sorted(par_annee.items()):
        plages, precedent = [], 0
        for num in sorted(nums):
            if num > precedent + 1:
                plages.append((precedent + 1, num - 1))
            precedent = num
        if not plages:
            continue
        nombre[annee] = sum(b - a + 1 for a, b in plages)
        plages_par_annee[annee] = [f"{a}" if a == b else f"{a}-{b}" for a, b in plages[:20]]
        empreintes[annee] = hashlib.sha256(
            ";".join(f"{a}-{b}" for a, b in plages).encode("ascii")).hexdigest()[:12]
        affiches: List[int] = []
        for a, b in plages:
            affiches.extend(range(a, min(b, a + 20) + 1))
            if len(affiches) >= 20:
                break
        manquants[annee] = affiches[:20]
    if not manquants:
        return _constat("ventes.numerotation", "Numérotation des tickets", OK, "Séquence continue, sans trou.")
    total = sum(nombre.values())
    return _constat("ventes.numerotation", "Numérotation des tickets", INFO,
                    f"{total} numéro(s) manquant(s) dans la séquence ({', '.join(f'{a} : {n}' for a, n in sorted(nombre.items()))}). "
                    "Un trou ancien est hérité d'une version antérieure ; un trou NOUVEAU est signalé.",
                    {"manquants": manquants, "plages": plages_par_annee, "nombre": nombre,
                     "empreinte_trous": empreintes},
                    "Un trou ne se comble pas après coup : le documenter pour le comptable.",
                    alerter_si_nouveau=True)


def _c_stock(ctx: _Contexte):
    if not ctx.existe("Stocks"):
        return _constat("stock.coherence", "Cohérence du stock", INFO, "Table Stocks absente.")
    cols = ctx.colonnes("Stocks")
    sans_drapeau = "COALESCE(requires_stock_audit, 0) = 0" if "requires_stock_audit" in cols else "1"
    negatifs = ctx.conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({sans_drapeau}), 0) FROM Stocks WHERE quantite_actuelle < 0").fetchone()
    doublons = ctx.conn.execute(
        "SELECT COUNT(*) FROM (SELECT id_produit FROM Stocks WHERE id_produit IS NOT NULL "
        "GROUP BY id_produit, COALESCE(taille, '') HAVING COUNT(*) > 1)").fetchone()[0]
    n_neg, non_signales = int(negatifs[0] or 0), int(negatifs[1] or 0)
    donnees = {"negatifs": n_neg, "negatifs_sans_drapeau_audit": non_signales, "doublons_taille": int(doublons)}
    problemes = []
    if non_signales:
        problemes.append(f"{non_signales} ligne(s) en stock négatif sans demande d'audit")
    if doublons:
        problemes.append(f"{doublons} article(s) avec la même taille en double")
    if problemes:
        return _constat("stock.coherence", "Cohérence du stock", ATTENTION, " ; ".join(problemes) + ".", donnees,
                        "Un stock négatif est légitime (vente hors-ligne) mais doit être audité ; "
                        "un doublon de taille rend le décompte ambigu.")
    if n_neg:
        return _constat("stock.coherence", "Cohérence du stock", INFO,
                        f"{n_neg} ligne(s) en stock négatif, déjà marquée(s) à auditer.", donnees)
    return _constat("stock.coherence", "Cohérence du stock", OK, "Aucun stock négatif, aucune taille en double.", donnees)


def _c_catalogue(ctx: _Contexte):
    if not ctx.existe("Produits"):
        return _constat("produits.catalogue", "Catalogue", INFO, "Table Produits absente.")
    sans_prix = ctx.conn.execute(
        "SELECT COUNT(*) FROM Produits WHERE prix_vente_tvac IS NULL OR CAST(prix_vente_tvac AS REAL) < 0").fetchone()[0]
    tva_incoherente = ctx.conn.execute(
        "SELECT COUNT(*) FROM Produits WHERE taux_tva IS NULL OR CAST(taux_tva AS REAL) < 0 OR CAST(taux_tva AS REAL) > 0.5"
    ).fetchone()[0]
    donnees = {"sans_prix_ou_negatif": sans_prix, "tva_incoherente": tva_incoherente}
    if sans_prix or tva_incoherente:
        morceaux = []
        if sans_prix:
            morceaux.append(f"{sans_prix} article(s) sans prix de vente valide")
        if tva_incoherente:
            morceaux.append(f"{tva_incoherente} article(s) avec un taux de TVA hors de 0–50 %")
        return _constat("produits.catalogue", "Catalogue", ATTENTION, " ; ".join(morceaux) + ".", donnees,
                        "Ces articles se vendraient à un prix ou une TVA faux : corriger les fiches.")
    return _constat("produits.catalogue", "Catalogue", OK, "Prix et taux de TVA plausibles.", donnees)


def _c_shopify(ctx: _Contexte):
    donnees: Dict[str, Any] = {}
    problemes: List[str] = []
    if ctx.existe("sync_queue"):
        row = ctx.conn.execute("SELECT COUNT(*), MIN(date_creation) FROM sync_queue WHERE statut = 'PENDING'").fetchone()
        n_attente, plus_ancien = int(row[0] or 0), row[1]
        indetermines = int(ctx.conn.execute(
            "SELECT COUNT(*) FROM sync_queue WHERE statut = 'INDETERMINE'").fetchone()[0] or 0)
        donnees.update(en_attente=n_attente, plus_ancien=plus_ancien, indetermines=indetermines)
        if n_attente and plus_ancien:
            try:
                # `date_creation` peut venir en ISO (« 2026-10-14T09:30:00 ») : sans ce remplacement le
                # parsing échouait et une file bloquée n'était jamais signalée.
                age = datetime.datetime.now() - datetime.datetime.strptime(
                    str(plus_ancien).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
                heures = int(age.total_seconds() // 3600)
                donnees["age_heures"] = heures
                if heures >= FILE_SHOPIFY_BLOQUEE_HEURES:
                    erreur = ctx.conn.execute(
                        "SELECT derniere_erreur FROM sync_queue WHERE statut = 'PENDING' AND derniere_erreur IS NOT NULL "
                        "ORDER BY date_maj DESC LIMIT 1").fetchone()
                    donnees["derniere_erreur"] = str(erreur[0] if erreur else "")[:160]
                    problemes.append(f"{n_attente} mouvement(s) de stock en attente d'envoi à Shopify depuis {heures} h"
                                     + (f" (dernière erreur : {donnees['derniere_erreur']})" if donnees["derniere_erreur"] else ""))
            except ValueError:
                pass
        if indetermines:
            problemes.append(f"{indetermines} envoi(s) Shopify de statut indéterminé, à vérifier à la main")
        # Mouvements écartés parce que l'article n'existe pas (ou plus) sur Shopify : légitime pour un
        # article vendu seulement en boutique, mais le stock du site n'a alors JAMAIS été mis à jour.
        ignores = int(ctx.conn.execute(
            "SELECT COUNT(*) FROM sync_queue WHERE statut = 'IGNORE' AND derniere_erreur LIKE 'SKU introuvable%'"
        ).fetchone()[0] or 0)
        donnees["ignores_sku_introuvable"] = ignores
    if ctx.existe("Shopify_Sync_Lignes"):
        ind = int(ctx.conn.execute("SELECT COUNT(*) FROM Shopify_Sync_Lignes WHERE statut = 'INDETERMINE'").fetchone()[0] or 0)
        donnees["lignes_indeterminees"] = ind
        if ind:
            problemes.append(f"{ind} ligne(s) du journal Shopify de statut indéterminé")
    if problemes:
        return _constat("shopify.file", "Synchronisation Shopify", ATTENTION, " ; ".join(problemes) + ".", donnees,
                        "Ouvrir Paramètres › Shopify : jeton expiré ou boutique injoignable ?")
    if not donnees:
        return _constat("shopify.file", "Synchronisation Shopify", OK, "Aucune file d'attente (Shopify non utilisé).")
    if donnees.get("ignores_sku_introuvable"):
        return _constat("shopify.file", "Synchronisation Shopify", INFO,
                        f"Aucun envoi bloqué ; {donnees['ignores_sku_introuvable']} mouvement(s) écarté(s) car "
                        "l'article est introuvable sur Shopify (stock du site non mis à jour pour ceux-là).", donnees)
    return _constat("shopify.file", "Synchronisation Shopify", OK, "Aucun envoi bloqué.", donnees)


# (id, titre, niveau minimal, nécessite la base ouverte, fonction)
CONTROLES: List[tuple] = [
    ("env.versions", "Versions installées", NIVEAU_RAPIDE, False, _c_versions),
    ("env.disque", "Disque et droits d'écriture", NIVEAU_RAPIDE, False, _c_disque),
    ("interface.dist", "Interface servie", NIVEAU_RAPIDE, False, _c_interface),
    ("base.fichier", "Fichier de la base", NIVEAU_RAPIDE, False, _c_fichier_base),
    ("base.integrite", "Intégrité SQLite", NIVEAU_RAPIDE, True, _c_integrite_sqlite),
    ("base.tables", "Tables essentielles", NIVEAU_RAPIDE, True, _c_tables),
    ("base.migrations", "Migrations du schéma", NIVEAU_RAPIDE, True, _c_migrations),
    ("base.derive_schema", "Structure de la base", NIVEAU_RAPIDE, True, _c_derive_schema),
    ("base.cles_etrangeres", "Références entre tables", NIVEAU_RAPIDE, True, _c_cles_etrangeres),
    ("ventes.jours_non_clotures", "Journées non clôturées", NIVEAU_COMPLET, True, _c_jours_non_clotures),
    ("ventes.z_orphelins", "Rattachement aux clôtures Z", NIVEAU_COMPLET, True, _c_z_orphelins),
    ("ventes.numerotation", "Numérotation des tickets", NIVEAU_COMPLET, True, _c_numerotation),
    ("ventes.registre_fiscal", "Registre fiscal (ventes à sceller)", NIVEAU_COMPLET, True, _c_registre_fiscal),
    ("stock.coherence", "Cohérence du stock", NIVEAU_COMPLET, True, _c_stock),
    ("produits.catalogue", "Catalogue", NIVEAU_COMPLET, True, _c_catalogue),
    ("shopify.file", "Synchronisation Shopify", NIVEAU_COMPLET, True, _c_shopify),
    # EN DERNIER : c'est le seul contrôle long (il relit tous les tickets). Placé en tête, il pouvait
    # consommer le budget et faire sauter tous les autres (mesuré : 118 s sur 300 000 tickets).
    ("ventes.chaine_nf525", "Chaîne de signatures NF525", NIVEAU_COMPLET, True, _c_chaine_nf525),
]


# --------------------------------------------------------------------------------------------
# Exécution
# --------------------------------------------------------------------------------------------

def _ouvrir_lecture_seule(db_path: str) -> sqlite3.Connection:
    # Le fichier est vérifié par l'appelant : `sqlite3.connect` créerait une base vide s'il manquait.
    # `mode=ro` : lecture seule au niveau du FICHIER. `query_only` seul laissait passer le changement de
    # mode de journal (SafeConnection exécute `PRAGMA journal_mode=WAL`, qui réécrit l'en-tête d'une
    # base en mode DELETE) et le point de contrôle du WAL à la fermeture.
    uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, detect_types=sqlite3.PARSE_DECLTYPES)
    try:
        conn.execute("PRAGMA query_only=ON")
        # `connect` et le pragma ne lisent pas le fichier : sans cette lecture, un fichier qui n'est
        # pas une base ne serait découvert qu'au premier contrôle, puis répété sur tous les autres.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except Exception:
        conn.close()
        raise
    return conn


# Mesures qui changent à chaque lancement sans que le PROBLÈME change (un message de file Shopify qui
# vieillit, un ticket de plus vendu). Elles restent dans le rapport mais n'entrent pas dans l'empreinte :
# sinon une bannière fermée reviendrait à chaque démarrage, et « nouveau » ne voudrait plus rien dire.
_MESURES_VOLATILES = {"age_heures", "libre_mo", "taille_octets", "wal_octets", "derniere_erreur",
                      "en_attente", "tickets", "ecriture_ok", "python", "plateforme"}


def _sans_mesures_volatiles(valeur: Any) -> Any:
    if isinstance(valeur, dict):
        return {k: _sans_mesures_volatiles(v) for k, v in valeur.items() if k not in _MESURES_VOLATILES}
    if isinstance(valeur, list):
        return [_sans_mesures_volatiles(v) for v in valeur]
    return valeur


def _empreinte_constat(c: Dict[str, Any]) -> str:
    stable = _sans_mesures_volatiles(c.get("donnees", {}))
    brut = json.dumps([c["id"], c["statut"], stable], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()[:16]


def _chemin_rapport(dossier: Optional[str]) -> str:
    if dossier is None:
        from kodo_core.config import ShopConfig
        dossier = os.path.join(ShopConfig.get_base_data_dir(), "diagnostics")
    return os.path.join(dossier, NOM_RAPPORT)


def _lire_json(chemin: str) -> Optional[dict]:
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _ecrire_atomique(chemin: str, data: dict) -> None:
    dossier = os.path.dirname(chemin)
    os.makedirs(dossier, exist_ok=True)
    # Nom unique : deux contrôles simultanés (lancement + « relancer » depuis l'API) ne s'écrasent plus.
    fd, tmp = tempfile.mkstemp(dir=dossier, prefix=NOM_RAPPORT + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, chemin)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _statut_global(constats: List[dict]) -> str:
    pire = max((_RANG[c["statut"]] for c in constats), default=0)
    # ERREUR (rang 1) = un contrôle n'a pas pu conclure : on ne peut pas dire « OK ».
    return {3: "CRITIQUE", 2: "ATTENTION", 1: "ATTENTION"}.get(pire, "OK")


def executer_controle(db_path: Optional[str] = None, dist_dir: Optional[str] = None,
                      niveau: str = NIVEAU_COMPLET, dossier_rapport: Optional[str] = None,
                      ecrire: bool = True) -> Dict[str, Any]:
    """Exécute le contrôle et retourne le rapport. Ne lève jamais."""
    debut = time.monotonic()
    if db_path is None:
        try:
            import database_manager
            db_path = database_manager.DB_NAME
        except Exception as e:  # noqa: BLE001
            db_path = ""
            logger.warning(f"Chemin de la base introuvable : {e}")

    ctx = _Contexte(db_path, dist_dir, niveau)
    constats: List[Dict[str, Any]] = []
    non_executes: List[str] = []
    base_illisible = False
    rang_niveau = {NIVEAU_RAPIDE: 0, NIVEAU_COMPLET: 1}

    base_presente = bool(db_path) and os.path.isfile(db_path)
    if not base_presente:
        constats.append(_constat("base.fichier", "Fichier de la base", CRITIQUE,
                                 f"Base de données introuvable : {_cacher_domicile(db_path)}",
                                 {"chemin": _cacher_domicile(db_path)},
                                 "Ne pas relancer d'import : contacter le support."))
    else:
        try:
            ctx.conn = _ouvrir_lecture_seule(db_path)
        except Exception as e:  # noqa: BLE001
            constats.append(_constat("base.ouverture", "Ouverture de la base", CRITIQUE,
                                     f"La base ne s'ouvre pas ou n'est pas une base SQLite valide : {_cacher_domicile(e)}",
                                     {}, "Ne pas la remplacer : restaurer une sauvegarde avec le support."))

    try:
        for id_, titre, niveau_min, besoin_base, fonction in CONTROLES:
            if rang_niveau.get(niveau_min, 1) > rang_niveau.get(niveau, 1):
                continue
            if besoin_base and ctx.conn is None:
                continue
            if id_ == "base.fichier" and not base_presente:
                continue
            if time.monotonic() - debut > BUDGET_SECONDES:
                non_executes.append(id_)
                continue
            if besoin_base and base_illisible:
                non_executes.append(id_)
                continue
            t0 = time.monotonic()
            try:
                resultat = fonction(ctx)
            except Exception as e:  # noqa: BLE001 — un contrôle qui plante est un constat, pas un plantage
                logger.warning(f"Contrôle {id_} en échec : {e}")
                resultat = _constat(id_, titre, ERREUR,
                                    f"Le contrôle n'a pas pu s'exécuter : {_cacher_domicile(e)[:200]}")
            resultat["duree_ms"] = int((time.monotonic() - t0) * 1000)
            constats.append(resultat)
            if id_ == "base.integrite" and resultat["statut"] == CRITIQUE:
                base_illisible = True  # les contrôles suivants échoueraient tous : un seul constat net suffit
    finally:
        if ctx.conn is not None:
            try:
                ctx.conn.close()
            except Exception:  # noqa: BLE001
                pass

    if non_executes and not base_illisible:
        # Jamais un « OK » silencieux : ce qui n'a pas tourné est dit, et compte dans le statut global.
        constats.append(_constat(
            "controle.incomplet", "Contrôle incomplet", ATTENTION,
            f"{len(non_executes)} contrôle(s) n'ont pas été exécutés (délai de {int(BUDGET_SECONDES)} s dépassé) : "
            + ", ".join(non_executes) + ".", {"non_executes": list(non_executes)},
            "Relancer le contrôle (Paramètres) ou transmettre ce rapport au support."))

    # Comparaison avec le contrôle précédent (le premier lancement n'a aucune référence : rien n'est « nouveau »).
    chemin = _chemin_rapport(dossier_rapport) if ecrire else None
    precedent = _lire_json(chemin) if chemin else None
    anciens = {c["id"]: c for c in (precedent or {}).get("controles", [])}
    nouveautes: List[str] = []
    for c in constats:
        c["empreinte"] = _empreinte_constat(c)
        ancien = anciens.get(c["id"])
        nouveau = precedent is not None and (ancien is None or ancien.get("empreinte") != c["empreinte"])
        c["nouveau"] = bool(nouveau) and c["statut"] != OK
        if c["alerter_si_nouveau"] and c["statut"] == INFO and c["nouveau"]:
            c["statut"] = ATTENTION
            c["detail"] += " — NOUVEAU depuis le contrôle précédent."
            c["empreinte"] = _empreinte_constat(c)
        if c["nouveau"]:
            nouveautes.append(c["id"])
    statut_par_id = {c["id"]: c["statut"] for c in constats}
    resolus = [i for i, a in anciens.items()
               if a.get("statut") in (ATTENTION, CRITIQUE) and statut_par_id.get(i) in (OK, INFO)]

    resume = Counter(c["statut"] for c in constats)
    alertes = sorted(c["empreinte"] for c in constats if c["statut"] in (ATTENTION, CRITIQUE))
    try:
        version = _version_installee(None)
    except Exception:  # noqa: BLE001
        version = ""
    rapport = {
        "schema": SCHEMA_RAPPORT,
        "genere_le": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duree_ms": int((time.monotonic() - debut) * 1000),
        "niveau": niveau,
        "version": version,
        "statut": _statut_global(constats),
        "resume": {k: resume.get(k, 0) for k in (OK, INFO, ATTENTION, CRITIQUE, ERREUR)},
        "controles": constats,
        "non_executes": non_executes,
        "raison_non_executes": "base illisible" if base_illisible else ("délai dépassé" if non_executes else ""),
        "nouveautes": nouveautes,
        "resolus": resolus,
        # Identifie l'ensemble des alertes : l'interface ne ré-affiche pas une bannière déjà fermée.
        "empreinte": hashlib.sha256("|".join(alertes).encode()).hexdigest()[:16] if alertes else "",
    }
    if chemin:
        try:
            _ecrire_atomique(chemin, rapport)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Rapport de contrôle non écrit : {e}")
    return rapport


# --------------------------------------------------------------------------------------------
# Thread de fond & accès
# --------------------------------------------------------------------------------------------

_VERROU = threading.Lock()
_ETAT: Dict[str, Any] = {"en_cours": False, "rapport": None}


def en_cours() -> bool:
    return bool(_ETAT["en_cours"])


def dernier_rapport(dossier_rapport: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Dernier rapport de CE processus, à défaut celui du lancement précédent (fichier)."""
    if _ETAT["rapport"] is not None:
        return _ETAT["rapport"]
    try:
        return _lire_json(_chemin_rapport(dossier_rapport))
    except Exception:  # noqa: BLE001
        return None


def _lancer(dist_dir_provider: Optional[Callable[[], str]], niveau: str, delai: float,
            db_path: Optional[str], dossier_rapport: Optional[str]) -> None:
    try:
        if delai > 0:
            time.sleep(delai)  # laisse le premier affichage de l'interface passer devant
        dist_dir = None
        if dist_dir_provider is not None:
            try:
                dist_dir = dist_dir_provider()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Dossier d'interface illisible : {e}")
        rapport = executer_controle(db_path=db_path, dist_dir=dist_dir, niveau=niveau, dossier_rapport=dossier_rapport)
        _ETAT["rapport"] = rapport
        sommaire = ", ".join(f"{n} {k}" for k, n in rapport["resume"].items() if n)
        print(f"🔎 [CONTRÔLE DE DÉMARRAGE] {rapport['statut']} — {sommaire} ({rapport['duree_ms']} ms)")
        for c in rapport["controles"]:
            if c["statut"] in (ATTENTION, CRITIQUE):
                print(f"   • [{c['statut'].upper()}] {c['titre']} : {c['detail']}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Contrôle de démarrage interrompu : {e}")
    finally:
        _ETAT["en_cours"] = False
        _VERROU.release()


def demarrer_en_arriere_plan(dist_dir_provider: Optional[Callable[[], str]] = None,
                             niveau: str = NIVEAU_COMPLET, delai: float = 4.0,
                             db_path: Optional[str] = None, dossier_rapport: Optional[str] = None) -> bool:
    """Lance le contrôle dans un thread démon. Retourne False s'il est désactivé ou déjà en cours.

    Le fournisseur du dossier d'interface est mémorisé : un nouveau contrôle demandé depuis l'API
    (sans accès à `server_pos`) réutilise celui du lancement."""
    if os.environ.get("KODO_STARTUP_CHECK", "1") == "0":
        return False
    if dist_dir_provider is not None:
        _ETAT["fournisseur_dist"] = dist_dir_provider
    else:
        dist_dir_provider = _ETAT.get("fournisseur_dist")
    if not _VERROU.acquire(blocking=False):
        return False
    _ETAT["en_cours"] = True
    try:
        threading.Thread(target=_lancer, args=(dist_dir_provider, niveau, delai, db_path, dossier_rapport),
                         name="kodo-startup-check", daemon=True).start()
    except Exception:  # noqa: BLE001
        _ETAT["en_cours"] = False
        _VERROU.release()
        raise
    return True


# --------------------------------------------------------------------------------------------
# Rendu
# --------------------------------------------------------------------------------------------

def resume_court(rapport: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Version légère pour `/api/system/health` : pas de détail, juste de quoi décider d'afficher."""
    if not rapport:
        return {"disponible": False, "en_cours": en_cours()}
    return {
        "disponible": True, "en_cours": en_cours(), "statut": rapport.get("statut"),
        "genere_le": rapport.get("genere_le"), "resume": rapport.get("resume", {}),
        "nouveautes": rapport.get("nouveautes", []), "empreinte": rapport.get("empreinte", ""),
        "non_executes": rapport.get("non_executes", []),
    }


def rendre_texte(rapport: Dict[str, Any]) -> str:
    """Rapport lisible à coller dans un message au support. Aucune donnée personnelle ni secret."""
    etiquettes = {CRITIQUE: "CRITIQUE", ATTENTION: "ATTENTION", ERREUR: "ERREUR  ", INFO: "info    ", OK: "ok      "}
    r = rapport.get("resume", {})
    lignes = [
        "RAPPORT DE CONTRÔLE — KŌDO POS",
        f"Généré le {rapport.get('genere_le', '?')} (niveau {rapport.get('niveau', '?')}, {rapport.get('duree_ms', '?')} ms)",
        f"Version : {rapport.get('version') or '?'}",
        f"Statut global : {rapport.get('statut', '?')} — "
        f"{r.get('critique', 0)} critique(s), {r.get('attention', 0)} attention, {r.get('erreur', 0)} contrôle(s) en erreur",
        "",
    ]
    ordre = sorted(rapport.get("controles", []), key=lambda c: -_RANG.get(c["statut"], 0))
    for c in ordre:
        marque = " (NOUVEAU)" if c.get("nouveau") else ""
        lignes.append(f"[{etiquettes.get(c['statut'], c['statut'])}] {c['titre']}{marque}")
        if c.get("detail"):
            lignes.append(f"           {c['detail']}")
        if c.get("action") and c["statut"] in (ATTENTION, CRITIQUE):
            lignes.append(f"           → {c['action']}")
    if rapport.get("non_executes"):
        lignes += ["", f"Non exécutés ({rapport.get('raison_non_executes') or 'raison inconnue'}) : "
                   + ", ".join(rapport["non_executes"])]
    if rapport.get("resolus"):
        lignes += ["", "Résolus depuis le contrôle précédent : " + ", ".join(rapport["resolus"])]
    return _cacher_domicile("\n".join(lignes))
