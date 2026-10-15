# -*- coding: utf-8 -*-
"""
Test de concurrence multi-thread sur la chaîne cryptographique SHA-256 (NF525).
Prouve l'absence absolue de fork de séquence et de collision de hash sous forte concurrence.
"""

import concurrent.futures
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from kodo_core.domain.accounting.ledger import GENESIS_HASH, build_hash_payload
from kodo_core.services.fiscal_service import (
    compute_sha256,
    ensure_schema,
    seal_sale,
)


class TestFiscalLedgerConcurrency(unittest.TestCase):

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name

        # Initialisation du schéma avec WAL mode et busy_timeout
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        ensure_schema(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        try:
            os.remove(self.db_path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.db_path + ext):
                    os.remove(self.db_path + ext)
        except Exception:
            pass

    def test_concurrent_sealing_produces_contiguous_chain_with_zero_forks(self):
        """Sous N threads concurrents sous BEGIN IMMEDIATE, la chaîne est strictement contiguë sans fork."""
        num_threads = 12

        def _worker(thread_idx: int):
            # Chaque thread ouvre sa propre connexion à SQLite avec un timeout généreux
            conn = sqlite3.connect(self.db_path, timeout=15.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                sale_id = 1000 + thread_idx
                totals = {
                    "total_ttc": Decimal("10.00") + Decimal(str(thread_idx)),
                    "total_ht": Decimal("8.26") + Decimal(str(thread_idx)),
                    "total_tva": Decimal("1.74"),
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                }
                seal_sale(conn, sale_id, totals)
                return True
            finally:
                conn.close()

        # Exécution concurrente
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(_worker, i) for i in range(num_threads)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(results), num_threads)
        self.assertTrue(all(results))

        # Audit de la chaîne complète résultante
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, sequence_number, sale_id, timestamp_utc, total_ttc, total_ht, total_tva, previous_hash, current_hash
            FROM fiscal_ledger
            ORDER BY id ASC
            """
        )
        rows = cursor.fetchall()
        conn.close()

        self.assertEqual(len(rows), num_threads, f"Doit contenir exactement {num_threads} scellements")

        prev_hash = GENESIS_HASH
        for idx, row in enumerate(rows, start=1):
            _id, seq_num, sale_id, ts, ttc, ht, tva, row_prev_hash, current_hash = row
            # 1. Vérification de la continuité du previous_hash (zéro fork)
            self.assertEqual(
                row_prev_hash,
                prev_hash,
                f"Rupture de chaînage au ticket #{idx} : previous_hash={row_prev_hash} attendu={prev_hash}",
            )

            # 2. Vérification du calcul SHA-256 déterministe
            payload = build_hash_payload(seq_num, ts, ttc, tva, prev_hash)
            expected_current_hash = compute_sha256(payload)
            self.assertEqual(
                current_hash,
                expected_current_hash,
                f"Hash altéré ou incohérent au ticket #{idx}",
            )

            prev_hash = current_hash


if __name__ == "__main__":
    unittest.main()
