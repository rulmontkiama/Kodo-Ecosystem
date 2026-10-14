# -*- coding: utf-8 -*-
"""
scripts/release/preflight.py — le contrôle de pré-livraison.

Un contrôle qui répond toujours « GO » est pire que pas de contrôle. Chaque porte est donc éprouvée
ici sur un dépôt SYNTHÉTIQUE où le défaut est planté : elle doit le voir, et ne doit pas crier sur
le cas sain voisin.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from unittest import mock

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, RACINE)

_spec = importlib.util.spec_from_file_location("kodo_preflight", os.path.join(RACINE, "scripts", "release", "preflight.py"))
pf = importlib.util.module_from_spec(_spec)
sys.modules["kodo_preflight"] = pf
_spec.loader.exec_module(pf)


class Depot(unittest.TestCase):
    """Dépôt synthétique dans un dossier jetable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kodo_preflight_test_")
        self.root = os.path.join(self.tmp, "depot")
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ecrire(self, rel, contenu, binaire=False):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb" if binaire else "w", **({} if binaire else {"encoding": "utf-8"})) as f:
            f.write(contenu if binaire else textwrap.dedent(contenu))
        return p

    def ctx(self, **kw):
        base = dict(racine=self.root, tmp=self.tmp, version=None, front=None, base_copie=None, version_publiee=None)
        base.update(kw)
        return pf.Contexte(**base)


class TestAnalyseDesImportations(Depot):

    def analyser(self):
        return pf.analyser_importations(self.root, entrees=("launch_app.py",), paquets=("kodo_core",))

    def test_module_introuvable_dans_une_fonction_est_vu(self):
        self.ecrire("launch_app.py", """
            def plus_tard():
                import module_qui_n_existe_nulle_part_xyz
        """)
        r = self.analyser()
        self.assertEqual(len(r["introuvables"]), 1)
        self.assertIn("module_qui_n_existe_nulle_part_xyz", r["introuvables"][0])
        self.assertIn("launch_app.py:3", r["introuvables"][0])

    def test_import_facultatif_n_est_pas_signale(self):
        self.ecrire("launch_app.py", """
            try:
                import module_facultatif_xyz
            except ImportError:
                module_facultatif_xyz = None
            import sys
            if sys.platform == "win32":
                import win32api_inexistant_ici
        """)
        self.assertEqual(self.analyser()["introuvables"], [])

    def test_import_dans_un_try_qui_n_attrape_pas_importerror_est_signale(self):
        self.ecrire("launch_app.py", """
            try:
                import module_xyz_absent
            except ValueError:
                pass
        """)
        self.assertEqual(len(self.analyser()["introuvables"]), 1)

    def test_import_dans_finally_ou_else_n_est_pas_facultatif(self):
        self.ecrire("launch_app.py", """
            try:
                pass
            except ImportError:
                pass
            else:
                import module_xyz_dans_else
            finally:
                import module_xyz_dans_finally
        """)
        self.assertEqual(len(self.analyser()["introuvables"]), 2)

    def test_nom_absent_d_un_module_local_est_vu_meme_en_import_paresseux(self):
        self.ecrire("launch_app.py", "from kodo_core import services_x\n")
        self.ecrire("kodo_core/__init__.py", "")
        self.ecrire("kodo_core/services_x.py", """
            def existe(): ...
            class Aussi: ...
        """)
        self.ecrire("kodo_core/routes.py", """
            def handler():
                from kodo_core.services_x import existe, a_ete_renomme
                return existe, a_ete_renomme
        """)
        r = self.analyser()
        self.assertEqual(r["noms_absents"], ["kodo_core.services_x.a_ete_renomme  ←  kodo_core/routes.py:3"])

    def test_noms_definis_de_toutes_les_facons_ne_sont_pas_signales(self):
        self.ecrire("launch_app.py", "")
        self.ecrire("kodo_core/__init__.py", "")
        self.ecrire("kodo_core/m.py", """
            import json as js
            from os import path as chemin
            CONSTANTE = 1
            a, (b, c) = 1, (2, 3)
            if CONSTANTE:
                conditionnel = 1
            try:
                import zlib as compresseur
            except ImportError:
                compresseur = None
            for boucle in range(1):
                pass
            with open(__file__) as fichier:
                pass
            def f(): ...
            class C: ...
            typé: int = 3
        """)
        self.ecrire("kodo_core/usage.py", """
            from kodo_core.m import (js, chemin, CONSTANTE, a, b, c, conditionnel, compresseur,
                                     boucle, fichier, f, C, typé)
        """)
        self.assertEqual(self.analyser()["noms_absents"], [])

    def test_sous_module_et_import_relatif(self):
        self.ecrire("launch_app.py", "")
        self.ecrire("kodo_core/__init__.py", "")
        self.ecrire("kodo_core/db/__init__.py", "")
        self.ecrire("kodo_core/db/connexion.py", "def ouvrir(): ...\n")
        self.ecrire("kodo_core/db/usage.py", """
            from kodo_core.db import connexion
            from .connexion import ouvrir
            from .connexion import fantome
        """)
        r = self.analyser()
        self.assertEqual(len(r["noms_absents"]), 1)
        self.assertIn("fantome", r["noms_absents"][0])

    def test_modules_dynamiques_ou_etoile_ne_declenchent_pas_de_faux_positif(self):
        self.ecrire("launch_app.py", "")
        self.ecrire("kodo_core/__init__.py", "")
        self.ecrire("kodo_core/etoile.py", "from os.path import *\n")
        self.ecrire("kodo_core/dyn.py", "globals()['x'] = 1\n")
        self.ecrire("kodo_core/lazy.py", "def __getattr__(n): ...\n")
        self.ecrire("kodo_core/usage.py", """
            from kodo_core.etoile import join
            from kodo_core.dyn import x
            from kodo_core.lazy import n
        """)
        self.assertEqual(self.analyser()["noms_absents"], [])

    def test_stdlib_et_paquets_installes_se_resolvent(self):
        self.ecrire("launch_app.py", "import json\nimport unittest\nimport pytest\nfrom collections import OrderedDict\n")
        self.assertEqual(self.analyser()["introuvables"], [])


