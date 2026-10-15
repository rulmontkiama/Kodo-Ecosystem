#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kōdo POS — contrôle de PRÉ-LIVRAISON (go / no-go).

À lancer sur VOTRE Mac, depuis la racine du dépôt, AVANT de publier une version :

  python3 scripts/release/preflight.py --version 2.1.2     # contrôle complet d'une release en préparation
  python3 scripts/release/preflight.py --rapide            # code seul, ~15 s : à lancer à chaque modification
  python3 scripts/release/preflight.py --version 2.1.2 --front ~/Desktop/kōdo-pos-3 --json rapport.json

Pourquoi ce script existe
-------------------------
Un défaut de CODE qui dort (un nom jamais défini sur un chemin d'erreur, une importation qui échoue
au premier usage, une migration avalée, un correctif publié qui n'est plus le code du dépôt…) ne se
voit pas à l'écran et ne peut pas être cherché chez le client : il faut l'arrêter AVANT la livraison.
Ce script enchaîne, en une commande, ce qui était fait à la main le 2026-10-10, et y ajoute les
contrôles statiques qui attrapent les défauts dormants.

Règles
------
- Il ne publie, ne signe et ne modifie RIEN. Les essais tournent dans des dossiers temporaires avec
  un HOME jetable ; la base réelle n'est jamais ouverte par SQLite, seulement copiée octet à octet.
- Verdict honnête : un contrôle qui n'a pas pu être fait est `NON VÉRIFIÉ`, jamais un succès.
  « GO » exige zéro échec et zéro contrôle manquant (sauf ceux écartés explicitement par l'option) ;
  les avertissements et les portes écartées sont REPRIS sous le verdict, jamais noyés. `--strict` fait
  d'un avertissement un échec.
- `--rapide` ne joue qu'une partie des portes : il ne rend JAMAIS « GO » (verdict RAPIDE, code 2).
- Code de sortie : 0 = GO, 1 = NO-GO (au moins un échec), 2 = INCOMPLET (contrôle(s) non vérifiés
  ou mode rapide).

Règle de croissance : un défaut réel trouvé un jour devient un contrôle ici (une fonction `g_*` + une
ligne dans `PORTES`) ou un test dans tests/.
"""
import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Callable, Dict, List, Optional, Set, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

PASS, FAIL, WARN, SKIP, IGNORE = "PASS", "FAIL", "WARN", "SKIP", "IGNORE"

FRONT_PAR_DEFAUT = os.path.expanduser("~/Desktop/kōdo-pos-3")
BASE_REELLE_PAR_DEFAUT = os.path.expanduser("~/Documents/Kodo_POS/db/kodo_pos.db")

# Points d'entrée du produit : ce que PyInstaller embarque est l'ensemble de modules qu'ils atteignent.
POINTS_ENTREE = ("launch_app.py", "server_pos.py", "patch_loader.py")
# Paquets entièrement embarqués par la spec PyInstaller.
PAQUETS_PRODUIT = ("kodo_core",)

EXCLUS = {".git", "node_modules", "__pycache__", ".claude", "tests", "dist", "build", "venv", ".venv",
          "snapshots", "public", "assets", ".agents", ".codex", ".next", "data", "product_images",
          "Installation_Pack", "Exports_L_ADRESSE_B", "Exports_Kodo_POS"}

# Tables dont le nombre de lignes ne doit JAMAIS changer pendant une migration.
TABLES_SENSIBLES = ("Produits", "Stocks", "Tickets", "Ventes_Details", "Ledger_Caisse", "Rapports_Z",
                    "Clotures_Caisse", "Audit_Trail", "Clients", "Vendeurs", "Cartes_Cadeaux")


class Resultat:
    def __init__(self, statut: str, detail: str = "", lignes: Optional[List[str]] = None):
        self.statut, self.detail, self.lignes = statut, detail, lignes or []


class Porte:
    def __init__(self, id_: str, titre: str, fonction: Callable, rapide: bool = False, groupe: str = "code"):
        self.id, self.titre, self.fonction, self.rapide, self.groupe = id_, titre, fonction, rapide, groupe


# --------------------------------------------------------------------------------------------
# Outils
# --------------------------------------------------------------------------------------------

def fichiers_python(racine: str, exclus: Set[str] = EXCLUS) -> List[str]:
    sortie = []
    for dossier, sous, fichiers in os.walk(racine):
        sous[:] = [d for d in sous if d not in exclus and not d.startswith("._")]
        for f in fichiers:
            if f.endswith(".py") and not f.startswith("._"):
                sortie.append(os.path.join(dossier, f))
    return sorted(sortie)


def lancer(cmd: List[str], cwd: str = ROOT, env: Optional[dict] = None, timeout: int = 1800) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 124, f"Délai de {timeout} s dépassé.\n{(e.stdout or b'')[-500:]!r}"
    except FileNotFoundError as e:
        return 127, str(e)


def queue(texte: str, n: int = 12) -> List[str]:
    return [l for l in texte.strip().splitlines() if l.strip()][-n:]


def env_isole(tmp: str) -> dict:
    """HOME jetable : aucun essai n'écrit dans ~/Documents/Kodo_POS, ~/.kodo_signing ou les caches."""
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ)
    env.update({"HOME": home, "PYTHONDONTWRITEBYTECODE": "1", "KODO_STARTUP_CHECK": "0",
                # sans cela, un HOME jetable ferait perdre les paquets installés « pour l'utilisateur » (pytest…)
                "PYTHONUSERBASE": site.getuserbase()})
    env.pop("KODO_DB_PATH", None)
    return env


# --------------------------------------------------------------------------------------------
# Analyse statique des importations (sans exécuter le code)
# --------------------------------------------------------------------------------------------

def _chemin_local(racine: str, nom: str) -> Optional[str]:
    """Fichier d'un module local (`a.b.c`), ou dossier d'un paquet (y compris « namespace »)."""
    base = os.path.join(racine, *nom.split("."))
    if os.path.isfile(base + ".py"):
        return base + ".py"
    if os.path.isfile(os.path.join(base, "__init__.py")):
        return os.path.join(base, "__init__.py")
    if os.path.isdir(base) and any(f.endswith(".py") for f in os.listdir(base)):
        return base
    return None


def _noms_de_premier_niveau(chemin: str, cache: Dict[str, Optional[Set[str]]]) -> Optional[Set[str]]:
    """Noms définis au niveau module (None = indéterminable : import étoile, __getattr__, globals())."""
    if chemin in cache:
        return cache[chemin]
    noms: Set[str] = set()
    resultat: Optional[Set[str]] = noms
    try:
        with open(chemin, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        arbre = ast.parse(source)
    except (SyntaxError, OSError):
        cache[chemin] = None
        return None
    if "globals()" in source or "setattr(sys.modules" in source:
        cache[chemin] = None
        return None

    def cibles(n):
        if isinstance(n, ast.Name):
            noms.add(n.id)
        elif isinstance(n, (ast.Tuple, ast.List)):
            for e in n.elts:
                cibles(e)
        elif isinstance(n, ast.Starred):
            cibles(n.value)

    def parcourir(corps):
        nonlocal resultat
        for n in corps:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                noms.add(n.name)
                if n.name == "__getattr__":
                    resultat = None
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    cibles(t)
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
                cibles(n.target)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    noms.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == "*":
                        resultat = None
                    else:
                        noms.add(a.asname or a.name)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                cibles(n.target)
                parcourir(n.body + n.orelse)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for it in n.items:
                    if it.optional_vars is not None:
                        cibles(it.optional_vars)
                parcourir(n.body)
            elif isinstance(n, ast.If):
                parcourir(n.body + n.orelse)
            elif isinstance(n, ast.Try):
                parcourir(n.body + n.orelse + n.finalbody)
                for h in n.handlers:
                    parcourir(h.body)
            elif isinstance(n, ast.While):
                parcourir(n.body + n.orelse)

    parcourir(arbre.body)
    cache[chemin] = resultat
    return resultat


def _garde_optionnel(chaine_ancetres: List[ast.AST]) -> bool:
    """
    Import volontairement facultatif : dans un try qui attrape ImportError, ou sous un test de plateforme.

    NE S'APPLIQUE QU'AUX MODULES TIERS (absents de l'installation). Un module ou un nom de NOTRE code est
    livré avec le produit : s'il manque, `except Exception: pass` ne le rend pas facultatif, il cache le
    défaut — c'est exactement le motif « Shopify jamais câblé ». Ces importations-là sont toujours jugées.
    """
    for i, noeud in enumerate(chaine_ancetres):
        if isinstance(noeud, ast.Try):
            suivant = chaine_ancetres[i + 1] if i + 1 < len(chaine_ancetres) else None
            # l'import doit être dans le CORPS du try (ni dans un handler, ni dans else/finally)
            if suivant is not None and any(suivant is s for s in noeud.body):
                for h in noeud.handlers:
                    t = h.type
                    noms = []
                    if t is None:
                        return True
                    for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
                        noms.append(getattr(e, "id", getattr(e, "attr", "")))
                    if any(n in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException", "OSError") for n in noms):
                        return True
        if isinstance(noeud, ast.If):
            texte = ast.dump(noeud.test)
            if any(m in texte for m in ("platform", "TYPE_CHECKING", "frozen", "os', ctx=Load()), attr='name'")):
                return True
    return False


def _importations(arbre: ast.AST):
    """(noeud, ancêtres) pour chaque import, y compris ceux faits à l'intérieur des fonctions."""
    pile: List[Tuple[ast.AST, List[ast.AST]]] = [(arbre, [])]
    while pile:
        noeud, anc = pile.pop()
        for enfant in ast.iter_child_nodes(noeud):
            if isinstance(enfant, (ast.Import, ast.ImportFrom)):
                yield enfant, anc + [noeud]
            pile.append((enfant, anc + [noeud]))


def analyser_importations(racine: str, entrees: Tuple[str, ...] = POINTS_ENTREE,
                          paquets: Tuple[str, ...] = PAQUETS_PRODUIT) -> Dict[str, List[str]]:
    """
    Parcourt, depuis les points d'entrée et les paquets du produit, TOUTES les importations — y compris
    celles écrites dans le corps d'une fonction, que rien n'exécute avant l'usage réel — et signale :
      - `introuvables` : module ni local, ni de la bibliothèque standard, ni installé ;
      - `noms_absents` : `from module_local import nom` où `nom` n'existe pas dans ce module.
    Ce sont exactement les défauts qui n'éclatent qu'au premier clic d'une fonctionnalité.
    """
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    cache_noms: Dict[str, Optional[Set[str]]] = {}
    a_traiter: List[str] = []
    vus: Set[str] = set()

    for e in entrees:
        p = os.path.join(racine, e)
        if os.path.isfile(p):
            a_traiter.append(p)
    for pq in paquets:
        base = os.path.join(racine, pq)
        if os.path.isdir(base):
            a_traiter += fichiers_python(base, EXCLUS)

    introuvables: Dict[str, List[str]] = {}
    noms_absents: Dict[str, List[str]] = {}
    nb_modules = 0

    def suivre_local(nom: str):
        p = _chemin_local(racine, nom)
        if p and os.path.isfile(p) and p not in vus:
            a_traiter.append(p)

    while a_traiter:
        chemin = a_traiter.pop()
        if chemin in vus:
            continue
        vus.add(chemin)
        try:
            with open(chemin, "r", encoding="utf-8", errors="replace") as f:
                arbre = ast.parse(f.read(), chemin)
        except SyntaxError:
            continue  # la porte « syntaxe » le signale
        nb_modules += 1
        rel = os.path.relpath(chemin, racine)
        paquet_courant = os.path.relpath(os.path.dirname(chemin), racine).replace(os.sep, ".")
        for noeud, ancetres in _importations(arbre):
            optionnel = _garde_optionnel(ancetres + [noeud])
            if isinstance(noeud, ast.Import):
                for a in noeud.names:
                    top = a.name.split(".")[0]
                    local = _chemin_local(racine, a.name)
                    if local:
                        suivre_local(a.name)
                    elif top in stdlib or _chemin_local(racine, top):
                        continue
                    elif importlib.util.find_spec(top) is None and not optionnel:
                        introuvables.setdefault(f"{a.name}", []).append(f"{rel}:{noeud.lineno}")
                continue

            # from X import a, b
            if noeud.level:
                parts = paquet_courant.split(".") if paquet_courant != "." else []
                parts = parts[: len(parts) - (noeud.level - 1)] if noeud.level > 1 else parts
                module = ".".join(parts + ([noeud.module] if noeud.module else []))
            else:
                module = noeud.module or ""
            if not module:
                continue
            top = module.split(".")[0]
            local = _chemin_local(racine, module)
            if not local:
                if top in stdlib or _chemin_local(racine, top):
                    # Paquet local dont le sous-module manque : défaut, qu'un `try` l'entoure ou non.
                    if top not in stdlib:
                        introuvables.setdefault(module, []).append(f"{rel}:{noeud.lineno}")
                    continue
                try:
                    absent = importlib.util.find_spec(top) is None
                except (ImportError, ValueError):
                    absent = True
                if absent and not optionnel:
                    introuvables.setdefault(module, []).append(f"{rel}:{noeud.lineno}")
                continue
            suivre_local(module)
            for a in noeud.names:
                if a.name == "*":
                    continue
                # sous-module ?
                if _chemin_local(racine, f"{module}.{a.name}"):
                    suivre_local(f"{module}.{a.name}")
                    continue
                if os.path.isdir(local):
                    continue  # paquet sans __init__ : on ne sait pas
                noms = _noms_de_premier_niveau(local, cache_noms)
                if noms is not None and a.name not in noms:
                    noms_absents.setdefault(f"{module}.{a.name}", []).append(f"{rel}:{noeud.lineno}")

    return {"introuvables": [f"{m}  ←  {', '.join(sorted(set(w))[:3])}" for m, w in sorted(introuvables.items())],
            "noms_absents": [f"{m}  ←  {', '.join(sorted(set(w))[:3])}" for m, w in sorted(noms_absents.items())],
            "modules_analyses": [str(nb_modules)]}


# --------------------------------------------------------------------------------------------
# Portes
# --------------------------------------------------------------------------------------------

def g_syntaxe(ctx) -> Resultat:
    fautes = []
    fichiers = fichiers_python(ctx.root, EXCLUS)
    for chemin in fichiers:
        try:
            with open(chemin, "rb") as f:
                compile(f.read(), chemin, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as e:
            fautes.append(f"{os.path.relpath(chemin, ctx.root)} : {e}")
    if fautes:
        return Resultat(FAIL, f"{len(fautes)} fichier(s) ne se compilent pas", fautes)
    return Resultat(PASS, f"{len(fichiers)} fichiers Python compilent")


def g_importations(ctx) -> Resultat:
    r = analyser_importations(ctx.root)
    lignes = [f"module introuvable : {x}" for x in r["introuvables"]] + \
             [f"nom absent du module : {x}" for x in r["noms_absents"]]
    if lignes:
        return Resultat(FAIL, f"{len(lignes)} importation(s) qui échoueront à l'usage", lignes)
    return Resultat(PASS, f"{r['modules_analyses'][0]} modules, toutes les importations se résolvent")


def g_filet_actif(ctx) -> Resultat:
    """Le filet de sécurité lui-même est-il encore câblé ?"""
    chemin = os.path.join(ctx.root, "server_pos.py")
    try:
        arbre = ast.parse(open(chemin, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return Resultat(FAIL, "server_pos.py illisible")
    absents = []
    # Un APPEL réel dans le code : le nom dans un commentaire ou une chaîne ne démarre rien.
    appelle = any(isinstance(n, ast.Call)
                  and getattr(n.func, "attr", getattr(n.func, "id", "")) == "demarrer_en_arriere_plan"
                  for n in ast.walk(arbre))
    if not appelle:
        absents.append("server_pos.py n'appelle plus demarrer_en_arriere_plan() (appel absent ou commenté)")
    routes = os.path.join(ctx.root, "kodo_core", "api", "routes", "system_routes.py")
    try:
        arbre_routes = ast.parse(open(routes, encoding="utf-8").read())
        # Chaîne littérale du CODE (un commentaire n'est pas dans l'arbre) : la route est réellement servie.
        if not any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "/api/system/diagnostic" in n.value
                   for n in ast.walk(arbre_routes)):
            absents.append("la route /api/system/diagnostic a disparu")
    except (OSError, SyntaxError):
        absents.append("system_routes.py illisible")
    if absents:
        return Resultat(FAIL, "le contrôle de démarrage n'est plus câblé", absents)
    return Resultat(PASS, "le contrôle de démarrage est lancé par le serveur et exposé par l'API")


def g_base_alignee(ctx) -> Resultat:
    def lire(rel, motif):
        try:
            m = re.search(motif, open(os.path.join(ctx.root, rel), encoding="utf-8").read(), re.M)
            return m.group(1) if m else None
        except OSError:
            return None
    courant = lire("kodo_core/services/updater.py", r'^CURRENT_VERSION\s*=\s*"([^"]+)"')
    base = lire("kodo_base.py", r'^BASE_VERSION\s*=\s*"([^"]+)"')
    if not courant or not base:
        return Resultat(FAIL, f"version illisible (updater : {courant}, kodo_base : {base})")
    if courant != base:
        return Resultat(FAIL, f"CURRENT_VERSION ({courant}) ≠ BASE_VERSION ({base}) : les correctifs backend "
                              "seraient refusés ou appliqués à la mauvaise base",
                        ["Corriger avec : python3 scripts/release/kodo_release.py stamp-base"])
    return Resultat(PASS, f"base du DMG alignée sur {base}")


def _pytest(ctx, cibles: List[str], timeout: int) -> Resultat:
    env = env_isole(ctx.tmp)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Les gardes textuelles de l'interface (ex. jeton Shopify hors du navigateur) lisent `src/` du front :
    # sans ce chemin elles se SAUTENT en silence sur une machine où le front n'est pas au chemin habituel.
    if getattr(ctx, "front", None):
        env["KODO_FRONT_SRC"] = os.path.join(str(ctx.front), "src")
    code, sortie = lancer([sys.executable, "-B", "-m", "pytest", *cibles, "-q", "-p", "no:cacheprovider", "--no-header"],
                          cwd=ctx.root, env=env, timeout=timeout)
    resume = queue(sortie, 1)[0] if queue(sortie, 1) else "(aucune sortie)"
    if code == 0:
        return Resultat(PASS, resume)
    echecs = [l for l in sortie.splitlines() if l.startswith(("FAILED ", "ERROR "))]
    return Resultat(FAIL, resume, echecs[:25] or queue(sortie, 15))


def g_golden(ctx) -> Resultat:
    return _pytest(ctx, ["tests/test_golden_contracts.py", "tests/test_noms_non_definis.py",
                         "tests/test_startup_check.py"], 600)


def g_suite_complete(ctx) -> Resultat:
    return _pytest(ctx, ["tests"], 3000)


def g_patching(ctx) -> Resultat:
    code, sortie = lancer([sys.executable, "-B", "tests_patching.py"], cwd=ctx.root, env=env_isole(ctx.tmp), timeout=900)
    resume = [l for l in sortie.splitlines() if "tests réussis" in l]
    if code == 0:
        return Resultat(PASS, resume[-1].strip() if resume else "ok")
    return Resultat(FAIL, resume[-1].strip() if resume else f"code {code}", queue(sortie, 15))


_SONDE_BASE_USINE = r"""
import json, os, sys
sys.path.insert(0, os.environ["KODO_ROOT"])
import database_manager as dm
dm.DB_NAME = os.environ["KODO_TMP_DB"]
os.environ["KODO_DB_PATH"] = dm.DB_NAME
dm.initialiser_db()
from kodo_core.services import startup_check as sc
r = sc.executer_controle(db_path=dm.DB_NAME, ecrire=False)
print("@@" + json.dumps({"statut": r["statut"], "resume": r["resume"],
      "alertes": [[c["id"], c["statut"], c["detail"]] for c in r["controles"] if c["statut"] in ("attention", "critique", "erreur")]}))
"""


def _sonde(ctx, code: str, extra_env: dict, timeout: int = 600) -> Tuple[Optional[dict], str]:
    env = env_isole(ctx.tmp)
    env.update({"KODO_ROOT": ctx.root, **extra_env})
    c, sortie = lancer([sys.executable, "-B", "-c", code], cwd=ctx.tmp, env=env, timeout=timeout)
    for ligne in sortie.splitlines():
        if ligne.startswith("@@"):
            return json.loads(ligne[2:]), sortie
    return None, sortie


def g_base_usine(ctx) -> Resultat:
    """Une base NEUVE fabriquée par ce code ne doit lever AUCUNE alerte du contrôle de démarrage."""
    db = os.path.join(ctx.tmp, "usine.db")
    res, sortie = _sonde(ctx, _SONDE_BASE_USINE, {"KODO_TMP_DB": db})
    if res is None:
        return Resultat(FAIL, "le contrôle n'a pas pu s'exécuter sur une base neuve", queue(sortie, 15))
    if res["statut"] != "OK" or res["resume"].get("erreur"):
        return Resultat(FAIL, f"statut {res['statut']} sur une base d'usine",
                        [f"[{s}] {i} : {d}" for i, s, d in res["alertes"]])
    return Resultat(PASS, "base d'usine : aucune alerte")


_SONDE_MIGRATION = r"""
import json, os, shutil, sqlite3, sys
sys.path.insert(0, os.environ["KODO_ROOT"])
src, dst = os.environ["KODO_SRC_DB"], os.environ["KODO_TMP_DB"]
shutil.copy2(src, dst)
for sfx in ("-wal", "-shm"):
    if os.path.exists(src + sfx):
        shutil.copy2(src + sfx, dst + sfx)
def compter(chemin):
    c = sqlite3.connect(chemin)
    try:
        out = {}
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            try: out[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except Exception: out[t] = None
        return out, c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()
avant, integ_avant = compter(dst)
import database_manager as dm
dm.DB_NAME = dst
os.environ["KODO_DB_PATH"] = dst
dm.initialiser_db()
apres, integ_apres = compter(dst)
from kodo_core.services import startup_check as sc
r = sc.executer_controle(db_path=dst, ecrire=False)
print("@@" + json.dumps({"avant": avant, "apres": apres, "integ_avant": integ_avant, "integ_apres": integ_apres,
      "statut": r["statut"], "alertes": [[c["id"], c["statut"], c["detail"]] for c in r["controles"]
                                         if c["statut"] in ("attention", "critique", "erreur")]}))
"""


def g_migration_copie(ctx) -> Resultat:
    """Migre une COPIE de la base réelle : rien ne se perd, la base reste saine, aucune alerte critique."""
    source = ctx.base_copie
    if not source or not os.path.isfile(source):
        return Resultat(SKIP, f"aucune base à essayer ({source or 'non précisée'}) — option --base-copie")
    db = os.path.join(ctx.tmp, "migree.db")
    res, sortie = _sonde(ctx, _SONDE_MIGRATION, {"KODO_SRC_DB": source, "KODO_TMP_DB": db})
    if res is None:
        return Resultat(FAIL, "la migration d'essai a échoué", queue(sortie, 15))
    problemes = []
    if res["integ_apres"] != "ok":
        problemes.append(f"intégrité SQLite après migration : {res['integ_apres']}")
    for t in TABLES_SENSIBLES:
        a, b = res["avant"].get(t), res["apres"].get(t)
        if a is not None and a != b:
            problemes.append(f"{t} : {a} ligne(s) avant, {b} après (PERTE OU AJOUT DE DONNÉES)")
    critiques = [f"[{s}] {i} : {d}" for i, s, d in res["alertes"] if s in ("critique", "erreur")]
    if problemes or critiques:
        return Resultat(FAIL, "la migration d'essai altère ou laisse la base incohérente", problemes + critiques)
    # Le bac à sable n'a volontairement PAS la clé de scellement de la machine (un secret ne se copie pas
    # dans un dossier jetable) : la chaîne NF525 ne peut donc pas y être vérifiée, ce n'est pas un défaut.
    notes = []
    attentions = []
    for i, s_, d in res["alertes"]:
        if s_ != "attention":
            continue
        if i == "ventes.chaine_nf525" and "clé de scellement" in d:
            notes.append("chaîne NF525 non vérifiée dans l'essai (clé de la machine volontairement absente du bac à sable)")
        else:
            attentions.append(f"[attention] {i} : {d}")
    tables = sum(1 for v in res["apres"].values() if v is not None)
    if attentions:
        return Resultat(WARN, f"migrée sans perte ({tables} tables) ; défauts hérités à connaître", attentions + notes)
    return Resultat(PASS, f"migrée sans perte ({tables} tables), aucune alerte", notes)


def g_front(ctx) -> Resultat:
    front = ctx.front
    if not front or not os.path.isdir(front):
        return Resultat(SKIP, f"dossier du front introuvable ({front}) — option --front")
    if not shutil.which("npm"):
        return Resultat(SKIP, "npm introuvable")
    if not os.path.isdir(os.path.join(front, "node_modules")):
        return Resultat(SKIP, f"node_modules absent dans {front} (npm install)")
    branche = lancer(["git", "branch", "--show-current"], cwd=front, timeout=20)[1].strip()
    # Le build va dans un dossier jetable : le dist/ du front (celui qu'on s'apprête peut-être à zipper)
    # n'est jamais écrasé par un simple contrôle.
    sortie_build = os.path.join(ctx.tmp, "front_dist")
    etapes = (("npm run lint (tsc --noEmit)", ["npm", "run", "lint"]),
              ("npm run build", ["npm", "run", "build", "--", "--outDir", sortie_build, "--emptyOutDir"]))
    for etape, cmd in etapes:
        code, sortie = lancer(cmd, cwd=front, timeout=900)
        if code != 0:
            return Resultat(FAIL, f"{etape} échoue (branche {branche or '?'})", queue(sortie, 20))
    neuf = os.path.join(sortie_build, "index.html")
    publie = os.path.join(ctx.root, "dist", "index.html")
    if not os.path.isfile(neuf):
        return Resultat(FAIL, f"le build a réussi mais n'a produit aucun index.html (branche {branche or '?'})")
    if not os.path.isfile(publie):
        # Sans ./dist, on ne peut pas dire que l'interface qui sera zippée est celle du front : pas un succès.
        return Resultat(WARN, f"lint + build sans erreur (branche {branche or '?'}), mais ./dist/index.html est "
                              "absent : l'interface à zipper n'a pas pu être comparée à ce build",
                        ["Copier dist/ du front vers ./dist (étape 3 de la procédure), puis relancer."])
    if open(neuf, "rb").read() != open(publie, "rb").read():
        return Resultat(WARN, f"lint + build sans erreur (branche {branche or '?'}), MAIS l'interface construite "
                              "depuis ce dossier n'est pas celle de ./dist",
                        ["Le zip OTA ne vient peut-être pas de cette branche du front : reconstruire puis recopier "
                         "dist/ (étapes 2-3), ou vérifier la branche."])
    return Resultat(PASS, f"lint + build sans erreur (branche {branche or '?'}), identique à ./dist")


def g_front_version(ctx) -> Resultat:
    front, attendu = ctx.front, ctx.version_publiee
    if not front or not os.path.isdir(front):
        return Resultat(SKIP, "dossier du front introuvable")
    if not attendu:
        return Resultat(SKIP, "version attendue inconnue (latest.json illisible)")
    # Seules les NOTES DE VERSION (première entrée = la plus récente) portent la version publiée.
    # Sidebar.tsx et App.tsx gardent la version de BASE du DMG (kodo_base.BASE_VERSION) : la version
    # réellement installée vient du backend (/api/version). Les exiger à jour ferait échouer une
    # release correcte (constaté sur release/2.1.1).
    rel = "src/components/Modals/WhatsNewModal.tsx"
    try:
        m = re.search(r'version:\s*["\']v?([0-9][0-9.]*)', open(os.path.join(front, rel), encoding="utf-8").read())
    except OSError:
        return Resultat(FAIL, f"{rel} illisible")
    trouvee = m.group(1) if m else None
    if trouvee != attendu:
        return Resultat(FAIL, f"les notes de version n'annoncent pas la version {attendu}",
                        [f"{rel} : première entrée = {trouvee or 'introuvable'} (attendu {attendu})"])
    return Resultat(PASS, f"les notes de version annoncent {attendu}")


def _json(chemin: str) -> Optional[dict]:
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def g_version_coherente(ctx) -> Resultat:
    """latest.json, la route Vercel et le nom des zips annoncent la MÊME version."""
    lj = _json(os.path.join(ctx.root, "public", "latest.json"))
    if not lj:
        return Resultat(FAIL, "public/latest.json illisible")
    v = lj.get("version")
    fautes = []
    for cle in ("latestVersion", "latest_version"):
        if lj.get(cle) != v:
            fautes.append(f"latest.json : {cle} = {lj.get(cle)} ≠ version {v}")
    for cle in ("distPatchUrl", "dist_patch_url"):
        if not str(lj.get(cle, "")).endswith(f"dist_v{v}.zip"):
            fautes.append(f"latest.json : {cle} ne vise pas dist_v{v}.zip")
    bp = lj.get("backendPatch")
    if bp:
        if bp.get("version") != v:
            fautes.append(f"latest.json : backendPatch.version = {bp.get('version')} ≠ {v}")
        if not str(bp.get("url", "")).endswith(f"backend_v{bp.get('version')}.zip"):
            fautes.append("latest.json : backendPatch.url incohérente avec sa version")
    route = os.path.join(ctx.root, "src", "app", "api", "version", "route.ts")
    try:
        txt = open(route, encoding="utf-8").read()
        for motif, attendu in ((r'\bversion:\s*"v?([0-9.]+)"', v), (r'latest_version:\s*"v?([0-9.]+)"', v)):
            m = re.search(motif, txt)
            if not m or m.group(1) != attendu:
                fautes.append(f"route.ts : {motif.split(':')[0].strip(chr(92)+'b')} = {m.group(1) if m else 'absent'} ≠ {attendu}")
        m = re.search(r'backendPatch:\s*\{\s*version:\s*"([0-9.]+)"', txt)
        if bp and (not m or m.group(1) != bp.get("version")):
            fautes.append(f"route.ts : backendPatch = {m.group(1) if m else 'absent'} ≠ latest.json {bp.get('version')}")
    except OSError:
        fautes.append("src/app/api/version/route.ts illisible")
    if ctx.version and ctx.version != v:
        fautes.append(f"la version préparée ({ctx.version}) n'est pas celle de latest.json ({v})")
    if fautes:
        return Resultat(FAIL, "les annonces de version divergent", fautes)
    return Resultat(PASS, f"latest.json, route.ts et noms de zips annoncent {v}")


def _entrees_zip(zf: zipfile.ZipFile) -> List[str]:
    return [i.filename for i in zf.infolist() if not i.is_dir()]


_SALE = re.compile(r"(^|/)(\._[^/]*|\.DS_Store|__MACOSX(/.*)?)$")


def _fichiers_modifies_hors_patch(ctx, pl, bundle, infos: List[str]) -> List[str]:
    """
    Fichiers patchables modifiés depuis la base du DMG (même sélection que `make-patch --since`) qui ne sont
    PAS dans le zip : un client corrigé à moitié, sans qu'aucun contrôle ne le voie. Renvoie [] quand la
    comparaison est impossible (étiquette de la base absente) — ce qui est dit dans `infos`.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts", "release"))
        import kodo_base
        import kodo_release as kr
        if os.path.abspath(kr.ROOT) != os.path.abspath(ctx.root):
            infos.append("dépôt contrôlé ≠ dépôt de l'outil de release : fichiers modifiés non comparés au patch")
            return []
        base_tag = f"v{kodo_base.BASE_VERSION}"
        code, _ = lancer(["git", "rev-parse", "--verify", "--quiet", base_tag + "^{commit}"], cwd=ctx.root, timeout=20)
        if code != 0:
            infos.append(f"étiquette {base_tag} absente : fichiers modifiés depuis la base non comparés au patch")
            return []
        attendus = []
        for rel in kr.changed_python_files(base_tag):
            rel = rel.replace(os.sep, "/")
            if rel.startswith(kr.NOT_MODULES):
                continue
            try:
                pl.validate_module_path(rel)
            except pl.PatchError:
                continue  # non patchable à distance (chargeur, updater…) : exclu par make-patch aussi
            attendus.append(rel)
        return sorted(set(attendus) - set(bundle.files))
    except Exception as e:  # noqa: BLE001 — la comparaison est un bonus de contrôle, jamais un plantage
        infos.append(f"comparaison des fichiers modifiés non faite ({type(e).__name__})")
        return []


def g_artefacts(ctx) -> Resultat:
    """Zips OTA publiés : signés, propres, et réellement égaux au code / à l'interface du dépôt."""
    v = ctx.version
    if not v:
        return Resultat(SKIP, "version préparée non précisée — option --version X.Y.Z")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import patch_loader as pl  # les outils de vérification sont ceux du dépôt qui porte CE script
    pub = os.path.join(ctx.root, "public")
    dist_zip, back_zip = os.path.join(pub, f"dist_v{v}.zip"), os.path.join(pub, f"backend_v{v}.zip")
    if not os.path.isfile(dist_zip):
        return Resultat(SKIP, f"{os.path.relpath(dist_zip, ctx.root)} pas encore fabriqué (étape 4 de la procédure)")
    fautes: List[str] = []
    infos: List[str] = []

    # --- dist ---
    payload = open(dist_zip, "rb").read()
    try:
        sig = open(dist_zip + ".sig", encoding="utf-8").read()
        if not pl.verify_signature("dist", v, payload, sig):
            fautes.append(f"dist_v{v}.zip : signature INVALIDE pour la version {v}")
    except OSError:
        fautes.append(f"dist_v{v}.zip.sig absent : les clients refuseront ce zip")
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
        noms = _entrees_zip(zf)
        sales = [n for n in noms if _SALE.search(n)]
        if sales:
            fautes.append(f"dist_v{v}.zip contient {len(sales)} fichier(s) parasites (._*, .DS_Store, __MACOSX) : {sales[:3]}")
        racine = "dist/" if all(n.startswith("dist/") for n in noms) else ""
        if f"{racine}index.html" not in noms:
            fautes.append(f"dist_v{v}.zip : index.html absent")
        else:
            html = zf.read(f"{racine}index.html").decode("utf-8", "replace")
            for rel in re.findall(r'src=["\']/?([^"\']+\.js)["\']', html):
                if f"{racine}{rel}" not in noms:
                    fautes.append(f"dist_v{v}.zip : le bundle {rel} cité par index.html est absent (écran blanc)")
        local = os.path.join(ctx.root, "dist")
        if os.path.isdir(local):
            ecarts = []
            for n in noms:
                if _SALE.search(n):
                    continue
                rel = n[len(racine):] if racine else n
                p = os.path.join(local, rel)
                if not os.path.isfile(p):
                    ecarts.append(f"{rel} absent de ./dist")
                elif hashlib.sha256(open(p, "rb").read()).hexdigest() != hashlib.sha256(zf.read(n)).hexdigest():
                    ecarts.append(f"{rel} diffère de ./dist")
            if ecarts:
                fautes.append(f"dist_v{v}.zip ≠ ./dist ({len(ecarts)} écart(s), ex. {ecarts[:2]}) : zip périmé ?")
            infos.append(f"dist_v{v}.zip : {len(noms)} fichiers, signature valide")
    except zipfile.BadZipFile:
        fautes.append(f"dist_v{v}.zip illisible")

    # --- backend ---
    if os.path.isfile(back_zip):
        payload = open(back_zip, "rb").read()
        try:
            sig = open(back_zip + ".sig", encoding="utf-8").read()
            if not pl.verify_signature("backend", v, payload, sig):
                fautes.append(f"backend_v{v}.zip : signature INVALIDE pour la version {v}")
        except OSError:
            fautes.append(f"backend_v{v}.zip.sig absent")
        try:
            bundle = pl.parse_bundle(payload)  # structure, empreintes et syntaxe de chaque fichier
            import kodo_base
            if bundle.version != v:
                fautes.append(f"backend_v{v}.zip : manifeste en version {bundle.version}")
            bmin, bmax = bundle.manifest["base_min"], bundle.manifest["base_max"]
            if not (pl.parse_version(bmin) <= pl.parse_version(kodo_base.BASE_VERSION) <= pl.parse_version(bmax)):
                fautes.append(f"backend_v{v}.zip : bases {bmin}→{bmax} n'incluent PAS la base du DMG "
                              f"({kodo_base.BASE_VERSION}) : aucun client ne l'appliquerait")
            manquants = _fichiers_modifies_hors_patch(ctx, pl, bundle, infos)
            if manquants:
                fautes.append(f"backend_v{v}.zip OUBLIE {len(manquants)} fichier(s) modifié(s) depuis la base du DMG : "
                              f"{manquants[:5]} — le refaire (make-patch --since)")
            perimes = []
            for rel, data in bundle.files.items():
                p = os.path.join(ctx.root, rel)
                if not os.path.isfile(p):
                    perimes.append(f"{rel} (n'existe plus dans le dépôt)")
                elif open(p, "rb").read() != data:
                    perimes.append(f"{rel} (modifié depuis la fabrication du patch)")
            if perimes:
                fautes.append(f"backend_v{v}.zip n'est plus le code du dépôt : {perimes[:5]} — le refaire (make-patch)")
            infos.append(f"backend_v{v}.zip : {len(bundle.files)} fichier(s), bases {bmin}→{bmax}, signature valide")
        except pl.PatchError as e:
            fautes.append(f"backend_v{v}.zip invalide : {e}")
    else:
        infos.append(f"pas de backend_v{v}.zip (release interface seule)")

    if fautes:
        return Resultat(FAIL, "artefacts de la version " + v + " non conformes", fautes)
    return Resultat(PASS, f"artefacts {v} conformes", infos)


_MOTIFS_SECRETS = [
    ("clé privée", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ED25519 )?PRIVATE KEY-----")),
    ("jeton Shopify", re.compile(r"\bshp(?:at|ss|ca|pa)_[0-9a-fA-F]{32}\b")),
    ("clé AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jeton GitHub", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("clé API", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
]
_EXT_BINAIRES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".dmg", ".db", ".woff", ".woff2", ".ttf",
                 ".otf", ".pdf", ".mp4", ".mov", ".icns", ".pyc", ".sig", ".bin", ".gz", ".tar"}
_NOMS_SENSIBLES = re.compile(r"(^|/)(\.env(\..*)?|.*\.pem|.*\.p12|.*\.key|.*adminsdk.*\.json|id_rsa|id_ed25519)$", re.I)


def _fichiers_suivis(racine: str) -> Optional[List[str]]:
    code, sortie = lancer(["git", "ls-files", "-z"], cwd=racine, timeout=60)
    if code != 0:
        return None
    return [f for f in sortie.split("\0") if f]


def g_secrets(ctx) -> Resultat:
    suivis = _fichiers_suivis(ctx.root)
    if suivis is None:
        return Resultat(SKIP, "git ls-files indisponible")
    fautes = []
    for rel in suivis:
        if _NOMS_SENSIBLES.search(rel):
            fautes.append(f"fichier sensible suivi par git : {rel}")
            continue
        if os.path.splitext(rel)[1].lower() in _EXT_BINAIRES:
            continue
        p = os.path.join(ctx.root, rel)
        try:
            if os.path.getsize(p) > 3_000_000 or not os.path.isfile(p):
                continue
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for nom, motif in _MOTIFS_SECRETS:
            if motif.search(txt):
                fautes.append(f"{nom} dans {rel}")
    if fautes:
        return Resultat(FAIL, f"{len(fautes)} secret(s) potentiel(s) dans les fichiers suivis", fautes)
    return Resultat(PASS, f"{len(suivis)} fichiers suivis : aucun secret reconnu")


def g_donnees_suivies(ctx) -> Resultat:
    """Données commerciales (exports, bases, caches de licence) suivies par git : le dépôt est PUBLIC."""
    suivis = _fichiers_suivis(ctx.root)
    if suivis is None:
        return Resultat(SKIP, "git ls-files indisponible")
    motif = re.compile(r"(^|/)(Exports?_[^/]*/|license_cache\.json$)|\.(db|sqlite|sqlite3|csv|xlsx)$", re.I)
    trouves = [f for f in suivis if motif.search(f) and not f.startswith(("tests/", "docs/")) and "/fixtures/" not in f]
    if trouves:
        return Resultat(FAIL, f"{len(trouves)} fichier(s) de données suivi(s) par git (le dépôt est PUBLIC)",
                        trouves[:15] + ["Retirer du suivi : git rm --cached <fichier> (le fichier reste sur le disque)"])
    return Resultat(PASS, "aucun export, base ni cache de licence suivi par git")


def g_fichiers_parasites(ctx) -> Resultat:
    """
    `._*` (AppleDouble) et .DS_Store que macOS sème sur un disque externe : ignorés par git, mais un
    `zip -r dist/` les embarque. C'est un AVERTISSEMENT sur le disque (le zip peut être fait en les
    excluant) ; le zip FINAL, lui, est jugé sans appel par la porte « artefacts ».
    """
    dist, paquets = [], []
    for dossier, _d, fichiers in os.walk(os.path.join(ctx.root, "dist")):
        for f in fichiers:
            if f.startswith("._") or f == ".DS_Store":
                dist.append(os.path.relpath(os.path.join(dossier, f), ctx.root))
    for pq in ("kodo_core", "core", "services"):
        for dossier, _d, fichiers in os.walk(os.path.join(ctx.root, pq)):
            for f in fichiers:
                if f.startswith("._"):
                    paquets.append(os.path.relpath(os.path.join(dossier, f), ctx.root))
    if dist:
        return Resultat(WARN, f"{len(dist)} fichier(s) parasite(s) dans dist/ : un zip fait sans les exclure les embarquerait",
                        dist[:10] + ["Zipper avec : zip -r -X dist_vX.Y.Z.zip dist -x '*/._*' -x '*.DS_Store'"])
    if paquets:
        return Resultat(WARN, f"{len(paquets)} fichier(s) ._* dans les paquets Python (ignorés par git, à garder hors du build)",
                        paquets[:8])
    return Resultat(PASS, "aucun fichier parasite dans dist/ ni dans les paquets Python")


PORTES: List[Porte] = [
    Porte("syntaxe", "Tous les fichiers Python se compilent", g_syntaxe, rapide=True),
    Porte("importations", "Toutes les importations se résolvent (y compris celles faites dans les fonctions)", g_importations, rapide=True),
    Porte("filet_actif", "Le contrôle de démarrage est encore câblé", g_filet_actif, rapide=True),
    Porte("base_alignee", "Base du DMG alignée (CURRENT_VERSION = BASE_VERSION)", g_base_alignee, rapide=True),
    Porte("golden", "Golden contracts, noms non définis, contrôle de démarrage", g_golden, rapide=True),
    Porte("base_usine", "Une base neuve ne lève aucune alerte", g_base_usine, rapide=True),
    Porte("suite", "Suite de tests complète", g_suite_complete),
    Porte("patching", "Mécanisme de patchs signés (tests_patching.py)", g_patching),
    Porte("migration", "Migration d'une copie de la base réelle", g_migration_copie, groupe="donnees"),
    Porte("secrets", "Aucun secret dans les fichiers suivis", g_secrets, groupe="depot"),
    Porte("donnees_suivies", "Aucune donnée commerciale suivie par git", g_donnees_suivies, groupe="depot"),
    Porte("parasites", "Aucun fichier parasite (._*, .DS_Store) à zipper", g_fichiers_parasites, groupe="depot"),
    Porte("version", "Annonces de version cohérentes (latest.json, route.ts)", g_version_coherente, groupe="release"),
    Porte("artefacts", "Zips OTA signés, propres et égaux au dépôt", g_artefacts, groupe="release"),
    Porte("front", "Interface : lint (tsc) + build", g_front, groupe="front"),
    Porte("front_version", "Interface : version annoncée = version publiée", g_front_version, groupe="front"),
]


# --------------------------------------------------------------------------------------------
# Exécution & verdict
# --------------------------------------------------------------------------------------------

class Contexte:
    def __init__(self, racine, tmp, version, front, base_copie, version_publiee):
        self.root, self.tmp, self.version = racine, tmp, version
        self.front, self.base_copie, self.version_publiee = front, base_copie, version_publiee


def executer(args, sortie=print) -> Tuple[int, dict]:
    racine = os.path.abspath(args.root or ROOT)
    tmp = tempfile.mkdtemp(prefix="kodo_preflight_")
    lj = _json(os.path.join(racine, "public", "latest.json")) or {}
    ctx = Contexte(racine, tmp, args.version, os.path.expanduser(args.front) if args.front else FRONT_PAR_DEFAUT,
                   os.path.expanduser(args.base_copie) if args.base_copie else BASE_REELLE_PAR_DEFAUT,
                   # version que l'interface doit annoncer : celle en préparation, sinon la dernière publiée
                   args.version or lj.get("version"))
    ignores = set(args.ignorer or [])
    if args.sans_front:
        ignores |= {"front", "front_version"}
    if args.sans_artefacts:
        ignores |= {"artefacts"}
    selection = [p for p in PORTES if (p.rapide or not args.rapide)]
    rapport = {"racine": racine, "version": args.version, "mode": "rapide" if args.rapide else "complet", "portes": []}
    sortie(f"\n═══ PRÉ-LIVRAISON KŌDO POS — {'contrôle rapide (code seul)' if args.rapide else 'contrôle complet'}"
           f"{' · version ' + args.version if args.version else ''} ═══\n")
    debut = time.time()
    try:
        for p in selection:
            t0 = time.time()
            if p.id in ignores:
                r = Resultat(IGNORE, "écarté à la demande")
            else:
                try:
                    r = p.fonction(ctx)
                except Exception as e:  # noqa: BLE001 — une porte qui plante est un échec, pas un succès muet
                    r = Resultat(FAIL, f"la porte a planté : {type(e).__name__}: {e}")
            marque = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "⏭ ", IGNORE: "➖"}[r.statut]
            sortie(f"{marque} {p.titre}\n     {r.detail}  ({time.time() - t0:.1f} s)")
            for l in r.lignes[:25]:
                sortie(f"       · {l}")
            rapport["portes"].append({"id": p.id, "titre": p.titre, "statut": r.statut, "detail": r.detail,
                                      "lignes": r.lignes[:50], "duree_s": round(time.time() - t0, 1)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    nb = {s: sum(1 for x in rapport["portes"] if x["statut"] == s) for s in (PASS, FAIL, WARN, SKIP, IGNORE)}
    non_jouees = [p.id for p in PORTES if p not in selection]
    strict_echecs = nb[WARN] if getattr(args, "strict", False) else 0
    if nb[FAIL] or strict_echecs:
        verdict, code = "NO-GO", 1
    elif nb[SKIP]:
        verdict, code = "INCOMPLET", 2
    elif args.rapide:
        # Une partie seulement des portes a tourné : ce n'est PAS un GO, quoi qu'elles disent.
        verdict, code = "RAPIDE", 2
    elif nb[PASS] == 0:
        # Tout écarté (ou rien joué) : aucune garantie n'a été donnée.
        verdict, code = "INCOMPLET", 2
    else:
        verdict, code = "GO", 0
    rapport.update(verdict=verdict, code=code, comptes=nb, duree_s=round(time.time() - debut, 1),
                   portes_non_jouees=non_jouees,
                   avertissements=[x["id"] for x in rapport["portes"] if x["statut"] == WARN],
                   portes_ecartees=[x["id"] for x in rapport["portes"] if x["statut"] == IGNORE])
    sortie(f"\n{'═' * 70}\nVERDICT : {verdict}  —  {nb[PASS]} passé(s), {nb[WARN]} avertissement(s), "
           f"{nb[FAIL]} échec(s), {nb[SKIP]} non vérifié(s), {nb[IGNORE]} écarté(s)  ({rapport['duree_s']} s)")
    if verdict == "NO-GO":
        sortie("→ NE PAS PUBLIER : corrigez les échecs ci-dessus puis relancez.")
    elif verdict == "INCOMPLET":
        manquants = [x["id"] for x in rapport["portes"] if x["statut"] == SKIP]
        sortie("→ Des contrôles n'ont pas pu être faits : " + ", ".join(manquants)
               + ". Fournissez ce qu'ils demandent, ou écartez-les explicitement (--ignorer ID).")
    elif verdict == "RAPIDE":
        sortie(f"→ Contrôle RAPIDE : aucun défaut dans les {len(selection)} portes jouées, mais "
               f"{len(non_jouees)} porte(s) n'ont PAS tourné ({', '.join(non_jouees)}). Ce n'est pas un GO : "
               "lancez le contrôle complet (--version X.Y.Z) avant de publier.")
    else:
        sortie("→ Aucun défaut détecté par ces contrôles (ils ne garantissent pas l'absence de défaut).")
    # Ce qui ne bloque pas mais mérite d'être lu : repris ICI, sous le verdict.
    if rapport["avertissements"]:
        sortie("⚠️  Avertissements à lire avant de publier : " + ", ".join(rapport["avertissements"]))
    if rapport["portes_ecartees"]:
        sortie("➖ Portes écartées à la demande (non jouées) : " + ", ".join(rapport["portes_ecartees"]))
    sortie("═" * 70 + "\n")
    return code, rapport


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Contrôle de pré-livraison Kōdo POS (go / no-go).")
    ap.add_argument("--version", help="version en préparation, ex. 2.1.2 (active le contrôle des artefacts)")
    ap.add_argument("--rapide", action="store_true", help="code seul (~15 s) : statique, golden contracts, base d'usine")
    ap.add_argument("--front", help=f"dossier du front React (défaut : {FRONT_PAR_DEFAUT})")
    ap.add_argument("--base-copie", help="base SQLite à COPIER pour l'essai de migration "
                                         f"(défaut : {BASE_REELLE_PAR_DEFAUT}, jamais ouverte en place)")
    ap.add_argument("--sans-front", action="store_true", help="écarte explicitement les contrôles du front")
    ap.add_argument("--sans-artefacts", action="store_true", help="écarte explicitement le contrôle des zips")
    ap.add_argument("--ignorer", action="append", metavar="ID", help="écarte une porte (répétable)")
    ap.add_argument("--strict", action="store_true", help="un avertissement devient un échec (NO-GO)")
    ap.add_argument("--json", metavar="FICHIER", help="écrit le rapport en JSON")
    ap.add_argument("--root", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    code, rapport = executer(args)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rapport, f, ensure_ascii=False, indent=2)
        print(f"Rapport écrit : {args.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
