# -*- coding: utf-8 -*-
"""
Service de gestion du fond de caisse matinal - Kōdo POS Core.
Isole la session de caisse active des sessions déjà clôturées pour éviter
toute réécriture rétroactive d'un historique comptable.
"""

import sqlite3


def set_fond_caisse_matin(cursor: sqlite3.Cursor, montant: str) -> None:
    """Met à jour le fond de caisse matinal de la session active (non clôturée),
    ou ouvre une nouvelle session si aucune n'est active.

    Ne modifie jamais une session dont ``date_cloture`` est renseignée : une
    session clôturée fait partie de l'historique comptable et doit rester
    intacte.
    """
    cursor.execute(
        "SELECT id FROM Sessions_Caisse WHERE date_cloture IS NULL ORDER BY id DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if row:
        cursor.execute(
            "UPDATE Sessions_Caisse SET fond_caisse_matin=? WHERE id=?",
            (montant, row[0]),
        )
    else:
        cursor.execute(
            "INSERT INTO Sessions_Caisse (fond_caisse_matin) VALUES (?)",
            (montant,),
        )