class TestPortesStatiques(Depot):

    def test_syntaxe(self):
        self.ecrire("bon.py", "x = 1\n")
        self.assertEqual(pf.g_syntaxe(self.ctx()).statut, pf.PASS)
        self.ecrire("casse.py", "def f(:\n")
        r = pf.g_syntaxe(self.ctx())
        self.assertEqual(r.statut, pf.FAIL)
        self.assertIn("casse.py", r.lignes[0])

    def test_base_alignee(self):
        self.ecrire("kodo_core/services/updater.py", 'CURRENT_VERSION = "2.0.5"\n')
        self.ecrire("kodo_base.py", 'BASE_VERSION = "2.0.5"\n')
        self.assertEqual(pf.g_base_alignee(self.ctx()).statut, pf.PASS)
        self.ecrire("kodo_base.py", 'BASE_VERSION = "2.0.4"\n')
        self.assertEqual(pf.g_base_alignee(self.ctx()).statut, pf.FAIL)

    def test_filet_actif(self):
        self.ecrire("server_pos.py", "from kodo_core.services import startup_check\nstartup_check.demarrer_en_arriere_plan()\n")
        self.ecrire("kodo_core/api/routes/system_routes.py", 'p = "/api/system/diagnostic"\n')
        self.assertEqual(pf.g_filet_actif(self.ctx()).statut, pf.PASS)
        self.ecrire("server_pos.py", "print('serveur')\n")
        self.assertEqual(pf.g_filet_actif(self.ctx()).statut, pf.FAIL)

    def test_fichiers_parasites(self):
        self.ecrire("dist/index.html", "<html></html>")
        self.assertEqual(pf.g_fichiers_parasites(self.ctx()).statut, pf.PASS)
        self.ecrire("dist/assets/._index.js", "x")
        r = pf.g_fichiers_parasites(self.ctx())
        self.assertEqual(r.statut, pf.WARN, "sur le disque c'est un avertissement : le zip final est jugé par `artefacts`")
        self.assertIn("._index.js", r.lignes[0])
        self.assertTrue(any("-x '*/._*'" in l for l in r.lignes), "la commande de zip sûre doit être donnée")

    def test_parasite_dans_public_n_est_pas_un_risque_de_zip(self):
        self.ecrire("public/._backend_v2.1.1.zip", "x")
        self.assertEqual(pf.g_fichiers_parasites(self.ctx()).statut, pf.PASS)

    def test_parasite_dans_un_paquet_python_n_est_qu_un_avertissement(self):
        self.ecrire("kodo_core/__init__.py", "")
        self.ecrire("kodo_core/._module.py", "x")
        self.assertEqual(pf.g_fichiers_parasites(self.ctx()).statut, pf.WARN)


