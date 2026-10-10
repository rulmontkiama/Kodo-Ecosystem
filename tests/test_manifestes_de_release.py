# -*- coding: utf-8 -*-
"""
Kōdo POS — Les manifestes de release sont valides, cohérents entre eux, et ne promettent que des fichiers qui existent.

Cas réel : la release 2.0.6 a livré un `src/app/api/version/route.ts` dont la chaîne `changelog` était
coupée par de VRAIS sauts de ligne (une ligne vide écrite à la main entre deux blocs de version) au lieu
de `\\n`. Un littéral entre guillemets ne peut pas s'étendre sur plusieurs lignes : « SyntaxError » à la
construction du site. Aucun test ne le voyait, ce fichier n'étant exécuté que par Vercel.

`public/latest.json` (lu par chaque app installée) et `route.ts` (recopié dans le site) sont de plus
renseignés À LA MAIN, l'un après l'autre : ils doivent annoncer la même version, le même patch backend
et le même texte, et `latest.json` ne doit renvoyer vers aucun zip absent du dépôt.
"""
import json
import os
import re
import unittest

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PUBLIC = os.path.join(RACINE, "public")
LATEST = os.path.join(PUBLIC, "latest.json")
ROUTE = os.path.join(RACINE, "src", "app", "api", "version", "route.ts")

_CHAINE = r'"(?:[^"\\\n]|\\.)*"'


def lire(chemin):
    with open(chemin, encoding="utf-8") as f:
        return f.read()


def ligne_d_une_chaine_coupee(source):
    """
    Numéro (à partir de 1) de la ligne où DÉBUTE une chaîne '…' ou "…" contenant un saut de ligne
    brut, sinon None. Les commentaires et les gabarits `…` (multilignes par nature) sont ignorés ;
    un `\\` suivi d'un saut de ligne (continuation de ligne) reste légal.
    """
    etat, debut, ligne, i, n = None, 0, 1, 0, len(source)
    while i < n:
        c = source[i]
        if etat is None:
            if c == "/" and source[i + 1:i + 2] == "/":
                etat = "//"
            elif c == "/" and source[i + 1:i + 2] == "*":
                etat = "/*"
                i += 1
            elif c in "\"'`":
                etat, debut = c, ligne
        elif etat == "//":
            if c == "\n":
                etat = None
        elif etat == "/*":
            if c == "*" and source[i + 1:i + 2] == "/":
                etat = None
                i += 1
        else:                                   # dans une chaîne
            if c == "\\":
                if source[i + 1:i + 2] == "\n":
                    ligne += 1
                i += 1
            elif c == etat:
                etat = None
            elif c == "\n" and etat != "`":
                return debut
        if c == "\n":
            ligne += 1
        i += 1
    return None


def champs_de_la_route(source):
    """Les champs `nom: "valeur"` de la réponse de la route, plus `backendPatch`."""
    champs = {}
    for m in re.finditer(r'^[ \t]*(\w+):[ \t]*(' + _CHAINE + ')', source, re.MULTILINE):
        champs[m.group(1)] = json.loads(m.group(2))
    bp = re.search(r'backendPatch:\s*\{\s*version:\s*(' + _CHAINE + r')\s*,\s*url:\s*(' + _CHAINE + r')\s*\}', source)
    if bp:
        champs["backendPatch"] = {"version": json.loads(bp.group(1)), "url": json.loads(bp.group(2))}
    return champs


