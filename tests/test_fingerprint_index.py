from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))

from fingerprint_index import FingerprintIndex, winnowed_hashes


class FingerprintIndexTest(unittest.TestCase):
    def test_winnowing_is_normalized_and_sparse(self) -> None:
        source = ("Écrire  un texte avec des espaces et des accents. " * 30).strip()
        equivalent = source.replace("É", "E\N{COMBINING ACUTE ACCENT}").upper().replace("  ", "\n")
        left = winnowed_hashes(source, 36, 180)
        right = winnowed_hashes(equivalent, 36, 180)
        self.assertEqual(left, right)
        self.assertGreater(len(left), 0)
        self.assertLess(len(left), len(source) // 10)

    def test_sqlite_schema_lookup_and_historical_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "fingerprints.sqlite3"
            text = " ".join(f"fragment-{index}" for index in range(200))
            hashes = winnowed_hashes(text, 36, 180)
            with FingerprintIndex(database_path, reset=True) as index:
                index.update_file_delta(
                    None,
                    "Alpha/manuscrit/source.md",
                    "Alpha",
                    "commit-1",
                    hashes,
                    [],
                )
                index.commit("commit-1")
                index.update_file_delta(
                    "Alpha/manuscrit/source.md",
                    None,
                    "Alpha",
                    "commit-2",
                    [],
                    [],
                )
                index.commit("commit-2")
                statements: list[str] = []
                index.connection.set_trace_callback(statements.append)
                known, sources = index.original_sources(hashes)
                index.connection.set_trace_callback(None)
                self.assertEqual(known, set(hashes))
                self.assertEqual(
                    len([statement for statement in statements if statement.startswith("SELECT")]),
                    2,
                )
                source = sources[next(iter(known))]
                self.assertEqual(source[1], "Alpha/manuscrit/source.md")
                self.assertEqual(source[2], "commit-1")

            database = sqlite3.connect(database_path)
            try:
                columns = {
                    row[1]: row[2]
                    for row in database.execute("PRAGMA table_info(fingerprints)")
                }
                indexes = {
                    row[1]
                    for row in database.execute("PRAGMA index_list(fingerprints)")
                }
                removed = database.execute(
                    "SELECT COUNT(*) FROM fingerprints WHERE removed_at_commit = 'commit-2'"
                ).fetchone()[0]
                query_plan = " ".join(
                    str(column)
                    for row in database.execute(
                        "EXPLAIN QUERY PLAN SELECT DISTINCT hash FROM fingerprints WHERE hash IN (?, ?)",
                        hashes[:2],
                    )
                    for column in row
                )
            finally:
                database.close()
            self.assertEqual(columns, {
                "hash": "TEXT",
                "project": "TEXT",
                "file": "TEXT",
                "commit_hash": "TEXT",
                "removed_at_commit": "TEXT",
            })
            self.assertIn("idx_hash", indexes)
            self.assertIn("idx_hash", query_plan)
            self.assertGreater(removed, 0)


if __name__ == "__main__":
    unittest.main()