class TestVersionEtDepot(Depot):

    def route(self, v="2.1.2", bp="2.1.2"):
        self.ecrire("src/app/api/version/route.ts", f'''
            export const dynamic = "force-dynamic";
            const data = {{
              version: "v{v}",
              latest_version: "v{v}",
              backendPatch: {{ version: "{bp}", url: "https://x/public/backend_v{bp}.zip" }},
            }};
        ''')

    def latest(self, v="2.1.2", dist=None, bp=None):
        d = {"version": v, "latestVersion": v, "latest_version": v,
             "distPatchUrl": f"https://x/public/dist_v{dist or v}.zip", "dist_patch_url": f"https://x/public/dist_v{dist or v}.zip",
             "backendPatch": {"version": bp or v, "url": f"https://x/public/backend_v{bp or v}.zip"}}
        self.ecrire("public/latest.json", json.dumps(d))

    def test_versions_coherentes(self):
        self.latest()
        self.route()
        self.assertEqual(pf.g_version_coherente(self.ctx(version="2.1.2")).statut, pf.PASS)

    def test_zip_oublie_dans_latest_json(self):
        self.latest(dist="2.1.1")
        self.route()
        r = pf.g_version_coherente(self.ctx())
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("dist_v2.1.2.zip" in l for l in r.lignes))

    def test_route_vercel_pas_mise_a_jour(self):
        self.latest()
        self.route(v="2.1.1", bp="2.1.1")
        r = pf.g_version_coherente(self.ctx())
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("route.ts" in l for l in r.lignes))

    def test_version_preparee_differente_de_celle_annoncee(self):
        self.latest()
        self.route()
        self.assertEqual(pf.g_version_coherente(self.ctx(version="2.1.3")).statut, pf.FAIL)

    def _git(self, *a):
        subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True)

    def test_secrets_et_donnees_suivies(self):
        self._git("init", "-q")
        self.ecrire("propre.py", "x = 1\n")
        self.ecrire("Exports_Client/ventes.csv", "a;b\n")
        self._git("add", "-A")
        self.assertEqual(pf.g_secrets(self.ctx()).statut, pf.PASS)
        r = pf.g_donnees_suivies(self.ctx())
        self.assertEqual(r.statut, pf.WARN)
        self.assertIn("Exports_Client/ventes.csv", r.lignes)

        self.ecrire("config.py", 'TOKEN = "shpat_' + "a" * 32 + '"\n')
        self.ecrire("cle.txt", "-----BEGIN PRIVATE KEY-----\nxx\n")
        self.ecrire("firebase-adminsdk-abc.json", "{}")
        self._git("add", "-A")
        r = pf.g_secrets(self.ctx())
        self.assertEqual(r.statut, pf.FAIL)
        texte = " | ".join(r.lignes)
        self.assertIn("jeton Shopify dans config.py", texte)
        self.assertIn("clé privée dans cle.txt", texte)
        self.assertIn("firebase-adminsdk-abc.json", texte)

    def test_secret_non_suivi_par_git_n_est_pas_signale(self):
        self._git("init", "-q")
        self.ecrire("propre.py", "x = 1\n")
        self._git("add", "-A")
        self.ecrire("local_non_suivi.txt", "shpat_" + "b" * 32)
        self.assertEqual(pf.g_secrets(self.ctx()).statut, pf.PASS)


