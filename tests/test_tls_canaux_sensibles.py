# -*- coding: utf-8 -*-
"""
Kōdo POS — Les canaux sortants sensibles doivent vérifier le certificat ET le nom d'hôte.

Historique : `license.py` et `offline_engine.py` construisaient un contexte avec
`check_hostname = False` / `verify_mode = ssl.CERT_NONE`, pour contourner l'absence de
magasin de CA système sur les Python macOS. Conséquence sur la licence : la clé et
l'empreinte matérielle circulaient lisibles par un intermédiaire, qui pouvait en plus
forger une réponse `{"valid": true}` (copie activée) ou `{"status": "suspended"}`
(caisse d'une boutique éteinte à distance).

Le bon remède existait déjà dans la maison : `updater.build_ssl_context()`, qui s'appuie
sur le magasin `certifi` embarqué dans le build et ne dégrade JAMAIS vers une connexion
non vérifiée. Ces tests interdisent le retour en arrière.

Aucun appel réseau réel n'est effectué : `urlopen` est bouchonné et on inspecte le
contexte TLS que le code de production lui a passé.
"""

import ssl
import sys
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _ReponseBidon:
    """Réponse HTTP minimale, suffisante pour les deux appelants testés."""

    status = 200

    def read(self):
        return b'{"valid": false}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ContexteTLSCapture:
    """Remplace `urlopen` et mémorise le contexte TLS reçu, sans ouvrir de socket."""

    def __init__(self):
        self.contexte = None
        self.appele = False

    def __call__(self, req, *args, **kwargs):
        self.appele = True
        self.contexte = kwargs.get("context")
        return _ReponseBidon()


class TestTLSCanauxSensibles(unittest.TestCase):

    def _assert_contexte_strict(self, ctx, canal):
        self.assertIsInstance(
            ctx, ssl.SSLContext,
            msg=f"{canal} : aucun contexte TLS explicite n'a été transmis à urlopen.",
        )
        self.assertEqual(
            ctx.verify_mode, ssl.CERT_REQUIRED,
            msg=f"{canal} : le certificat du serveur n'est pas vérifié (CERT_NONE).",
        )
        self.assertTrue(
            ctx.check_hostname,
            msg=f"{canal} : le nom d'hôte n'est pas vérifié, un certificat valide "
                f"pour un autre domaine serait accepté.",
        )

    def test_contexte_maison_de_l_updater_est_strict(self):
        from kodo_core.services.updater import build_ssl_context
        self._assert_contexte_strict(build_ssl_context(), "updater.build_ssl_context")

    def test_validation_de_licence_en_ligne_verifie_le_certificat(self):
        from kodo_core.services import license as licence_mod

        capture = ContexteTLSCapture()
        original = urllib.request.urlopen
        urllib.request.urlopen = capture
        try:
            licence_mod.validate_license_online("KODO-TEST-0000", "empreinte-de-test")
        finally:
            urllib.request.urlopen = original

        self.assertTrue(capture.appele, "validate_license_online n'a émis aucune requête.")
        self._assert_contexte_strict(capture.contexte, "validate_license_online")

    def test_sonde_de_connectivite_verifie_le_certificat(self):
        from kodo_core.sync.offline_engine import OfflineSyncEngine

        capture = ContexteTLSCapture()
        original = urllib.request.urlopen
        urllib.request.urlopen = capture
        try:
            OfflineSyncEngine.check_internet_connection(timeout=1)
        finally:
            urllib.request.urlopen = original

        self.assertTrue(capture.appele, "check_internet_connection n'a émis aucune requête.")
        self._assert_contexte_strict(capture.contexte, "check_internet_connection")

    def test_aucune_desactivation_residuelle_dans_les_deux_modules(self):
        """Garde-fou textuel : ces deux fichiers ne doivent plus jamais neutraliser TLS."""
        racine = Path(__file__).resolve().parent.parent
        for relatif in ("kodo_core/services/license.py", "kodo_core/sync/offline_engine.py"):
            source = (racine / relatif).read_text(encoding="utf-8")
            for motif in ("CERT_NONE", "check_hostname = False", "check_hostname=False"):
                self.assertNotIn(
                    motif, source,
                    msg=f"{relatif} neutralise à nouveau la vérification TLS ({motif}).",
                )


if __name__ == "__main__":
    unittest.main()
