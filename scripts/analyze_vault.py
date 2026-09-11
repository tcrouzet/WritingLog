#!/usr/bin/env python3
"""Analyse l'historique Git d'un vault et enregistre son état dans SQLite."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    yaml = None
YAMLError = yaml.YAMLError if yaml is not None else RuntimeError

from fingerprint_index import FingerprintIndex, winnowed_hashes
from git_utils import (
    BlobReader,
    Change,
    GitError,
    changed_paths_batch,
    commit_exists,
    commit_timestamp,
    ensure_repository,
    list_commits,
    list_commits_after,
)


def compact_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h{minutes:02d}m"
    if minutes:
        return f"{minutes:d}m{seconds:02d}s"
    return f"{seconds:d}s"


class ProgressBar:
    """Barre mono-ligne en terminal, jalons sobres dans les logs redirigés."""

    def __init__(self, label: str, total: int, initial: int = 0) -> None:
        self.label = label
        self.total = total
        self.initial = initial
        self.started = time.monotonic()
        self.terminal = sys.stderr.isatty()
        self.last_log = 0
        if self.terminal:
            self.update(initial)
        elif total == 0:
            print(f"{label} : 0/0 commits", file=sys.stderr, flush=True)

    def update(self, current: int) -> None:
        elapsed = time.monotonic() - self.started
        ratio = current / self.total if self.total else 1.0
        completed_now = current - self.initial
        rate = completed_now / elapsed if elapsed > 0 else 0.0
        eta = (self.total - current) / rate if rate > 0 else 0.0
        if self.terminal:
            width = 28
            filled = round(width * ratio)
            bar = "█" * filled + "░" * (width - filled)
            line = (
                f"\r{self.label} [{bar}] {ratio:6.1%} "
                f"{current}/{self.total} · {rate:5.1f} c/s · "
                f"{compact_duration(elapsed)} · ETA {compact_duration(eta)}"
            )
            print(line, end="", file=sys.stderr, flush=True)
            if current >= self.total:
                print(file=sys.stderr, flush=True)
        elif current == 1 or current == self.total or current - self.last_log >= 250:
            print(
                f"{self.label} : {current}/{self.total} commits "
                f"({ratio:.0%}, {compact_duration(elapsed)})",
                file=sys.stderr,
                flush=True,
            )
            self.last_log = current


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError(
            "PyYAML manque. Lancez : python -m pip install -r scripts/requirements.txt"
        )
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} doit contenir un dictionnaire YAML.")
    return value


def json_compatible(value: Any) -> Any:
    """Convertit notamment les dates YAML implicites sans filtrer les métadonnées."""
    if isinstance(value, dict):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_compatible(item) for item in value]
    if hasattr(value, "isoformat") and value.__class__.__module__ == "datetime":
        return value.isoformat()
    return value


def path_key(path: str) -> str:
    """Évite d'exposer les noms de fichiers dans l'état destiné à être publié."""
    return hashlib.sha256(path.encode("utf-8", errors="surrogateescape")).hexdigest()


def content_hash(text: str) -> str:
    """Identité exacte du contenu intégral d'un fichier."""
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


DIFF_TOKEN_RE = re.compile(r"\s+|\w+(?:['’]\w+)*|[^\w\s]", re.UNICODE)


def diff_tokens(text: str) -> tuple[list[str], list[str]]:
    raw = DIFF_TOKEN_RE.findall(text)
    keys = [" " if token.isspace() else token.casefold() for token in raw]
    return raw, keys


def changed_middle(old: str, new: str) -> tuple[str, str]:
    """Retire rapidement les préfixes/suffixes strictement identiques."""
    limit = min(len(old), len(new))
    prefix = 0
    block = 4096
    while prefix + block <= limit and old[prefix : prefix + block] == new[prefix : prefix + block]:
        prefix += block
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1

    old_remaining = len(old) - prefix
    new_remaining = len(new) - prefix
    suffix_limit = min(old_remaining, new_remaining)
    suffix = 0
    while suffix + block <= suffix_limit:
        old_start = len(old) - suffix - block
        new_start = len(new) - suffix - block
        if old[old_start : old_start + block] != new[new_start : new_start + block]:
            break
        suffix += block
    while (
        suffix < suffix_limit
        and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]
    ):
        suffix += 1

    old_end = len(old) - suffix if suffix else len(old)
    new_end = len(new) - suffix if suffix else len(new)
    return old[prefix:old_end], new[prefix:new_end]


