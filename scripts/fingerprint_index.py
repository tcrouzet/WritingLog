"""Index SQLite persistant de fingerprints winnowés."""

from __future__ import annotations

from collections import Counter, deque
import hashlib
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Iterable


SCHEMA_VERSION = 1
SPACE_RE = re.compile(r"\s+", re.UNICODE)


def normalize_text(text: str) -> str:
    """Normalisation stable avant fingerprinting, sans conserver le texte."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return SPACE_RE.sub(" ", normalized).strip()


def winnowed_hashes(text: str, gram_chars: int, selection_chars: int) -> list[str]:
    """Retourne les minima de fenêtres glissantes selon l'algorithme winnowing."""
    normalized = normalize_text(text)
    if len(normalized) < gram_chars:
        return []
    width = gram_chars
    hashes = [
        hashlib.blake2b(
            normalized[index : index + width].encode("utf-8"),
            digest_size=8,
        ).hexdigest()
        for index in range(len(normalized) - width + 1)
    ]
    selection_width = max(1, selection_chars - width + 1)
    selection_width = min(selection_width, len(hashes))
    minima: deque[int] = deque()
    selected: list[str] = []
    last_position = -1
    for position, value in enumerate(hashes):
        while minima and minima[0] <= position - selection_width:
            minima.popleft()
        # Le minimum le plus à droite rend le résultat déterministe en cas
        # d'égalité et correspond au winnowing classique.
        while minima and hashes[minima[-1]] >= value:
            minima.pop()
        minima.append(position)
        if position + 1 < selection_width:
            continue
        chosen = minima[0]
        if chosen != last_position:
            selected.append(hashes[chosen])
            last_position = chosen
    return selected


class FingerprintIndex:
    """Index global interrogé par hash, jamais rechargé intégralement en mémoire."""

    def __init__(self, path: Path, *, reset: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        if reset:
            self.connection.executescript(
                "DROP TABLE IF EXISTS active_file_hashes; "
                "DROP TABLE IF EXISTS fingerprints; "
                "DROP TABLE IF EXISTS metadata;"
            )
        self._create_schema()
        version = self.meta("schema_version")
        if version not in {None, str(SCHEMA_VERSION)}:
            raise ValueError("Index SQLite incompatible ; relancez ./analyse.sh full.")
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS fingerprints (
                hash TEXT NOT NULL,
                project TEXT NOT NULL,
                file TEXT NOT NULL,
                commit_hash TEXT NOT NULL,
                removed_at_commit TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_hash
                ON fingerprints(hash);
            CREATE INDEX IF NOT EXISTS fingerprints_active_file
                ON fingerprints(file, removed_at_commit);
            CREATE INDEX IF NOT EXISTS fingerprints_file_commit
                ON fingerprints(file, commit_hash);
            CREATE TABLE IF NOT EXISTS active_file_hashes (
                file TEXT NOT NULL,
                hash TEXT NOT NULL,
                project TEXT NOT NULL,
                PRIMARY KEY(file, hash)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def clear(self) -> None:
        self.connection.executescript(
            "DELETE FROM active_file_hashes; DELETE FROM fingerprints; DELETE FROM metadata;"
        )

    def meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @staticmethod
    def _chunks(values: list[str], size: int = 500) -> Iterable[list[str]]:
        for start in range(0, len(values), size):
            yield values[start : start + size]

    def original_sources(self, hashes: list[str]) -> tuple[set[str], list[dict]]:
        """Retourne les hashes connus et leur toute première provenance."""
        unique = list(dict.fromkeys(hashes))
        known: set[str] = set()
        # La décision de recouvrement ne charge aucune provenance inutile :
        # elle repose sur la seule recherche indexée demandée.
        for chunk in self._chunks(unique):
            placeholders = ",".join("?" for _ in chunk)
            known.update(
                row[0]
                for row in self.connection.execute(
                    f"SELECT hash FROM fingerprints WHERE hash IN ({placeholders})",
                    chunk,
                )
            )
        first: dict[str, tuple[str, str, str]] = {}
        for chunk in self._chunks(list(known)):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT hash, project, file, commit_hash FROM fingerprints "
                f"WHERE rowid IN (SELECT MIN(rowid) FROM fingerprints "
                f"WHERE hash IN ({placeholders}) GROUP BY hash)",
                chunk,
            )
            for fingerprint, project, file_path, commit_hash in rows:
                first.setdefault(fingerprint, (project, file_path, commit_hash))
        counts: Counter[tuple[str, str, str]] = Counter()
        for fingerprint in hashes:
            source = first.get(fingerprint)
            if source:
                counts[source] += 1
        sources = [
            {
                "project": project,
                "file": file_path,
                "commit": commit_hash,
                "matching_hashes": count,
            }
            for (project, file_path, commit_hash), count in counts.most_common()
        ]
        return known, sources

    def update_file(
        self,
        old_path: str | None,
        new_path: str | None,
        project: str | None,
        commit_sha: str,
        current_hashes: Iterable[str],
        introduced_hashes: Iterable[str],
    ) -> None:
        """Met à jour une seule filiation de fichier, sans effacer son passé."""
        old_file = old_path
        new_file = new_path
        if old_file and old_file != new_file:
            self.connection.execute(
                "UPDATE fingerprints SET removed_at_commit = ? "
                "WHERE file = ? AND removed_at_commit IS NULL",
                (commit_sha, old_file),
            )
            self.connection.execute(
                "DELETE FROM active_file_hashes WHERE file = ?", (old_file,)
            )

        if not new_file:
            if old_file:
                self.connection.execute(
                    "UPDATE fingerprints SET removed_at_commit = ? "
                    "WHERE file = ? AND removed_at_commit IS NULL",
                    (commit_sha, old_file),
                )
                self.connection.execute(
                    "DELETE FROM active_file_hashes WHERE file = ?", (old_file,)
                )
            return

        current = set(current_hashes)
        active = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT hash FROM active_file_hashes WHERE file = ?", (new_file,)
            )
        }
        disappeared = active - current
        for chunk in self._chunks(list(disappeared)):
            placeholders = ",".join("?" for _ in chunk)
            self.connection.execute(
                f"UPDATE fingerprints SET removed_at_commit = ? WHERE file = ? "
                f"AND removed_at_commit IS NULL AND hash IN ({placeholders})",
                [commit_sha, new_file, *chunk],
            )
            self.connection.execute(
                f"DELETE FROM active_file_hashes WHERE file = ? "
                f"AND hash IN ({placeholders})",
                [new_file, *chunk],
            )

        # Les fingerprints introduits par le bloc obtiennent toujours une
        # nouvelle provenance, quelle que soit sa classification.
        to_record = set(introduced_hashes) | (current - active)
        existing = {
            row[0]
            for row in self.connection.execute(
                "SELECT hash FROM fingerprints WHERE file = ? AND commit_hash = ?",
                (new_file, commit_sha),
            )
        }
        self.connection.executemany(
            "INSERT INTO fingerprints"
            "(hash, project, file, commit_hash, removed_at_commit) VALUES(?, ?, ?, ?, NULL)",
            ((value, project or "", new_file, commit_sha) for value in to_record - existing),
        )
        self.connection.executemany(
            "INSERT INTO active_file_hashes(file, hash, project) VALUES(?, ?, ?) "
            "ON CONFLICT(file, hash) DO UPDATE SET project = excluded.project",
            ((new_file, value, project or "") for value in current),
        )

    def commit(self, last_commit: str) -> None:
        self.set_meta("last_commit", last_commit)
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "FingerprintIndex":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type:
            self.rollback()
        self.close()