class TestArtefacts(Depot):
    """Zips OTA réellement signés avec une clé de test, vérifiés par le vrai patch_loader."""

    V = "2.1.2"

    def setUp(self):
        super().setUp()
        import kodo_base
        import kodo_ed25519
        import patch_loader as pl
        self.pl, self.kodo_base = pl, kodo_base
        self.secret = os.urandom(32)
        self.pub = kodo_ed25519.public_key_from_secret(self.secret).hex()
        self.kodo_ed25519 = kodo_ed25519
        patcher = mock.patch.object(kodo_base, "TRUSTED_PUBLIC_KEYS", [self.pub])
        patcher.start()
        self.addCleanup(patcher.stop)

        # dépôt : une interface construite, un module Python
        self.ecrire("dist/index.html", '<html><script type="module" src="/assets/index-1.js"></script></html>')
        self.ecrire("dist/assets/index-1.js", "console.log(1)")
        self.ecrire("kodo_core/services/exemple.py", "VALEUR = 1\n")
        self.fabriquer()

    def signer(self, kind, payload, version=None):
        msg = self.pl.signed_message(kind, version or self.V, payload)
        return base64.b64encode(self.kodo_ed25519.sign(self.secret, msg)).decode()

    def fabriquer(self, base_min=None, base_max=None, sans_sig=False, parasite=False, sans_bundle=False):
        base = self.kodo_base.BASE_VERSION
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dist/index.html", open(os.path.join(self.root, "dist/index.html")).read())
            if not sans_bundle:
                zf.writestr("dist/assets/index-1.js", open(os.path.join(self.root, "dist/assets/index-1.js")).read())
            if parasite:
                zf.writestr("dist/assets/._index-1.js", b"\x00\x05\x16\x07")
        dist = buf.getvalue()
        self.ecrire(f"public/dist_v{self.V}.zip", dist, binaire=True)
        if not sans_sig:
            self.ecrire(f"public/dist_v{self.V}.zip.sig", self.signer("dist", dist) + "\n")

        data = open(os.path.join(self.root, "kodo_core/services/exemple.py"), "rb").read()
        manifest = {"format": self.pl.FORMAT_VERSION, "version": self.V, "base_min": base_min or base,
                    "base_max": base_max or base, "notes": "",
                    "files": {"kodo_core/services/exemple.py": hashlib.sha256(data).hexdigest()}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("kodo_core/services/exemple.py", data)
        back = buf.getvalue()
        self.ecrire(f"public/backend_v{self.V}.zip", back, binaire=True)
        self.ecrire(f"public/backend_v{self.V}.zip.sig", self.signer("backend", back) + "\n")

    def porte(self, version=None):
        return pf.g_artefacts(self.ctx(version=version))

    def test_artefacts_conformes(self):
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.PASS, r.lignes)

    def test_sans_version_la_porte_n_invente_rien(self):
        self.assertEqual(self.porte(None).statut, pf.SKIP)

    def test_zip_pas_encore_fabrique_est_non_verifie(self):
        os.remove(os.path.join(self.root, f"public/dist_v{self.V}.zip"))
        self.assertEqual(self.porte(self.V).statut, pf.SKIP)

    def test_signature_absente(self):
        self.fabriquer(sans_sig=True)
        os.remove(os.path.join(self.root, f"public/dist_v{self.V}.zip.sig"))
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any(".sig absent" in l for l in r.lignes))

    def test_signature_pour_une_autre_version(self):
        dist = open(os.path.join(self.root, f"public/dist_v{self.V}.zip"), "rb").read()
        self.ecrire(f"public/dist_v{self.V}.zip.sig", self.signer("dist", dist, version="9.9.9") + "\n")
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("INVALIDE" in l for l in r.lignes))

    def test_cle_inconnue_des_clients(self):
        with mock.patch.object(self.kodo_base, "TRUSTED_PUBLIC_KEYS", ["00" * 32]):
            self.assertEqual(self.porte(self.V).statut, pf.FAIL)

    def test_fichier_parasite_dans_le_zip(self):
        self.fabriquer(parasite=True)
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("parasites" in l for l in r.lignes))

    def test_bundle_manquant_dans_le_zip_ecran_blanc(self):
        self.fabriquer(sans_bundle=True)
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("écran blanc" in l for l in r.lignes))

    def test_interface_reconstruite_apres_le_zip(self):
        self.ecrire("dist/assets/index-1.js", "console.log('reconstruit apres le zip')")
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("≠ ./dist" in l for l in r.lignes))

    def test_patch_perime_par_rapport_au_depot(self):
        self.ecrire("kodo_core/services/exemple.py", "VALEUR = 2\n")
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("n'est plus le code du dépôt" in l for l in r.lignes))

    def test_patch_qui_n_inclut_pas_la_base_du_dmg(self):
        self.fabriquer(base_min="1.0.0", base_max="1.9.9")
        r = self.porte(self.V)
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("aucun client ne l'appliquerait" in l for l in r.lignes))