class TestManifestesDeRelease(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source_route = lire(ROUTE)
        cls.latest = json.loads(lire(LATEST))      # un JSON invalide fait échouer toute la classe, nommément
        cls.route = champs_de_la_route(cls.source_route)

    # --- l'outil de contrôle lui-même ---------------------------------------------------------

    def test_le_detecteur_voit_une_chaine_coupee_et_ignore_le_reste(self):
        self.assertEqual(ligne_d_une_chaine_coupee('a = 1;\nb = "x\ny";'), 2)
        self.assertEqual(ligne_d_une_chaine_coupee("c = 'abc\ndef';"), 1)
        # Légaux : \n échappé, continuation de ligne, commentaires avec apostrophe, gabarit multiligne.
        self.assertIsNone(ligne_d_une_chaine_coupee('b = "x\\ny"; // l\'import\nc = `a\nb`;'))
        self.assertIsNone(ligne_d_une_chaine_coupee('d = "ligne un \\\nligne deux";'))
        self.assertIsNone(ligne_d_une_chaine_coupee('s = "il a dit \\"non\\""; t = \'l"a\'; /* l\'a\nb */'))

    # --- route.ts : doit être du code valide --------------------------------------------------

    def test_route_ts_n_a_aucune_chaine_coupee_par_un_saut_de_ligne(self):
        ligne = ligne_d_une_chaine_coupee(self.source_route)
        self.assertIsNone(
            ligne,
            f"route.ts : la chaîne qui commence ligne {ligne} contient un VRAI saut de ligne. "
            "Écrire \\n (antislash + n) : sinon « SyntaxError » à la construction du site.")

    # --- latest.json : une seule version, des fichiers qui existent ---------------------------

    def test_latest_json_annonce_une_seule_version(self):
        v = self.latest["version"]
        self.assertEqual(self.latest["latestVersion"], v)
        self.assertEqual(self.latest["latest_version"], v)
        self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+", v), f"version illisible : {v!r}")
        self.assertTrue(self.latest["distPatchUrl"].endswith(f"/dist_v{v}.zip"), self.latest["distPatchUrl"])
        self.assertEqual(self.latest["dist_patch_url"], self.latest["distPatchUrl"])
        patch = self.latest["backendPatch"]
        self.assertEqual(patch["version"], v, "le patch backend porte la version du dist (procédure de release)")
        self.assertTrue(patch["url"].endswith(f"/backend_v{v}.zip"), patch["url"])
        self.assertTrue(self.latest["changelog"].startswith(f"v{v} :"),
                        "le texte des nouveautés doit commencer par la version annoncée")

    def test_les_fichiers_annonces_existent_et_sont_signes(self):
        v = self.latest["version"]
        for nom in (f"dist_v{v}.zip", f"backend_v{v}.zip"):
            zip_ = os.path.join(PUBLIC, nom)
            self.assertTrue(os.path.isfile(zip_), f"latest.json annonce {nom}, absent de public/")
            sig = zip_ + ".sig"
            self.assertTrue(os.path.isfile(sig), f"{nom} sans signature ({nom}.sig) : les apps la refusent")
            self.assertTrue(lire(sig).strip(), f"{nom}.sig est vide")

    # --- route.ts et latest.json disent la même chose -----------------------------------------

    def test_route_ts_est_lisible(self):
        for champ in ("version", "latestVersion", "latest_version", "releaseDate", "changelog",
                      "distPatchUrl", "dist_patch_url", "backendPatch"):
            self.assertIn(champ, self.route, f"route.ts : champ `{champ}` introuvable ou illisible")

    def test_route_ts_annonce_la_meme_release_que_latest_json(self):
        r, l = self.route, self.latest
        self.assertEqual(r["version"].lstrip("v"), l["version"])
        self.assertEqual(r["latest_version"].lstrip("v"), l["version"])
        self.assertEqual(r["latestVersion"], l["latestVersion"])
        self.assertEqual(r["releaseDate"], l["releaseDate"])
        self.assertEqual(r["distPatchUrl"], l["distPatchUrl"])
        self.assertEqual(r["dist_patch_url"], l["dist_patch_url"])
        self.assertEqual(r["backendPatch"], l["backendPatch"])

    def test_route_ts_porte_le_meme_texte_de_nouveautes_que_latest_json(self):
        self.assertIn("changelog", self.route, "route.ts : chaîne `changelog` illisible (voir le test de syntaxe)")
        self.assertEqual(self.route["changelog"], self.latest["changelog"])


if __name__ == "__main__":
    unittest.main()