def character_changes(old: str, new: str) -> tuple[list[str], list[str]]:
    """Retourne les fragments réellement ajoutés et supprimés."""
    if old == new:
        return [], []
    old_middle, new_middle = changed_middle(old, new)
    if not old_middle:
        return ([new_middle] if new_middle else []), []
    if not new_middle:
        return [], [old_middle]
    if max(len(old_middle), len(new_middle)) > 20_000:
        old_lines = old_middle.splitlines(keepends=True)
        new_lines = new_middle.splitlines(keepends=True)
        if min(len(old_lines), len(new_lines)) >= 4:
            matcher = SequenceMatcher(
                None,
                [line.casefold() for line in old_lines],
                [line.casefold() for line in new_lines],
                autojunk=False,
            )
            added: list[str] = []
            removed: list[str] = []
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                if tag == "equal":
                    continue
                old_part = "".join(old_lines[old_start:old_end])
                new_part = "".join(new_lines[new_start:new_end])
                if tag == "replace" and max(len(old_part), len(new_part)) <= 20_000:
                    sub_added, sub_removed = character_changes(old_part, new_part)
                    added.extend(sub_added)
                    removed.extend(sub_removed)
                else:
                    if tag in {"insert", "replace"} and new_part:
                        added.append(new_part)
                    if tag in {"delete", "replace"} and old_part:
                        removed.append(old_part)
            return added, removed
    old_raw, old_keys = diff_tokens(old_middle)
    new_raw, new_keys = diff_tokens(new_middle)
    matcher = SequenceMatcher(None, old_keys, new_keys, autojunk=True)
    added: list[str] = []
    removed: list[str] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_part = "".join(old_raw[old_start:old_end])
        new_part = "".join(new_raw[new_start:new_end])
        if tag == "replace" and old_part and new_part and max(len(old_part), len(new_part)) <= 4_000:
            chars = SequenceMatcher(None, old_part, new_part, autojunk=False)
            for char_tag, a, b, c, d in chars.get_opcodes():
                if char_tag in {"insert", "replace"} and c != d:
                    added.append(new_part[c:d])
                if char_tag in {"delete", "replace"} and a != b:
                    removed.append(old_part[a:b])
        else:
            if tag in {"insert", "replace"} and new_part:
                added.append(new_part)
            if tag in {"delete", "replace"} and old_part:
                removed.append(old_part)
    return added, removed


def classification_blocks(parts: Iterable[str]) -> list[str]:
    """Découpe en petits groupes de phrases sans perdre de caractères.

    Un paragraphe entier est une unité trop grossière : la correction d'un seul
    mot peut modifier ses minima winnowés et recréditer des milliers de signes.
    Les groupes courts limitent ce risque tout en restant assez longs pour
    produire plusieurs fingerprints.
    """
    blocks: list[str] = []

    def append_sentences(text: str) -> None:
        pieces = re.split(r"(?<=[.!?…])([ \t\n]+)", text)
        current = ""
        for piece in pieces:
            if not piece:
                continue
            current += piece
            if len(current) >= 360 and re.search(r"[.!?…][ \t\n]*$", current):
                blocks.append(current)
                current = ""
        if current:
            if blocks and len(current) < 36:
                blocks[-1] += current
            else:
                blocks.append(current)

    for part in parts:
        pending_separator = ""
        for piece in re.split(r"(\n[ \t]*\n+)", part):
            if not piece:
                continue
            if re.fullmatch(r"\n[ \t]*\n+", piece):
                pending_separator += piece
            else:
                append_sentences(pending_separator + piece)
                pending_separator = ""
        if pending_separator:
            if blocks:
                blocks[-1] += pending_separator
            else:
                blocks.append(pending_separator)
    return blocks


def infer_edited_renames(
    commit_sha: str,
    changes: list[Change],
    blobs: BlobReader,
    extensions: set[str],
    gram_chars: int,
    selection_chars: int,
) -> list[Change]:
    """Apparie les A/D très proches que Git rate après une forte édition.

    Git raisonne surtout par lignes. Dans un manuscrit aux paragraphes longs,
    quelques corrections peuvent donc transformer un renommage évident en une
    suppression suivie d'une création. Les fingerprints donnent ici un signal
    mieux adapté au texte continu.
    """
    additions = [
        (index, change)
        for index, change in enumerate(changes)
        if change.status == "A"
        and change.new_path
        and PurePosixPath(change.new_path).suffix.lower() in extensions
    ]
    deletions = [
        (index, change)
        for index, change in enumerate(changes)
        if change.status == "D"
        and change.old_path
        and PurePosixPath(change.old_path).suffix.lower() in extensions
    ]
    if not additions or not deletions:
        return changes

    text_cache: dict[tuple[str, str], str] = {}

    def text(ref: str, path: str) -> str:
        key = (ref, path)
        if key not in text_cache:
            text_cache[key] = blobs.text(ref, path) or ""
        return text_cache[key]

    candidates: list[tuple[float, int, int]] = []
    for add_index, addition in additions:
        assert addition.new_path
        new_text = text(commit_sha, addition.new_path)
        new_hashes = set(winnowed_hashes(new_text, gram_chars, selection_chars))
        if not new_hashes:
            continue
        for delete_index, deletion in deletions:
            assert deletion.old_path
            if PurePosixPath(addition.new_path).parent != PurePosixPath(deletion.old_path).parent:
                continue
            old_text = text(f"{commit_sha}^", deletion.old_path)
            size_ratio = min(len(old_text), len(new_text)) / max(len(old_text), len(new_text), 1)
            if size_ratio < 0.5:
                continue
            old_hashes = set(winnowed_hashes(old_text, gram_chars, selection_chars))
            if not old_hashes:
                continue
            overlap = len(old_hashes & new_hashes) / min(len(old_hashes), len(new_hashes))
            if overlap >= 0.7:
                candidates.append((overlap, add_index, delete_index))

    replacements: dict[int, Change] = {}
    consumed_additions: set[int] = set()
    consumed_deletions: set[int] = set()
    for _, add_index, delete_index in sorted(candidates, reverse=True):
        if add_index in consumed_additions or delete_index in consumed_deletions:
            continue
        addition = changes[add_index]
        deletion = changes[delete_index]
        replacements[add_index] = Change(
            "R",
            deletion.old_path,
            addition.new_path,
            deletion.old_oid,
            addition.new_oid,
        )
        consumed_additions.add(add_index)
        consumed_deletions.add(delete_index)

    return [
        replacements.get(index, change)
        for index, change in enumerate(changes)
        if index not in consumed_deletions
    ]