class TestFront(Depot):

    def faux_npm(self, code):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        p = os.path.join(bin_dir, "npm")
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\necho \"npm $@\"\n{'echo erreur TS2304 >&2' if code else ''}\nexit {code}\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        return bin_dir

    def front(self):
        front = os.path.join(self.tmp, "front")
        os.makedirs(os.path.join(front, "node_modules"))
        return front

    def test_lint_en_echec(self):
        front = self.front()
        with mock.patch.dict(os.environ, {"PATH": self.faux_npm(2) + os.pathsep + os.environ["PATH"]}):
            r = pf.g_front(self.ctx(front=front))
        self.assertEqual(r.statut, pf.FAIL)
        self.assertTrue(any("TS2304" in l for l in r.lignes))

    def test_lint_et_build_ok(self):
        front = self.front()
        with mock.patch.dict(os.environ, {"PATH": self.faux_npm(0) + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(pf.g_front(self.ctx(front=front)).statut, pf.PASS)

    def test_front_absent_est_non_verifie_pas_reussi(self):
        self.assertEqual(pf.g_front(self.ctx(front=os.path.join(self.tmp, "nulle_part"))).statut, pf.SKIP)
        front = os.path.join(self.tmp, "front_sans_modules")
        os.makedirs(front)
        self.assertEqual(pf.g_front(self.ctx(front=front)).statut, pf.SKIP)

    def test_version_annoncee_par_l_interface(self):
        front = os.path.join(self.tmp, "front")
        os.makedirs(os.path.join(front, "src/components/Modals"))
        open(os.path.join(front, "src/components/Sidebar.tsx"), "w").write('  currentVersion = "2.1.2",\n')
        open(os.path.join(front, "src/components/Modals/WhatsNewModal.tsx"), "w").write('  version: "v2.1.2",\n')
        open(os.path.join(front, "src/App.tsx"), "w").write("localStorage.setItem('kodo_installed_version', '2.1.2');\n")
        self.assertEqual(pf.g_front_version(self.ctx(front=front, version_publiee="2.1.2")).statut, pf.PASS)
        open(os.path.join(front, "src/App.tsx"), "w").write("localStorage.setItem('kodo_installed_version', '2.0.5');\n")
        r = pf.g_front_version(self.ctx(front=front, version_publiee="2.1.2"))
        self.assertEqual(r.statut, pf.FAIL)
        self.assertIn("src/App.tsx", r.lignes[0])


class TestDonnees(Depot):

    def _sonde(self, avant, apres, integ="ok", alertes=()):
        res = {"avant": avant, "apres": apres, "integ_avant": "ok", "integ_apres": integ, "statut": "OK",
               "alertes": [list(a) for a in alertes]}
        return mock.patch.object(pf, "_sonde", return_value=(res, ""))

    def source(self):
        p = os.path.join(self.tmp, "src.db")
        open(p, "wb").write(b"x")
        return p

    def test_sans_base_a_essayer_la_porte_ne_reussit_pas_en_silence(self):
        self.assertEqual(pf.g_migration_copie(self.ctx(base_copie=None)).statut, pf.SKIP)

    def test_migration_sans_perte(self):
        with self._sonde({"Tickets": 10, "Produits": 5}, {"Tickets": 10, "Produits": 5, "sync_queue": 0}):
            self.assertEqual(pf.g_migration_copie(self.ctx(base_copie=self.source())).statut, pf.PASS)

    def test_perte_de_lignes_sur_une_table_sensible(self):
        with self._sonde({"Tickets": 10}, {"Tickets": 9}):
            r = pf.g_migration_copie(self.ctx(base_copie=self.source()))
        self.assertEqual(r.statut, pf.FAIL)
        self.assertIn("Tickets", r.lignes[0])

    def test_corruption_apres_migration(self):
        with self._sonde({"Tickets": 1}, {"Tickets": 1}, integ="*** in database main ***"):
            self.assertEqual(pf.g_migration_copie(self.ctx(base_copie=self.source())).statut, pf.FAIL)

    def test_alerte_critique_du_controle_de_demarrage(self):
        with self._sonde({"Tickets": 1}, {"Tickets": 1},
                         alertes=[("base.derive_schema", "critique", "table absente : sync_queue")]):
            r = pf.g_migration_copie(self.ctx(base_copie=self.source()))
        self.assertEqual(r.statut, pf.FAIL)

    def test_cle_de_scellement_absente_du_bac_a_sable_n_est_pas_un_defaut(self):
        alerte = ("ventes.chaine_nf525", "attention",
                  "2 ticket(s) mais la clé de scellement de cette machine est absente : la chaîne ne peut pas être vérifiée ici")
        with self._sonde({"Tickets": 2}, {"Tickets": 2}, alertes=[alerte]):
            r = pf.g_migration_copie(self.ctx(base_copie=self.source()))
        self.assertEqual(r.statut, pf.PASS)
        self.assertTrue(any("non vérifiée" in l for l in r.lignes), "l'absence de vérification doit rester dite")

    def test_defaut_herite_est_un_avertissement_pas_un_echec(self):
        with self._sonde({"Tickets": 1}, {"Tickets": 1},
                         alertes=[("base.cles_etrangeres", "attention", "2 lignes orphelines")]):
            r = pf.g_migration_copie(self.ctx(base_copie=self.source()))
        self.assertEqual(r.statut, pf.WARN)
        self.assertIn("2 lignes orphelines", r.lignes[0])

    def test_vraie_migration_d_une_copie_de_base(self):
        """Bout en bout, sans simulation : une base neuve « vieillie » est migrée sans perte."""
        import sqlite3
        db = os.path.join(self.tmp, "ancienne.db")
        env = pf.env_isole(self.tmp)
        env.update({"KODO_ROOT": RACINE, "KODO_TMP_DB": db})
        code = ("import os, sys; sys.path.insert(0, os.environ['KODO_ROOT']); import database_manager as dm; "
                "dm.DB_NAME = os.environ['KODO_TMP_DB']; os.environ['KODO_DB_PATH'] = dm.DB_NAME; dm.initialiser_db()")
        p = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE sync_queue")
        conn.execute("DELETE FROM schema_version WHERE version IN ('2.0.9', '2.1.0')")
        conn.execute("INSERT INTO Produits (nom, prix_vente_tvac) VALUES ('Robe', 59.9)")
        conn.commit()
        conn.close()
        r = pf.g_migration_copie(self.ctx(racine=RACINE, base_copie=db))
        self.assertEqual(r.statut, pf.PASS, r.lignes)


class TestVerdict(unittest.TestCase):

    def lancer(self, portes, **kw):
        args = mock.Mock(root=None, version=None, front=None, base_copie=None, ignorer=None, sans_front=False,
                         sans_artefacts=False, rapide=False)
        for k, v in kw.items():
            setattr(args, k, v)
        with mock.patch.object(pf, "PORTES", portes):
            return pf.executer(args, sortie=lambda *_: None)

    @staticmethod
    def porte(id_, statut, rapide=False):
        return pf.Porte(id_, id_, lambda ctx: pf.Resultat(statut, "d"), rapide=rapide)

    def test_go(self):
        code, r = self.lancer([self.porte("a", pf.PASS), self.porte("b", pf.WARN)])
        self.assertEqual((code, r["verdict"]), (0, "GO"))

    def test_un_echec_suffit_pour_no_go(self):
        code, r = self.lancer([self.porte("a", pf.PASS), self.porte("b", pf.FAIL)])
        self.assertEqual((code, r["verdict"]), (1, "NO-GO"))

    def test_un_controle_non_verifie_donne_incomplet_jamais_go(self):
        code, r = self.lancer([self.porte("a", pf.PASS), self.porte("b", pf.SKIP)])
        self.assertEqual((code, r["verdict"]), (2, "INCOMPLET"))

    def test_l_echec_l_emporte_sur_le_non_verifie(self):
        code, _ = self.lancer([self.porte("a", pf.SKIP), self.porte("b", pf.FAIL)])
        self.assertEqual(code, 1)

    def test_ecarter_une_porte_est_explicite(self):
        code, r = self.lancer([self.porte("a", pf.PASS), self.porte("front", pf.SKIP)], sans_front=True)
        self.assertEqual((code, r["verdict"]), (0, "GO"))
        self.assertEqual(r["comptes"]["IGNORE"], 1)

    def test_une_porte_qui_plante_est_un_echec(self):
        def boom(ctx):
            raise RuntimeError("défaut imprévu")
        code, r = self.lancer([pf.Porte("x", "x", boom)])
        self.assertEqual(code, 1)
        self.assertIn("défaut imprévu", r["portes"][0]["detail"])

    def test_mode_rapide_ne_lance_que_les_portes_rapides(self):
        code, r = self.lancer([self.porte("rapide", pf.PASS, rapide=True), self.porte("lente", pf.FAIL)], rapide=True)
        self.assertEqual((code, [p["id"] for p in r["portes"]]), (0, ["rapide"]))

    def test_le_catalogue_de_portes_est_coherent(self):
        ids = [p.id for p in pf.PORTES]
        self.assertEqual(len(ids), len(set(ids)), "identifiants de portes en double")
        self.assertTrue(any(p.rapide for p in pf.PORTES))


if __name__ == "__main__":
    unittest.main()
