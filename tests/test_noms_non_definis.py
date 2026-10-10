# -*- coding: utf-8 -*-
"""
Barrière anti-régression : AUCUN nom utilisé sans être défini dans le code de production.

Un nom non défini ne plante qu'à l'instant où la ligne s'exécute. C'est le défaut classique
« NameError au premier échec réel » : le chemin d'erreur n'est exercé par aucun test, et le jour
où il l'est (disque plein, dossier refusé par macOS, réseau coupé) le logiciel plante au lieu de
se rabattre. Cas réel trouvé par cette barrière : `logger` utilisé quatre fois dans la route
d'export du catalogue Shopify sans jamais être défini, donc un refus d'écriture dans
~/Downloads (autorisation macOS) faisait échouer tout l'export au lieu d'utiliser le dossier
d'export de repli.

Analyse statique minimale (esprit « pyflakes », sans dépendance) sur kodo_core et les modules
racine. Les tests, scripts et dossiers générés sont exclus.
"""
import ast
import builtins
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXCLURE = {".git", "node_modules", "__pycache__", ".claude", "tests", "dist", "build", "venv", ".venv",
           "scripts", "snapshots", "public", "assets"}
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__path__", "__builtins__", "__spec__",
                                 "__package__", "__class__", "__loader__"}


def cibles(noeud, out):
    """Noms liés par une cible d'affectation / for / with / import."""
    if isinstance(noeud, ast.Name):
        out.add(noeud.id)
    elif isinstance(noeud, (ast.Tuple, ast.List)):
        for e in noeud.elts:
            cibles(e, out)
    elif isinstance(noeud, ast.Starred):
        cibles(noeud.value, out)


class Portee:
    def __init__(self, parent=None, est_classe=False):
        self.noms, self.parent, self.est_classe = set(), parent, est_classe
        self.globaux = set()

    def definis(self, nom):
        p, premier = self, True
        while p is not None:
            if nom in p.noms and (premier or not p.est_classe):
                return True
            p, premier = p.parent, False
        return False


class Analyse(ast.NodeVisitor):
    def __init__(self, fichier, etoile):
        self.fichier, self.problemes, self.etoile = fichier, [], etoile
        self.portee = Portee()

    # --- collecte des liaisons d'un corps (sans descendre dans les fonctions imbriquées)
    def collecter(self, corps, portee):
        pile = list(corps) if isinstance(corps, list) else [corps]
        while pile:
            n = pile.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                portee.noms.add(n.name)
                continue
            if isinstance(n, ast.ClassDef):
                portee.noms.add(n.name)
                continue
            if isinstance(n, ast.Lambda):
                continue
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    portee.noms.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.Assign,)):
                for t in n.targets:
                    cibles(t, portee.noms)
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                cibles(n.target, portee.noms)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                cibles(n.target, portee.noms)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        cibles(item.optional_vars, portee.noms)
            elif isinstance(n, ast.ExceptHandler):
                if n.name:
                    portee.noms.add(n.name)
            elif isinstance(n, ast.NamedExpr):
                cibles(n.target, portee.noms)
            elif isinstance(n, ast.Global):
                portee.noms.update(n.names)
                portee.globaux.update(n.names)
            elif isinstance(n, ast.Nonlocal):
                portee.noms.update(n.names)
            elif isinstance(n, (ast.Match,)):
                pass
            for e in ast.iter_child_nodes(n):
                if not isinstance(e, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    pile.append(e)
                else:
                    pile.append(e)

    def analyser_module(self, arbre):
        self.collecter(arbre.body, self.portee)
        # un `global x` dans une fonction définit x au niveau module
        for n in ast.walk(arbre):
            if isinstance(n, ast.Global):
                self.portee.noms.update(n.names)
        for n in arbre.body:
            self.visit(n)

    def _fonction(self, n):
        p = Portee(self.portee)
        a = n.args
        for x in a.posonlyargs + a.args + a.kwonlyargs:
            p.noms.add(x.arg)
        if a.vararg: p.noms.add(a.vararg.arg)
        if a.kwarg: p.noms.add(a.kwarg.arg)
        corps = n.body if isinstance(n.body, list) else [n.body]
        self.collecter(corps, p)
        # défauts et décorateurs s'évaluent dans la portée englobante
        for d in a.defaults + [d for d in a.kw_defaults if d is not None]:
            self.visit(d)
        for d in getattr(n, "decorator_list", []):
            self.visit(d)
        anc, self.portee = self.portee, p
        for c in corps:
            self.visit(c)
        self.portee = anc

    def visit_FunctionDef(self, n): self._fonction(n)
    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = _fonction

    def visit_ClassDef(self, n):
        for b in n.bases: self.visit(b)
        for d in n.decorator_list: self.visit(d)
        p = Portee(self.portee, est_classe=True)
        self.collecter(n.body, p)
        anc, self.portee = self.portee, p
        for c in n.body: self.visit(c)
        self.portee = anc

    def _compr(self, n):
        p = Portee(self.portee)
        for g in n.generators:
            cibles(g.target, p.noms)
        anc, self.portee = self.portee, p
        for g in n.generators:
            self.visit(g.iter)
            for i in g.ifs: self.visit(i)
        if isinstance(n, ast.DictComp):
            self.visit(n.key); self.visit(n.value)
        else:
            self.visit(n.elt)
        self.portee = anc

    visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _compr

    def visit_Name(self, n):
        if isinstance(n.ctx, ast.Load) and not self.etoile:
            if n.id not in BUILTINS and not self.portee.definis(n.id):
                self.problemes.append((n.lineno, n.id))


def fichiers_production():
    for dossier, sous, fichiers in os.walk(RACINE):
        sous[:] = [d for d in sous if d not in EXCLURE and not d.startswith("._")]
        for f in fichiers:
            if f.endswith(".py") and not f.startswith("._"):
                yield os.path.join(dossier, f)


def noms_non_definis(chemin):
    source = open(chemin, encoding="utf-8").read()
    arbre = ast.parse(source, chemin)
    etoile = any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(arbre))
    a = Analyse(os.path.relpath(chemin, RACINE), etoile)
    a.analyser_module(arbre)
    return sorted(set(a.problemes))


