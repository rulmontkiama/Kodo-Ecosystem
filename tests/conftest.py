# -*- coding: utf-8 -*-
"""Isole toute la suite de la vraie machine : HOME est redirigé vers un dossier jetable.

Sans cela, les tests écrivaient dans les vrais dossiers de l'utilisateur : base et snapshots
(~/Documents/Kodo_POS), sauvegardes (~/Documents/Kodo_Backups) et une fausse licence de test
(~/Library/Application Support/Kodo_POS/license.lic).
Ce fichier est chargé par pytest avant l'import des modules de test, donc avant le calcul des chemins.
"""
import atexit
import os
import shutil
import site
import tempfile

# Les sous-processus lancés par les tests gardent ainsi accès aux paquets installés « pour l'utilisateur ».
os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())

_HOME_JETABLE = tempfile.mkdtemp(prefix="kodo_tests_home_")
os.environ["HOME"] = _HOME_JETABLE
atexit.register(shutil.rmtree, _HOME_JETABLE, ignore_errors=True)
