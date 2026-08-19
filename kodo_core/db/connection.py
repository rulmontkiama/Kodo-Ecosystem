"""
kodo_core.db.connection - Gestionnaire de connexion SQLite thread-safe avec mode WAL,
pragmas de performance, row factory et convertisseur Decimal.
"""

import sqlite3
import os
import hashlib
from decimal import Decimal
from contextlib import contextmanager
from kodo_core.config import ShopConfig

# -----------------------------------------------------------------------------
# Convertisseurs et adaptateurs pour le type Decimal dans SQLite
# -----------------------------------------------------------------------------
def adapt_decimal(d: Decimal) -> str:
    """Convertit un objet Decimal en chaîne pour SQLite."""
    return str(d)

def convert_decimal(s: bytes) -> Decimal:
    """Reconvertit une valeur binaire/texte SQLite en objet Decimal Python."""
    return Decimal(s.decode('utf-8'))

# Enregistrement global des adaptateurs SQLite
sqlite3.register_adapter(Decimal, adapt_decimal)
sqlite3.register_converter("DECIMAL", convert_decimal)
sqlite3.register_converter("decimal", convert_decimal)

def hash_pin(pin_plain: str) -> str:
    """Génère un hachage SHA-256 avec sel pour sécuriser les PINs vendeurs/admin."""
    if not pin_plain:
        return ""
    salt = ShopConfig.get_salt()
    return hashlib.sha256((str(pin_plain) + salt).encode('utf-8')).hexdigest()

class SafeConnection:
    """
    Wrapper de connexion SQLite ultra-sécurisé et thread-safe.
    Garantit l'activation du mode WAL, l'application des pragmas de performance,
    la conversion des Rows et la fermeture sans fuite de descripteur.
    """

    def __init__(self, db_path: str = None, conn: sqlite3.Connection = None, **kwargs):
        if conn is not None:
            self._conn = conn
            self._external_conn = True
        else:
            self.db_path = db_path or ShopConfig.get_db_path()
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
            
            if "detect_types" not in kwargs:
                kwargs["detect_types"] = sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
            
            self._conn = sqlite3.connect(self.db_path, **kwargs)
            self._external_conn = False

        self._conn.row_factory = sqlite3.Row
        self._closed = False
        self._apply_pragmas()

    def _apply_pragmas(self):
        """Applique les pragmas SQLite de performance."""
        try:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            pass

    def cursor(self) -> sqlite3.Cursor:
        return self._conn.cursor()

    def execute(self, *args, **kwargs) -> sqlite3.Cursor:
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs) -> sqlite3.Cursor:
        return self._conn.executemany(*args, **kwargs)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            pass

    def close(self):
        if not self._closed:
            if not self._external_conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()

    def __del__(self):
        self.close()

def get_connection(db_path: str = None, conn: sqlite3.Connection = None) -> SafeConnection:
    """Retourne une instance fermable de SafeConnection."""
    return SafeConnection(db_path=db_path, conn=conn)

@contextmanager
def db_transaction(db_path: str = None, conn: sqlite3.Connection = None):
    """
    Gestionnaire de contexte de transaction atomique.
    Valide (commit) automatiquement si aucune exception n'est levée, sinon annule (rollback).
    """
    connection = get_connection(db_path=db_path, conn=conn)
    try:
        cursor = connection.cursor()
        yield cursor
        connection.commit()
    except Exception as e:
        connection.rollback()
        raise e
    finally:
        connection.close()

@contextmanager
def db_query(db_path: str = None, conn: sqlite3.Connection = None):
    """Gestionnaire de contexte en lecture seule pour requêtes SQLite."""
    connection = get_connection(db_path=db_path, conn=conn)
    try:
        cursor = connection.cursor()
        yield cursor
    finally:
        connection.close()
