"""Index SQLite persistant de fingerprints winnowés."""

from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Iterable


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
                "DROP TABLE IF EXISTS deletion_candidate_hashes; "
                "DROP TABLE IF EXISTS active_file_hashes; "
                "DROP TABLE IF EXISTS fingerprint_origins; "
                "DROP TABLE IF EXISTS fingerprints; "
                "DROP TABLE IF EXISTS project_commits; "
                "DROP TABLE IF EXISTS commits; "
                "DROP TABLE IF EXISTS analysis_state; "
                "DROP TABLE IF EXISTS web_data;"
            )
        self._create_schema()

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
            CREATE TABLE IF NOT EXISTS commits (
                commit_hash TEXT PRIMARY KEY,
                commit_timestamp TEXT NOT NULL,
                processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS project_commits (
                commit_hash TEXT NOT NULL,
                project TEXT NOT NULL,
                size INTEGER NOT NULL,
                size_root TEXT,
                touched INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(commit_hash, project),
                FOREIGN KEY(commit_hash) REFERENCES commits(commit_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_project_commits_project
                ON project_commits(project, commit_hash);
            CREATE TABLE IF NOT EXISTS fingerprint_origins (
                hash TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                file TEXT NOT NULL,
                first_commit_hash TEXT NOT NULL,
                FOREIGN KEY(first_commit_hash) REFERENCES commits(commit_hash)
            );
            CREATE INDEX IF NOT EXISTS fingerprint_origins_commit
                ON fingerprint_origins(first_commit_hash);
            CREATE TABLE IF NOT EXISTS active_file_hashes (
                file TEXT NOT NULL,
                hash TEXT NOT NULL,
                project TEXT NOT NULL,
                occurrences INTEGER NOT NULL,
                PRIMARY KEY(file, hash)
            );
            CREATE TABLE IF NOT EXISTS deletion_candidate_hashes (
                candidate_id TEXT NOT NULL,
                hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_deletion_candidate_hash
                ON deletion_candidate_hashes(hash);
            CREATE INDEX IF NOT EXISTS idx_deletion_candidate_id
                ON deletion_candidate_hashes(candidate_id);
            CREATE TABLE IF NOT EXISTS analysis_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def clear(self) -> None:
        self.connection.executescript(
            "DELETE FROM deletion_candidate_hashes; DELETE FROM active_file_hashes; "
            "DELETE FROM fingerprint_origins; DELETE FROM fingerprints; "
            "DELETE FROM project_commits; DELETE FROM commits; DELETE FROM analysis_state;"
        )

    def load_analysis_state(self) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM analysis_state WHERE id = 1"
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_analysis(self, state: dict) -> None:
        """Enregistre l'état analytique autoritaire."""
        self.connection.execute("DROP TABLE IF EXISTS web_data")
        self.connection.execute(
            "INSERT INTO analysis_state(id, payload, updated_at) "
            "VALUES(1, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
            "updated_at=CURRENT_TIMESTAMP",
            (json.dumps(state, ensure_ascii=False, separators=(",", ":")),),
        )

    def _chunks(self, values: list[str], size: int | None = None) -> Iterable[list[str]]:
        if size is None:
            size = max(1, self.connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 10)
        for start in range(0, len(values), size):
            yield values[start : start + size]

    def original_sources(
        self, hashes: list[str]
    ) -> tuple[set[str], dict[str, tuple[str, str, str]]]:
        """Retourne en une paire de requêtes les hashes et leurs origines."""
        unique = list(dict.fromkeys(hashes))
        known: set[str] = set()
        # La décision de recouvrement ne charge aucune provenance inutile :
        # elle repose sur la seule recherche indexée demandée.
        for chunk in self._chunks(unique):
            placeholders = ",".join("?" for _ in chunk)
            known.update(
                row[0]
                for row in self.connection.execute(
                    f"SELECT DISTINCT hash FROM fingerprints WHERE hash IN ({placeholders})",
                    chunk,
                )
            )
        first: dict[str, tuple[str, str, str]] = {}
        for chunk in self._chunks(list(known)):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT hash, project, file, first_commit_hash FROM fingerprint_origins "
                f"WHERE hash IN ({placeholders})",
                chunk,
            )
            for fingerprint, project, file_path, commit_hash in rows:
                first.setdefault(fingerprint, (project, file_path, commit_hash))
        return known, first

    def fingerprint_files(self, hashes: list[str]) -> dict[str, set[str]]:
        """Retourne tous les fichiers ayant porté chaque fingerprint."""
        result: dict[str, set[str]] = {}
        unique = list(dict.fromkeys(hashes))
        for chunk in self._chunks(unique):
            placeholders = ",".join("?" for _ in chunk)
            for fingerprint, file_path in self.connection.execute(
                f"SELECT DISTINCT hash, file FROM fingerprints "
                f"WHERE hash IN ({placeholders})",
                chunk,
            ):
                result.setdefault(fingerprint, set()).add(file_path)
        return result

    def active_hashes(self, hashes: Iterable[str]) -> set[str]:
        """Retourne les fingerprints qui possèdent encore une occurrence active."""
        unique = list(dict.fromkeys(hashes))
        result: set[str] = set()
        for chunk in self._chunks(unique):
            placeholders = ",".join("?" for _ in chunk)
            result.update(
                row[0]
                for row in self.connection.execute(
                    f"SELECT DISTINCT hash FROM active_file_hashes "
                    f"WHERE hash IN ({placeholders}) AND occurrences > 0",
                    chunk,
                )
            )
        return result

    def add_deletion_candidate(self, candidate_id: str, hashes: Iterable[str]) -> None:
        """Conserve les fingerprints d'une suppression hors du blob d'état."""
        self.connection.executemany(
            "INSERT INTO deletion_candidate_hashes(candidate_id, hash) VALUES(?, ?)",
            ((candidate_id, value) for value in hashes),
        )

    def deletion_candidates_matching(self, hashes: Iterable[str]) -> set[str]:
        """Identifie les suppressions anciennes touchées par des fingerprints nouveaux."""
        unique = list(dict.fromkeys(hashes))
        result: set[str] = set()
        for chunk in self._chunks(unique):
            placeholders = ",".join("?" for _ in chunk)
            result.update(
                row[0]
                for row in self.connection.execute(
                    f"SELECT DISTINCT candidate_id FROM deletion_candidate_hashes "
                    f"WHERE hash IN ({placeholders})",
                    chunk,
                )
            )
        return result

    def deletion_hashes(self, candidate_ids: Iterable[str]) -> dict[str, list[str]]:
        """Recharge seulement les fingerprints des candidats à réévaluer."""
        unique = list(dict.fromkeys(candidate_ids))
        result: dict[str, list[str]] = {}
        for chunk in self._chunks(unique):
            placeholders = ",".join("?" for _ in chunk)
            for candidate_id, value in self.connection.execute(
                f"SELECT candidate_id, hash FROM deletion_candidate_hashes "
                f"WHERE candidate_id IN ({placeholders}) ORDER BY rowid",
                chunk,
            ):
                result.setdefault(candidate_id, []).append(value)
        return result

    def update_file_delta(
        self,
        old_path: str | None,
        new_path: str | None,
        project: str | None,
        commit_sha: str,
        added_hashes: Iterable[str],
        removed_hashes: Iterable[str],
    ) -> None:
        """Applique uniquement les fingerprints ajoutés/retirés à un fichier."""
        old_file = old_path
        new_file = new_path
        if old_file and old_file != new_file:
            active_rows = list(self.connection.execute(
                "SELECT hash, occurrences FROM active_file_hashes WHERE file = ?",
                (old_file,),
            ))
            self.connection.execute(
                "UPDATE fingerprints SET removed_at_commit = ? "
                "WHERE file = ? AND removed_at_commit IS NULL",
                (commit_sha, old_file),
            )
            self.connection.execute(
                "DELETE FROM active_file_hashes WHERE file = ?", (old_file,)
            )
            if new_file:
                self.connection.executemany(
                    "INSERT INTO active_file_hashes(file, hash, project, occurrences) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(file, hash) DO UPDATE SET "
                    "project = excluded.project, "
                    "occurrences = active_file_hashes.occurrences + excluded.occurrences",
                    ((new_file, value, project or "", count) for value, count in active_rows),
                )
                self.connection.executemany(
                    "INSERT INTO fingerprints"
                    "(hash, project, file, commit_hash, removed_at_commit) "
                    "VALUES(?, ?, ?, ?, NULL)",
                    ((value, project or "", new_file, commit_sha) for value, _ in active_rows),
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

        added = Counter(added_hashes)
        removed = Counter(removed_hashes)
        changed = set(added) | set(removed)
        active = {
            row[0]: int(row[1])
            for chunk in self._chunks(list(changed))
            for row in self.connection.execute(
                f"SELECT hash, occurrences FROM active_file_hashes WHERE file = ? "
                f"AND hash IN ({','.join('?' for _ in chunk)})",
                [new_file, *chunk],
            )
        }
        for value in changed:
            count = active.get(value, 0) + added[value] - removed[value]
            if count > 0:
                self.connection.execute(
                    "INSERT INTO active_file_hashes(file, hash, project, occurrences) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(file, hash) DO UPDATE SET "
                    "project = excluded.project, occurrences = excluded.occurrences",
                    (new_file, value, project or "", count),
                )
            else:
                self.connection.execute(
                    "DELETE FROM active_file_hashes WHERE file = ? AND hash = ?",
                    (new_file, value),
                )
                self.connection.execute(
                    "UPDATE fingerprints SET removed_at_commit = ? WHERE file = ? "
                    "AND hash = ? AND removed_at_commit IS NULL",
                    (commit_sha, new_file, value),
                )

        # Tous les fingerprints ajoutés reçoivent la provenance courante,
        # quelle que soit la classification du bloc.
        self.connection.executemany(
            "INSERT INTO fingerprints"
            "(hash, project, file, commit_hash, removed_at_commit) VALUES(?, ?, ?, ?, NULL)",
            ((value, project or "", new_file, commit_sha) for value in added),
        )
        self.connection.executemany(
            "INSERT OR IGNORE INTO fingerprint_origins"
            "(hash, project, file, first_commit_hash) VALUES(?, ?, ?, ?)",
            ((value, project or "", new_file, commit_sha) for value in added),
        )

    def record_processed_commit(self, commit_hash: str, commit_timestamp: str) -> None:
        """Enregistre explicitement chaque commit après son traitement."""
        self.connection.execute(
            "INSERT OR IGNORE INTO commits(commit_hash, commit_timestamp) VALUES(?, ?)",
            (commit_hash, commit_timestamp),
        )

    def record_project_commits(
        self,
        commit_hash: str,
        snapshots: Iterable[tuple[str, int, str | None, bool]],
    ) -> None:
        """Enregistre l'état de chaque projet à ce commit, touché ou non."""
        self.connection.executemany(
            "INSERT INTO project_commits(commit_hash, project, size, size_root, touched) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(commit_hash, project) DO UPDATE SET "
            "size=excluded.size, size_root=excluded.size_root, touched=excluded.touched",
            (
                (commit_hash, project, int(size), size_root, int(touched))
                for project, size, size_root, touched in snapshots
            ),
        )

    def update_project_commit_size(self, commit_hash: str, project: str, size: int) -> None:
        """Répercute une correction rétroactive dans la série relationnelle."""
        self.connection.execute(
            "UPDATE project_commits SET size = ? WHERE commit_hash = ? AND project = ?",
            (int(size), commit_hash, project),
        )

    def last_processed_commit(self) -> str | None:
        row = self.connection.execute(
            "SELECT commit_hash FROM commits ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return str(row[0]) if row else None

    def commit(self) -> None:
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