class TestAucunNomNonDefini(unittest.TestCase):

    def test_le_code_de_production_n_utilise_aucun_nom_non_defini(self):
        trouves = []
        nb = 0
        for chemin in sorted(fichiers_production()):
            nb += 1
            for ligne, nom in noms_non_definis(chemin):
                trouves.append(f"{os.path.relpath(chemin, RACINE)}:{ligne}  {nom}")
        self.assertGreater(nb, 50, "l'analyse n'a presque rien lu : la barrière serait muette")
        self.assertEqual(trouves, [], "noms utilisés sans être définis :\n" + "\n".join(trouves))

    def test_l_analyseur_detecte_bien_un_nom_non_defini(self):
        """La barrière elle-même est testée : sur un fichier fautif, elle doit crier."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write("import os\n\ndef route():\n    try:\n        os.getcwd()\n"
                    "    except Exception as e:\n        logger.warning(e)\n")
            chemin = f.name
        try:
            self.assertEqual([nom for _, nom in noms_non_definis(chemin)], ["logger"])
        finally:
            os.remove(chemin)


class TestExportShopifyRepliSiTelechargementsRefuse(unittest.TestCase):
    """Le chemin d'erreur qui plantait : copie vers ~/Downloads refusée -> repli sur le dossier d'export."""

    def setUp(self):
        import database_manager
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        self._old_db = database_manager.DB_NAME
        database_manager.DB_NAME = self.path
        os.environ["KODO_DB_PATH"] = self.path
        database_manager.initialiser_db()
        conn = database_manager.get_connection()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO Produits (code_barre, nom, categorie, prix_vente_tvac, taux_tva) "
                      "VALUES ('EXP-1', 'Robe export', 'Test', '49.00', '0.21')")
            c.execute("INSERT INTO Stocks (id_produit, taille, quantite_actuelle) VALUES (?, 'M', 3)", (c.lastrowid,))
            conn.commit()
        finally:
            conn.close()
        self.downloads = os.path.expanduser("~/Downloads")
        os.makedirs(self.downloads, exist_ok=True)   # HOME jetable (conftest)

    def tearDown(self):
        import database_manager
        os.environ.pop("KODO_DB_PATH", None)
        database_manager.DB_NAME = self._old_db
        os.close(self.fd)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.remove(self.path + suffix)

    def test_un_refus_d_ecriture_dans_telechargements_n_empeche_pas_l_export(self):
        from kodo_core.api.routes.products_routes import handle_products_request
        import shutil
        copie_reelle = shutil.copyfile

        def copie_refusee_vers_downloads(src, dst, *a, **k):
            if os.path.dirname(os.path.abspath(dst)) == os.path.abspath(self.downloads):
                raise PermissionError("Operation not permitted (autorisation macOS)")
            return copie_reelle(src, dst, *a, **k)

        with mock.patch("shutil.copyfile", side_effect=copie_refusee_vers_downloads):
            statut, corps = handle_products_request(
                "POST", "/api/products/export/shopify", {}, {"action": "save", "status": "active", "reveal": False})

        self.assertEqual(statut, 200, corps)
        self.assertTrue(corps["success"])
        self.assertTrue(os.path.exists(corps["filePath"]), "aucun fichier d'export de repli n'a été écrit")
        self.assertNotEqual(os.path.dirname(os.path.abspath(corps["filePath"])), os.path.abspath(self.downloads))
        os.remove(corps["filePath"])


if __name__ == "__main__":
    unittest.main()