def project_for(
    path: str | None,
    extensions: set[str],
    excluded: set[str],
    project_paths: dict[str, list[tuple[str, ...]]],
) -> str | None:
    if not path:
        return None
    parts = PurePosixPath(path).parts
    if len(parts) < 2:
        return None
    if PurePosixPath(path).suffix.lower() not in extensions:
        return None
    folded = tuple(part.casefold() for part in parts)
    # Une définition explicite prime sur les dossiers exclus.
    for project, locations in project_paths.items():
        if any(folded[: len(location)] == location for location in locations):
            return project
    configured_roots = {
        location[0]
        for locations in project_paths.values()
        for location in locations
        if location and location[0] != "archives"
    }
    if folded[0] in configured_roots:
        return None
    if parts[0].casefold() in excluded:
        return None
    return parts[0]


def tracked_folder_for(
    path: str | None,
    project: str | None,
    project_paths: dict[str, list[tuple[str, ...]]],
) -> str | None:
    """Retourne le chemin racine qui a effectivement attribué un fichier."""
    if not path or not project:
        return None
    parts = PurePosixPath(path).parts
    folded = tuple(part.casefold() for part in parts)
    for location in project_paths.get(project, []):
        if folded[: len(location)] == location:
            return "/".join(parts[: len(location)])
    return parts[0] if parts else None


def configured_project_paths(
    metadata: dict[str, Any], include_history: bool = True
) -> dict[str, list[tuple[str, ...]]]:
    paths: dict[str, list[tuple[str, ...]]] = {}
    for project, fields in metadata.items():
        project_id = str(project)
        raw_folder = fields.get("folder") if isinstance(fields, dict) else None
        if raw_folder:
            parts = PurePosixPath(str(raw_folder).strip("/")).parts
            # Compatibilité avec l'ancien format relatif : folder: manuscrit.
            if len(parts) == 1:
                parts = (project_id, *parts)
        else:
            parts = (project_id,)
        locations = [tuple(part.casefold() for part in parts)]
        if include_history and isinstance(fields, dict):
            for historical in fields.get("history_folders", []):
                historical_parts = PurePosixPath(str(historical).strip("/")).parts
                locations.append(tuple(part.casefold() for part in historical_parts))
        # Un projet archivé conserve aussi son emplacement historique avant le déplacement.
        if include_history and len(parts) >= 2 and parts[0].casefold() == "archives":
            locations.append(tuple(part.casefold() for part in (project_id, *parts[2:])))
        paths[project_id] = locations
    return paths


def config_fingerprint(config: dict[str, Any], metadata: dict[str, Any]) -> str:
    relevant = {
        "excluded_folders": config["excluded_folders"],
        "file_extensions": config["file_extensions"],
        "internal_detection": config["internal_detection"],
        "project_paths": configured_project_paths(metadata),
    }
    raw = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def normalized_config(raw: dict[str, Any]) -> dict[str, Any]:
    result = {
        "vault_path": raw.get("vault_path", "."),
        "history_repo": raw.get("history_repo"),
        "output_dir": raw.get("output_dir", "site"),
        "archived_projects_file": raw.get("archived_projects_file", "projets_archives.yml"),
        "fingerprint_db": raw.get("fingerprint_db", ".cache/fingerprints.sqlite3"),
        "excluded_folders": list(raw.get("excluded_folders", [])),
        "file_extensions": list(raw.get("file_extensions", [".md"])),
        "internal_detection": {
            "gram_chars": raw.get("internal_detection", {}).get("gram_chars", 36),
            "selection_chars": raw.get("internal_detection", {}).get("selection_chars", 180),
            "overlap_threshold": raw.get("internal_detection", {}).get("overlap_threshold", 0.85),
        },
    }
    gram = int(result["internal_detection"]["gram_chars"])
    selection = int(result["internal_detection"]["selection_chars"])
    overlap = float(result["internal_detection"]["overlap_threshold"])
    if not 30 <= gram <= 40:
        raise ValueError("internal_detection.gram_chars doit être compris entre 30 et 40.")
    if selection < gram:
        raise ValueError("internal_detection.selection_chars doit être supérieur à gram_chars.")
    if not 0 < overlap <= 1:
        raise ValueError("internal_detection.overlap_threshold doit être compris entre 0 et 1.")
    return result


