# -*- coding: utf-8 -*-
"""
Garde de câblage : la reprise du scellement fiscal est appelée au lancement du serveur.

Revue du 2026-10-14 : `reseal_pending_sales` existait, était testée, mais n'était appelée par AUCUN code de
production : les ventes dont le scellement avait échoué restaient en attente indéfiniment.
"""
import ast
import os
import unittest

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class TestCablageRepriseFiscale(unittest.TestCase):
    def test_run_server_appelle_reseal_pending_sales(self):
        with open(os.path.join(RACINE, "server_pos.py"), encoding="utf-8") as f:
            arbre = ast.parse(f.read())
        run_server = next(n for n in ast.walk(arbre) if isinstance(n, ast.FunctionDef) and n.name == "run_server")
        appels = {n.func.attr for n in ast.walk(run_server)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("reseal_pending_sales", appels)


if __name__ == "__main__":
    unittest.main()