def empty_state(fingerprint: str) -> dict[str, Any]:
    return {
        "config_fingerprint": fingerprint,
        "last_commit": None,
        "files": {},
        "project_sizes": {},
        "events": [],
        "size_points": [],
        "archive_moves": {},
        "deletion_candidates": [],
        "file_lifecycles": {},
    }


def load_state(
    database: Path,
    fingerprint: str,
    full_rebuild: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    if full_rebuild:
        return empty_state(fingerprint)
    if not database.exists():
        raise ValueError("Base d'analyse absente ; lancez d'abord : ./analyse.sh full")
    with FingerprintIndex(database) as index:
        state = index.load_analysis_state()
    if not state:
        raise ValueError("État d'analyse absent de SQLite ; lancez d'abord : ./analyse.sh full")
    # Vestige des anciens états versionnés : il n'intervient plus dans la
    # reprise incrémentale et disparaît à la prochaine écriture.
    state.pop("version", None)
    state.pop("excluded_sizes", None)
    # Les états produits avant l'unification conservaient deux compteurs. Le
    # compteur historique est le seul qui ait toujours utilisé tous les chemins
    # configurés : il devient donc directement project_sizes à la migration.
    historical_sizes = state.pop("historical_project_sizes", None)
    if isinstance(historical_sizes, dict):
        state["project_sizes"] = historical_sizes
    if state.get("config_fingerprint") != fingerprint:
        raise ValueError("La configuration d'analyse a changé ; relancez avec --full-rebuild.")
    return state


def minutes_between(newer: str, older: str | None, default: float) -> float:
    if not older:
        return default
    delta = (datetime.fromisoformat(newer) - datetime.fromisoformat(older)).total_seconds() / 60
    return max(0.0, delta)


def fallback_project(path: str | None, tracked_project: str | None) -> str:
    """Provenance de l'index, y compris pour un dossier non suivi."""
    if tracked_project:
        return tracked_project
    parts = PurePosixPath(path).parts if path else ()
    return parts[0] if parts else ""


def process_history_diff(
    repo: Path,
    state: dict[str, Any],
    config: dict[str, Any],
    metadata: dict[str, Any],
    fingerprint_db: Path,
    persist_index: bool = True,
) -> tuple[int, int]:
    """Analyse les seuls fichiers modifiés et maintient l'index SQLite."""
    full_run = state.get("last_commit") is None
    start = 0
    relevant_commits = 0
    if full_run:
        commits = list_commits(repo)
        pending = commits
        previous_timestamp = None
    else:
        with FingerprintIndex(fingerprint_db) as existing_index:
            previous = existing_index.last_processed_commit()
        if not previous:
            raise ValueError("Index des commits traités absent ; lancez ./analyse.sh full.")
        if state.get("last_commit") != previous:
            raise ValueError("L'index SQLite et son état d'analyse divergent ; relancez ./analyse.sh full.")
        if not commit_exists(repo, previous):
            raise ValueError("Le dernier commit traité n'est plus dans l'historique ; lancez ./analyse.sh full.")
        pending = list_commits_after(repo, previous)
        commits = pending
        previous_timestamp = commit_timestamp(repo, previous)
    commit_intervals: dict[str, str | None] = {}
    for commit in pending:
        commit_intervals[commit.sha] = previous_timestamp
        previous_timestamp = commit.timestamp
    extensions = {
        str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
        for ext in config["file_extensions"]
    }
    excluded = {str(folder).strip("/").casefold() for folder in config["excluded_folders"]}
    history_paths = configured_project_paths(metadata, include_history=True)
    gram_chars = int(config["internal_detection"]["gram_chars"])
    selection_chars = int(config["internal_detection"]["selection_chars"])
    overlap_threshold = float(config["internal_detection"]["overlap_threshold"])
    files = state["files"]
    project_sizes = state["project_sizes"]
    events = state["events"]
    size_points = state["size_points"]
    archive_moves = state["archive_moves"]
    deletion_candidates = state["deletion_candidates"]
    file_lifecycles = state.setdefault("file_lifecycles", {})

    def active_excluded_size(project: str) -> int:
        """Taille des seules compilations actuellement présentes."""
        return sum(
            int(active.get("size", 0))
            for lifecycle in file_lifecycles.values()
            if (active := lifecycle.get("active_creation"))
            and active.get("project") == project
            and active.get("excluded_from_size")
        )
    first_new_deletion_candidate = len(deletion_candidates)
    run_added_hashes: set[str] = set()

    def new_bucket() -> dict[str, Any]:
        return {
            "novel": 0,
            "removed": [],
            "internal": 0,
            "internal_hashes": set(),
            "sources": [],
            "folders": set(),
        }

    # Un full est une reconstruction sans état caché : la base est toujours
    # purgée avant de rejouer le premier commit.
    reset_index = full_run
    with FingerprintIndex(fingerprint_db, reset=reset_index) as fingerprints, BlobReader(repo) as blobs:
        total = len(commits) if full_run else len(pending)
        progress = ProgressBar("Analyse chronologique", total, start if full_run else 0)
        batch_start = -1
        batch_changes: dict[str, list[Any]] = {}
        for relative_index, commit in enumerate(pending, start=1):
            # Inséré dans la même transaction que ses fingerprints. Tant que
            # celle-ci n'est pas validée, le commit n'est pas considéré traité.
            fingerprints.record_processed_commit(commit.sha, commit.timestamp)
            wanted_batch_start = ((relative_index - 1) // 100) * 100
            if wanted_batch_start != batch_start:
                batch_start = wanted_batch_start
                batch = pending[batch_start : batch_start + 100]
                batch_changes = changed_paths_batch(
                    repo,
                    [item.sha for item in batch],
                    detect_renames=True,
                )
            absolute_index = start + relative_index
            display_index = absolute_index if full_run else relative_index
            work: dict[str, dict[str, Any]] = defaultdict(new_bucket)
            touched_sizes: set[str] = set()
            change_records: list[dict[str, Any]] = []
            commit_added_hashes: list[str] = []

            commit_changes = infer_edited_renames(
                commit.sha,
                batch_changes.get(commit.sha, []),
                blobs,
                extensions,
                gram_chars,
                selection_chars,
            )
            for change in commit_changes:
                old_path = change.old_path or (change.new_path if change.status != "A" else None)
                new_path = change.new_path
                old_md = bool(old_path and PurePosixPath(old_path).suffix.lower() in extensions)
                new_md = bool(new_path and PurePosixPath(new_path).suffix.lower() in extensions)
                if not old_md and not new_md:
                    continue
                old_text = blobs.text(f"{commit.sha}^", old_path) or "" if old_md and old_path else ""
                new_text = blobs.text(commit.sha, new_path) or "" if new_md and new_path else ""
                old_project = project_for(old_path, extensions, excluded, history_paths)
                new_project = project_for(new_path, extensions, excluded, history_paths)
                old_folder = tracked_folder_for(old_path, old_project, history_paths)
                new_folder = tracked_folder_for(new_path, new_project, history_paths)
                if old_project and old_folder:
                    work[old_project]["folders"].add(old_folder)
                if new_project and new_folder:
                    work[new_project]["folders"].add(new_folder)

                if old_project and change.status != "C":
                    project_sizes[old_project] = max(
                        0,
                        int(project_sizes.get(old_project, 0)) - len(old_text),
                    )
                    touched_sizes.add(old_project)
                    if old_path:
                        files.pop(path_key(old_path), None)
                if new_project:
                    project_sizes[new_project] = int(project_sizes.get(new_project, 0)) + len(new_text)
                    files[path_key(new_path)] = {
                        "size": len(new_text),
                        "project": new_project,
                        "last_timestamp": commit.timestamp,
                    }
                    touched_sizes.add(new_project)

                if change.status == "A":
                    added_parts, removed_parts = ([new_text] if new_text else []), []
                elif change.status == "D":
                    added_parts, removed_parts = [], []
                else:
                    added_parts, removed_parts = character_changes(old_text, new_text)
                added_parts = classification_blocks(added_parts)
                removed_parts = classification_blocks(removed_parts)
                whole_added_block = (
                    (new_text, winnowed_hashes(new_text, gram_chars, selection_chars))
                    if change.status == "A" and new_text
                    else None
                )
                lifecycle_key = path_key(new_path or old_path) if (new_path or old_path) else None
                lifecycle = file_lifecycles.setdefault(
                    lifecycle_key, {"additions": 0, "deletions": 0}
                ) if lifecycle_key else {"additions": 0, "deletions": 0}
                # Un export temporaire peut être renommé plusieurs fois avant
                # sa suppression. Son cycle de vie doit suivre le fichier ;
                # sinon la suppression finale ne retrouve jamais sa création et
                # sa taille reste définitivement injectée dans la courbe.
                if (
                    change.status == "R"
                    and old_path
                    and new_path
                    and old_path != new_path
                ):
                    old_lifecycle = file_lifecycles.setdefault(
                        path_key(old_path), {"additions": 0, "deletions": 0}
                    )
                    active_creation = old_lifecycle.pop("active_creation", None)
                    if active_creation:
                        active_creation.setdefault("size_history", []).append({
                            "timestamp": commit.timestamp,
                            "size": len(new_text),
                        })
                        active_creation["size"] = len(new_text)
                        lifecycle["active_creation"] = active_creation
                reappeared_file = bool(
                    change.status == "A"
                    and lifecycle["additions"] > 0
                    and lifecycle["deletions"] > 0
                )
                if change.status == "A":
                    lifecycle["additions"] += 1
                elif change.status == "D":
                    lifecycle["deletions"] += 1
                    active_creation = lifecycle.pop("active_creation", None)
                    deleted_content_hash = content_hash(old_text)
                    if not active_creation:
                        # Git peut présenter un déplacement comme une paire A/D
                        # sans renommage. Le hash intégral rattache alors la
                        # suppression au fichier rempli apparu sous un autre nom.
                        for candidate_lifecycle in file_lifecycles.values():
                            candidate = candidate_lifecycle.get("active_creation")
                            if (
                                candidate
                                and candidate.get("project") == old_project
                                and candidate.get("content_hash") == deleted_content_hash
                            ):
                                active_creation = candidate_lifecycle.pop("active_creation")
                                break
                    if active_creation:
                        lifetime = minutes_between(
                            commit.timestamp,
                            active_creation["timestamp"],
                            float("inf"),
                        )
                        exact_transient = (
                            active_creation.get("content_hash") == deleted_content_hash
                        )
                        if exact_transient or float(active_creation.get("overlap_ratio", 0.0)) >= 0.5:
                            original_event = events[int(active_creation["event_index"])]
                            reclassified = min(
                                int(active_creation["real_chars"]),
                                int(original_event["real_chars"]),
                            )
                            original_event["real_chars"] -= reclassified
                            if exact_transient:
                                removed_internal = min(
                                    int(active_creation.get("internal_chars", 0)),
                                    int(original_event["internal_chars"]),
                                )
                                original_event["internal_chars"] -= removed_internal
                                creation_file_key = active_creation.get("file_key")
                                original_event["duplication_sources"] = [
                                    source
                                    for source in original_event.get("duplication_sources", [])
                                    if not source.get("target_file")
                                    or path_key(source["target_file"]) != creation_file_key
                                ]
                            else:
                                removed_internal = 0
                                original_event["internal_chars"] += reclassified
                            original_event.setdefault("temporary_compilations", []).append({
                                "file": old_path,
                                "removed_commit": commit.sha,
                                "lifetime_minutes": round(lifetime, 3),
                                "overlap_ratio": active_creation["overlap_ratio"],
                                "reclassified_chars": reclassified,
                                "removed_internal_chars": removed_internal,
                                "exact_content_hash": exact_transient,
                            })
                            if not active_creation.get("excluded_from_size"):
                                size_history = active_creation.get("size_history", [])
                                for point in size_points:
                                    if (
                                        point["project"] != active_creation["project"]
                                        or point["timestamp"] < active_creation["timestamp"]
                                    ):
                                        continue
                                    effective_size = int(
                                        size_history[0]["size"]
                                        if size_history
                                        else active_creation["size"]
                                    )
                                    for historical_size in size_history:
                                        if historical_size["timestamp"] <= point["timestamp"]:
                                            effective_size = int(historical_size["size"])
                                    point["size"] = max(0, int(point["size"]) - effective_size)

                elif change.status == "M" and lifecycle.get("active_creation"):
                    active_creation = lifecycle["active_creation"]
                    active_creation.setdefault("size_history", []).append({
                        "timestamp": commit.timestamp,
                        "size": len(new_text),
                    })
                    active_creation["size"] = len(new_text)
                added_blocks = [
                    (part, winnowed_hashes(part, gram_chars, selection_chars))
                    for part in added_parts
                ]
                removed_blocks = [
                    (part, winnowed_hashes(part, gram_chars, selection_chars))
                    for part in removed_parts
                ]
                commit_added_hashes.extend(
                    value for _, block_hashes in added_blocks for value in block_hashes
                )
                if whole_added_block:
                    commit_added_hashes.extend(whole_added_block[1])
                change_records.append({
                    "status": change.status,
                    "old_path": old_path if old_md else None,
                    "new_path": new_path if new_md else None,
                    "old_project": old_project,
                    "new_project": new_project,
                    "index_project": fallback_project(new_path or old_path, new_project or old_project),
                    "added_blocks": added_blocks,
                    "whole_added_block": whole_added_block,
                    "reappeared_file": reappeared_file,
                    "removed_blocks": removed_blocks,
                    "novel_chars": 0,
                    "internal_chars": 0,
                    "exclude_from_size": False,
                })

                if old_project and change.status == "R" and new_path:
                    parts = PurePosixPath(new_path).parts
                    if len(parts) >= 2 and parts[0].casefold() == "archives":
                        archive_moves[old_project] = {
                            "archive_folder": "/".join(parts[:2]),
                            "moved_at": commit.timestamp,
                        }

            known_hashes, source_by_hash = fingerprints.original_sources(commit_added_hashes)
            run_added_hashes.update(commit_added_hashes)
            # L'index SQL représente les commits précédents. Cet ensemble est
            # enrichi bloc après bloc afin qu'une seconde occurrence créée dans
            # le même commit soit déjà reconnue comme duplication.
            commit_known_hashes = set(known_hashes)
            for record in change_records:
                new_project = record["new_project"]
                if new_project:
                    blocks_to_classify = record["added_blocks"]
                    whole_added = record["whole_added_block"]
                    if whole_added:
                        whole_text, whole_hashes = whole_added
                        whole_matched = sum(
                            1 for value in whole_hashes if value in commit_known_hashes
                        )
                        whole_ratio = (
                            whole_matched / len(whole_hashes) if whole_hashes else 0.0
                        )
                        # Un nouveau fichier globalement recouvert est une
                        # compilation. Le découpage par paragraphes ne doit pas
                        # recréditer ses marges ou ses passages légèrement édités.
                        # Un fichier déjà apparu puis supprimé est en outre un
                        # artefact de compilation récurrent dès que la majorité
                        # de son contenu possède une origine antérieure.
                        repeated_compilation = (
                            record["reappeared_file"] and whole_ratio >= 0.5
                        )
                        if whole_hashes and (
                            whole_ratio >= overlap_threshold or repeated_compilation
                        ):
                            blocks_to_classify = [whole_added]
                            record["force_whole_internal"] = repeated_compilation
                            record["exclude_from_size"] = True

                    for part, block_hashes in blocks_to_classify:
                        matched = sum(1 for value in block_hashes if value in commit_known_hashes)
                        ratio = matched / len(block_hashes) if block_hashes else 0.0
                        # À cette granularité courte, une seule empreinte exacte
                        # de 36 caractères suffit à conserver la filiation. Un
                        # seuil de 85 % recréditerait tout un groupe de phrases
                        # dès que quelques mots ont été corrigés.
                        if block_hashes and (
                            matched > 0 or record.get("force_whole_internal", False)
                        ):
                            work[new_project]["internal"] += len(part)
                            record["internal_chars"] += len(part)
                            work[new_project]["internal_hashes"].update(block_hashes)
                            origin_counts: dict[tuple[str, str, str], int] = defaultdict(int)
                            for value in block_hashes:
                                source = source_by_hash.get(value)
                                if source:
                                    origin_counts[source] += 1
                            origins = [
                                {
                                    "project": project,
                                    "file": file_path,
                                    "commit": source_commit,
                                    "matching_hashes": count,
                                }
                                for (project, file_path, source_commit), count in sorted(
                                    origin_counts.items(), key=lambda item: item[1], reverse=True
                                )
                            ]
                            work[new_project]["sources"].append({
                                "target_file": record["new_path"],
                                "characters": len(part),
                                "matching_hashes": matched,
                                "total_hashes": len(block_hashes),
                                "overlap_ratio": round(ratio, 6),
                                "origins": origins,
                            })
                        else:
                            work[new_project]["novel"] += len(part)
                            record["novel_chars"] += len(part)
                        for value in block_hashes:
                            source_by_hash.setdefault(
                                value,
                                (new_project, record["new_path"], commit.sha),
                            )
                        commit_known_hashes.update(block_hashes)
                    if record["removed_blocks"] and record["status"] not in {"R", "C"}:
                        target = record["old_project"] or new_project
                        work[target]["removed"].extend(
                            (record["old_path"], part, block_hashes)
                            for part, block_hashes in record["removed_blocks"]
                        )

                fingerprints.update_file_delta(
                    record["old_path"],
                    record["new_path"],
                    record["index_project"],
                    commit.sha,
                    (
                        value
                        for _, block_hashes in record["added_blocks"]
                        for value in block_hashes
                    ),
                    (
                        value
                        for _, block_hashes in record["removed_blocks"]
                        for value in block_hashes
                    ),
                )

            event_indexes: dict[str, int] = {}
            for project, values in work.items():
                novel_chars = int(values["novel"])
                interval_start = commit_intervals.get(commit.sha)
                # Les fingerprints décident seuls : déjà connu signifie copie ou
                # déplacement ; inconnu signifie production nouvelle.
                real_chars = novel_chars
                event_index = len(events)
                event_indexes[project] = event_index
                events.append({
                    "timestamp": commit.timestamp,
                    "interval_start": interval_start,
                    "commit": commit.sha,
                    "project": project,
                    "real_chars": real_chars,
                    "import_chars": 0,
                    "internal_chars": values["internal"],
                    "edit_delta": 0,
                    "folders": sorted(values["folders"]),
                    "duplication_sources": values["sources"],
                })
                for candidate_number, (source_file, part, removed_hashes) in enumerate(values["removed"]):
                    if not removed_hashes:
                        events[event_index]["edit_delta"] -= len(part)
                        continue
                    candidate_id = f"{commit.sha}:{event_index}:{candidate_number}"
                    deletion_candidates.append({
                        "id": candidate_id,
                        "event_index": event_index,
                        "project": project,
                        "file": source_file,
                        "commit": commit.sha,
                        "characters": len(part),
                        "counted": False,
                        "resolved": False,
                    })
                    fingerprints.add_deletion_candidate(candidate_id, removed_hashes)

            for record in change_records:
                if record["status"] != "A" or not record["new_path"] or not record["new_project"]:
                    continue
                lifecycle = file_lifecycles[path_key(record["new_path"])]
                whole_added = record["whole_added_block"]
                whole_hashes = whole_added[1] if whole_added else []
                matched = sum(1 for value in whole_hashes if value in known_hashes)
                overlap_ratio = matched / len(whole_hashes) if whole_hashes else 0.0
                lifecycle["active_creation"] = {
                    "timestamp": commit.timestamp,
                    "event_index": event_indexes[record["new_project"]],
                    "real_chars": int(record["novel_chars"]),
                    "internal_chars": int(record["internal_chars"]),
                    "overlap_ratio": round(overlap_ratio, 6),
                    "project": record["new_project"],
                    "file_key": path_key(record["new_path"]),
                    "content_hash": content_hash(whole_added[0] if whole_added else ""),
                    "size": len(whole_added[0]) if whole_added else 0,
                    "size_history": [{
                        "timestamp": commit.timestamp,
                        "size": len(whole_added[0]) if whole_added else 0,
                    }],
                    "excluded_from_size": bool(record["exclude_from_size"]),
                }

            if work:
                relevant_commits += 1
            for project in sorted(touched_sizes):
                size_points.append({
                    "timestamp": commit.timestamp,
                    "commit": commit.sha,
                    "project": project,
                    "size": max(
                        0,
                        int(project_sizes.get(project, 0))
                        - active_excluded_size(project),
                    ),
                })
            state["last_commit"] = commit.sha
            progress.update(display_index)
        # Les candidats créés pendant cette exécution doivent tous être jugés.
        # Pour une mise à jour incrémentale, un ancien candidat ne peut changer
        # de verdict que si l'un de ses fingerprints vient de réapparaître : on
        # évite ainsi de rescanner toutes les suppressions historiques.
        matching_old_candidates = fingerprints.deletion_candidates_matching(run_added_hashes)
        unresolved = [
            candidate
            for index, candidate in enumerate(deletion_candidates)
            if not candidate["resolved"]
            and (
                full_run
                or index >= first_new_deletion_candidate
                or candidate["id"] in matching_old_candidates
            )
        ]
        candidate_hashes = fingerprints.deletion_hashes(
            candidate["id"] for candidate in unresolved
        )
        all_removed_hashes = [value for hashes in candidate_hashes.values() for value in hashes]
        files_by_hash = fingerprints.fingerprint_files(all_removed_hashes)
        still_active_hashes = fingerprints.active_hashes(all_removed_hashes)
        for candidate in unresolved:
            hashes = candidate_hashes.get(candidate["id"], [])
            matching = sum(
                1
                for value in hashes
                if value in still_active_hashes
                or any(path != candidate["file"] for path in files_by_hash.get(value, set()))
            )
            ratio = matching / len(hashes) if hashes else 0.0
            event = events[int(candidate["event_index"])]
            # Même symétrie pour les suppressions : une empreinte retrouvée
            # ailleurs suffit à établir que le passage n'a pas disparu.
            if matching > 0:
                if candidate["counted"]:
                    event["edit_delta"] += int(candidate["characters"])
                candidate["resolved"] = True
                candidate["counted"] = False
                candidate["overlap_ratio"] = round(ratio, 6)
            elif not candidate["counted"]:
                event["edit_delta"] -= int(candidate["characters"])
                candidate["counted"] = True

        if state.get("last_commit") and persist_index:
            fingerprints.commit()
        elif not persist_index:
            fingerprints.rollback()
    return len(pending), relevant_commits


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyse l'activité d'écriture d'un vault Git")
    parser.add_argument("mode", nargs="?", choices=("full", "incremental"), default="incremental", help="full rejoue tout ; incremental traite uniquement les nouveaux commits")
    parser.add_argument("--config", default=None, help="Chemin de config.yaml")
    parser.add_argument("--full-rebuild", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Analyser sans modifier la base SQLite")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve() if args.config else project_root / "config.yaml"
    base_dir = config_path.parent
    try:
        config = normalized_config(load_yaml(config_path))
        metadata = json_compatible(load_yaml(base_dir / "projet.yml"))
        vault = (base_dir / config["vault_path"]).resolve()
        history_repo = (
            (base_dir / config["history_repo"]).resolve()
            if config.get("history_repo")
            else vault
        )
        fingerprint_db = (base_dir / config["fingerprint_db"]).resolve()
        if not history_repo.exists() and config.get("history_repo"):
            raise ValueError(
                f"Le miroir historique {history_repo} n'existe pas. "
                "Lancez d'abord : python scripts/cache_history.py"
            )
        ensure_repository(history_repo)
        fingerprint = config_fingerprint(config, metadata)
        full_rebuild = args.mode == "full" or args.full_rebuild
        temporary_database = None
        processing_database = fingerprint_db
        if args.dry_run and full_rebuild:
            temporary_database = tempfile.TemporaryDirectory(prefix="writinglog-dry-run-")
            processing_database = Path(temporary_database.name) / "fingerprints.sqlite3"
        state = load_state(
            fingerprint_db,
            fingerprint,
            full_rebuild,
            config,
        )
        processed, relevant = process_history_diff(
            history_repo,
            state,
            config,
            metadata,
            processing_database,
            persist_index=not args.dry_run,
        )
        # Le générateur web dispose ainsi des titres et réglages associés à
        # l'état analysé sans relire ni interpréter le vault.
        state["project_metadata"] = metadata
        if not args.dry_run:
            with FingerprintIndex(fingerprint_db) as database:
                database.save_analysis(state)
                database.commit()
        if temporary_database:
            temporary_database.cleanup()
    except (GitError, OSError, RuntimeError, ValueError, json.JSONDecodeError, YAMLError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    suffix = " (simulation, aucun fichier écrit)" if args.dry_run else ""
    project_count = len({event["project"] for event in state["events"]})
    print(f"Analyse {args.mode} terminée{suffix} : {processed} nouveau(x) commit(s), {relevant} pertinent(s), {project_count} projet(s).")
    if not args.dry_run:
        print(f"Analyse enregistrée dans {fingerprint_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
