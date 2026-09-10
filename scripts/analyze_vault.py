#!/usr/bin/env python3
"""Analyse l'historique Git d'un vault et produit les JSON du dashboard."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import pickle
import re
import sys
import time
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    print("PyYAML manque. Lancez : python -m pip install -r scripts/requirements.txt", file=sys.stderr)
    raise SystemExit(2)

from fingerprint_index import FingerprintIndex, winnowed_hashes
from git_utils import (
    BlobReader,
    GitError,
    changed_paths_batch,
    commit_exists,
    commit_timestamp,
    ensure_repository,
    list_commits,
    list_commits_after,
)


STATE_VERSION = 20


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


def history_identity(commits: list[Any], fingerprint: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    for commit in commits:
        digest.update(commit.sha.encode("ascii"))
        digest.update(b"\0")
    return {
        "state_version": STATE_VERSION,
        "history": digest.hexdigest(),
        "config": fingerprint,
        "commits": len(commits),
    }


def load_checkpoint(path: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            checkpoint = pickle.load(handle)
    except (OSError, EOFError, pickle.PickleError):
        print("Checkpoint illisible ignoré ; reconstruction depuis le début.", file=sys.stderr)
        return None
    if checkpoint.get("identity") != identity:
        print("Checkpoint incompatible avec le miroir ou la configuration ; ignoré.", file=sys.stderr)
        return None
    return checkpoint


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        handle.flush()
    temporary.replace(path)


def load_yaml(path: Path) -> dict[str, Any]:
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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    temporary.replace(path)


def write_yaml(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(value, handle, allow_unicode=True, sort_keys=False)
    temporary.replace(path)


def path_key(path: str) -> str:
    """Évite d'exposer les noms de fichiers dans l'état destiné à être publié."""
    return hashlib.sha256(path.encode("utf-8", errors="surrogateescape")).hexdigest()


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
        "import_detection": config["import_detection"],
        "internal_detection": config["internal_detection"],
        "session": config["session"],
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
        "import_detection": {
            "rate_percentile": raw.get("import_detection", {}).get("rate_percentile", 99),
        },
        "session": {
            "timeout_minutes": raw.get("session", {}).get("timeout_minutes", 45),
        },
    }
    gram = int(result["internal_detection"]["gram_chars"])
    selection = int(result["internal_detection"]["selection_chars"])
    overlap = float(result["internal_detection"]["overlap_threshold"])
    percentile = float(result["import_detection"]["rate_percentile"])
    if not 30 <= gram <= 40:
        raise ValueError("internal_detection.gram_chars doit être compris entre 30 et 40.")
    if selection < gram:
        raise ValueError("internal_detection.selection_chars doit être supérieur à gram_chars.")
    if not 0 < overlap <= 1:
        raise ValueError("internal_detection.overlap_threshold doit être compris entre 0 et 1.")
    if not 0 < percentile <= 100:
        raise ValueError("import_detection.rate_percentile doit être compris entre 0 et 100.")
    if result["session"]["timeout_minutes"] <= 0:
        raise ValueError("session.timeout_minutes doit être supérieur à zéro.")
    return result


def empty_state(fingerprint: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "config_fingerprint": fingerprint,
        "last_commit": None,
        "files": {},
        "project_sizes": {},
        "events": [],
        "size_points": [],
        "archive_moves": {},
        "project_rates": {},
    }


def load_state(
    path: Path,
    fingerprint: str,
    full_rebuild: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    if full_rebuild:
        return empty_state(fingerprint)
    if not path.exists():
        raise ValueError("État incrémental absent ; lancez d'abord : python scripts/analyze_vault.py full")
    with path.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("version") != STATE_VERSION:
        raise ValueError("Version de state.json incompatible ; relancez ./analyse.sh full.")
    if state.get("config_fingerprint") != fingerprint:
        raise ValueError("La configuration d'analyse a changé ; relancez avec --full-rebuild.")
    return state


def minutes_between(newer: str, older: str | None, default: float) -> float:
    if not older:
        return default
    delta = (datetime.fromisoformat(newer) - datetime.fromisoformat(older)).total_seconds() / 60
    return max(0.0, delta)


def percentile_threshold(values: list[float], percentile: float) -> float | None:
    """Percentile empirique par rang supérieur, sans seuil de rythme fixe."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[rank]


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
    checkpoint_path: Path | None = None,
    analysis_fingerprint: str = "",
    persist_index: bool = True,
) -> tuple[int, int]:
    """Analyse les seuls fichiers modifiés et maintient l'index SQLite."""
    full_run = state.get("last_commit") is None
    start = 0
    relevant_commits = 0
    if full_run:
        commits = list_commits(repo)
        identity = history_identity(commits, analysis_fingerprint)
        checkpoint = load_checkpoint(checkpoint_path, identity) if checkpoint_path else None
        if checkpoint and checkpoint.get("phase") == "diff-analysis":
            state.clear()
            state.update(checkpoint["state"])
            start = int(checkpoint["next_index"])
            relevant_commits = int(checkpoint["relevant_commits"])
            print(f"Reprise de l'analyse au commit {start}/{len(commits)}.", file=sys.stderr)
        pending = commits[start:]
        previous_timestamp = commits[start - 1].timestamp if start else None
    else:
        checkpoint = None
        previous = str(state["last_commit"])
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
    current_paths = configured_project_paths(metadata, include_history=False)
    history_paths = configured_project_paths(metadata, include_history=True)
    gram_chars = int(config["internal_detection"]["gram_chars"])
    selection_chars = int(config["internal_detection"]["selection_chars"])
    overlap_threshold = float(config["internal_detection"]["overlap_threshold"])
    rate_percentile = float(config["import_detection"]["rate_percentile"])
    timeout_minutes = float(config["session"]["timeout_minutes"])
    files = state["files"]
    project_sizes = state["project_sizes"]
    events = state["events"]
    size_points = state["size_points"]
    archive_moves = state["archive_moves"]
    project_rates = state["project_rates"]

    def new_bucket() -> dict[str, Any]:
        return {
            "novel": 0,
            "removed": [],
            "internal": 0,
            "internal_hashes": set(),
            "sources": [],
            "folders": set(),
        }

    reset_index = full_run and checkpoint is None
    with FingerprintIndex(fingerprint_db, reset=reset_index) as fingerprints, BlobReader(repo) as blobs:
        expected_index_commit = state.get("last_commit")
        if checkpoint and fingerprints.meta("last_commit") != expected_index_commit:
            raise ValueError(
                "Le checkpoint et l'index SQLite divergent ; supprimez le checkpoint puis relancez ./analyse.sh full."
            )
        if not full_run and fingerprints.meta("last_commit") != expected_index_commit:
            raise ValueError("L'index SQLite ne correspond pas à state.json ; relancez ./analyse.sh full.")
        total = len(commits) if full_run else len(pending)
        progress = ProgressBar("Analyse chronologique", total, start if full_run else 0)
        batch_start = -1
        batch_changes: dict[str, list[Any]] = {}
        for relative_index, commit in enumerate(pending, start=1):
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

            for change in batch_changes.get(commit.sha, []):
                old_path = change.old_path or (change.new_path if change.status != "A" else None)
                new_path = change.new_path
                old_md = bool(old_path and PurePosixPath(old_path).suffix.lower() in extensions)
                new_md = bool(new_path and PurePosixPath(new_path).suffix.lower() in extensions)
                if not old_md and not new_md:
                    continue
                old_text = blobs.text(f"{commit.sha}^", old_path) or "" if old_md and old_path else ""
                new_text = blobs.text(commit.sha, new_path) or "" if new_md and new_path else ""
                old_current = project_for(old_path, extensions, excluded, current_paths)
                new_current = project_for(new_path, extensions, excluded, current_paths)
                old_project = project_for(old_path, extensions, excluded, history_paths)
                new_project = project_for(new_path, extensions, excluded, history_paths)
                old_folder = tracked_folder_for(old_path, old_project, history_paths)
                new_folder = tracked_folder_for(new_path, new_project, history_paths)
                if old_project and old_folder:
                    work[old_project]["folders"].add(old_folder)
                if new_project and new_folder:
                    work[new_project]["folders"].add(new_folder)

                if old_current and change.status != "C":
                    project_sizes[old_current] = max(0, int(project_sizes.get(old_current, 0)) - len(old_text))
                    touched_sizes.add(old_current)
                    if old_path:
                        files.pop(path_key(old_path), None)
                if new_current:
                    project_sizes[new_current] = int(project_sizes.get(new_current, 0)) + len(new_text)
                    files[path_key(new_path)] = {
                        "size": len(new_text),
                        "project": new_current,
                        "last_timestamp": commit.timestamp,
                    }
                    touched_sizes.add(new_current)

                if change.status == "A":
                    added_parts, removed_parts = ([new_text] if new_text else []), []
                elif change.status == "D":
                    added_parts, removed_parts = [], []
                else:
                    added_parts, removed_parts = character_changes(old_text, new_text)
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
                change_records.append({
                    "status": change.status,
                    "old_path": old_path if old_md else None,
                    "new_path": new_path if new_md else None,
                    "old_project": old_project,
                    "new_project": new_project,
                    "index_project": fallback_project(new_path or old_path, new_project or old_project),
                    "added_blocks": added_blocks,
                    "removed_blocks": removed_blocks,
                })

                if old_project and change.status == "R" and new_path:
                    parts = PurePosixPath(new_path).parts
                    if len(parts) >= 2 and parts[0].casefold() == "archives":
                        archive_moves[old_project] = {
                            "archive_folder": "/".join(parts[:2]),
                            "moved_at": commit.timestamp,
                        }

            known_hashes, source_by_hash = fingerprints.original_sources(commit_added_hashes)
            for record in change_records:
                new_project = record["new_project"]
                if new_project:
                    for part, block_hashes in record["added_blocks"]:
                        matched = sum(1 for value in block_hashes if value in known_hashes)
                        ratio = matched / len(block_hashes) if block_hashes else 0.0
                        if block_hashes and ratio >= overlap_threshold:
                            work[new_project]["internal"] += len(part)
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
                    if record["removed_blocks"] and record["status"] not in {"R", "C"}:
                        target = record["old_project"] or new_project
                        work[target]["removed"].extend(record["removed_blocks"])

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

            commit_internal_hashes = set().union(
                *(values["internal_hashes"] for values in work.values())
            ) if work else set()
            for project, values in work.items():
                removed_chars = 0
                for part, removed_hashes in values["removed"]:
                    overlap = (
                        sum(1 for value in removed_hashes if value in commit_internal_hashes)
                        / len(removed_hashes)
                        if removed_hashes else 0.0
                    )
                    if overlap < overlap_threshold:
                        removed_chars += len(part)

                novel_chars = int(values["novel"])
                interval_start = commit_intervals.get(commit.sha)
                gap = minutes_between(commit.timestamp, interval_start, 0.0) if interval_start else 0.0
                rates = [float(value) for value in project_rates.get(project, [])]
                threshold = percentile_threshold(rates, rate_percentile)
                current_rate = novel_chars / gap if novel_chars > 0 and gap > 0 else None
                is_import = current_rate is not None and threshold is not None and current_rate > threshold
                real_chars = 0 if is_import else novel_chars
                import_chars = novel_chars if is_import else 0
                events.append({
                    "timestamp": commit.timestamp,
                    "interval_start": interval_start,
                    "commit": commit.sha,
                    "project": project,
                    "real_chars": real_chars,
                    "import_chars": import_chars,
                    "internal_chars": values["internal"],
                    "edit_delta": -removed_chars,
                    "folders": sorted(values["folders"]),
                    "rate_chars_per_minute": current_rate,
                    "rate_percentile_threshold": threshold,
                    "duplication_sources": values["sources"],
                })
                if (
                    real_chars > 0
                    and current_rate is not None
                    and 0 < gap <= timeout_minutes
                ):
                    project_rates.setdefault(project, []).append(current_rate)

            if work:
                relevant_commits += 1
            for project in sorted(touched_sizes):
                size_points.append({
                    "timestamp": commit.timestamp,
                    "commit": commit.sha,
                    "project": project,
                    "size": int(project_sizes.get(project, 0)),
                })
            state["last_commit"] = commit.sha
            progress.update(display_index)
            if full_run and checkpoint_path and (
                absolute_index % 100 == 0 or absolute_index == len(commits)
            ):
                fingerprints.commit(commit.sha)
                write_checkpoint(checkpoint_path, {
                    "identity": identity,
                    "phase": "diff-analysis",
                    "next_index": absolute_index,
                    "state": state,
                    "relevant_commits": relevant_commits,
                })
        if state.get("last_commit") and persist_index:
            fingerprints.commit(state["last_commit"])
        elif not persist_index:
            fingerprints.rollback()
    return len(pending), relevant_commits


def period_keys(timestamp: str) -> tuple[str, str, str]:
    moment = datetime.fromisoformat(timestamp)
    day = moment.date().isoformat()
    iso = moment.isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    month = f"{moment.year:04d}-{moment.month:02d}"
    return day, week, month


def production_days(interval_start: str | None, timestamp: str) -> list[str]:
    """Jours auxquels rattacher un travail regroupé dans un commit espacé."""
    end_day = datetime.fromisoformat(timestamp).date()
    if not interval_start:
        return [end_day.isoformat()]
    start_day = datetime.fromisoformat(interval_start).date()
    if start_day >= end_day:
        return [end_day.isoformat()]
    cursor = start_day + timedelta(days=1)
    days: list[str] = []
    while cursor <= end_day:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def split_integer_over_days(value: int, days: list[str]) -> Iterable[tuple[str, int]]:
    """Répartit exactement un nombre de signes, reste affecté aux jours récents."""
    quotient, remainder = divmod(value, len(days))
    first_extra = len(days) - remainder
    for index, day in enumerate(days):
        yield day, quotient + (1 if index >= first_extra else 0)


def split_float_over_days(value: float, days: list[str]) -> Iterable[tuple[str, float]]:
    share = value / len(days)
    for day in days:
        yield day, share


def split_minutes_by_day(start: datetime, end: datetime) -> Iterable[tuple[str, float]]:
    cursor = start
    while cursor < end:
        boundary = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        segment_end = min(end, boundary)
        yield cursor.date().isoformat(), (segment_end - cursor).total_seconds() / 60
        cursor = segment_end


def aggregate(state: dict[str, Any], config: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    events = sorted(state["events"], key=lambda item: (item["timestamp"], item["commit"], item["project"]))
    active_projects = {project for project, size in state["project_sizes"].items() if int(size) > 0}
    known_projects = {event["project"] for event in events}
    selected_projects = {str(project).casefold() for project in metadata}
    active_projects.update(project for project in known_projects if project.casefold() in selected_projects)
    daily: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "signes_reels": 0,
            "signes_supprimes": 0,
            "temps_minutes": 0.0,
            "temps_inconnu": False,
            "temps_estime": False,
            "dossiers": set(),
        }
    )
    events_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    timeout_minutes = float(config["session"]["timeout_minutes"])
    project_minutes: dict[str, float] = defaultdict(float)
    for event in events:
        if event["project"] not in active_projects:
            continue
        if int(event.get("real_chars", 0)) <= 0 and int(event.get("edit_delta", 0)) >= 0:
            continue
        events_by_project[event["project"]].append(event)
        real_chars = int(event["real_chars"])
        deleted_chars = max(0, -int(event.get("edit_delta", 0)))
        days = production_days(event.get("interval_start"), event["timestamp"])
        for day, chars in split_integer_over_days(real_chars, days):
            daily[(day, event["project"])]["signes_reels"] += chars
            daily[(day, event["project"])]["dossiers"].update(event.get("folders", []))
        for day, chars in split_integer_over_days(deleted_chars, days):
            daily[(day, event["project"])]["signes_supprimes"] += chars

    for project, project_events in events_by_project.items():
        known_chars = 0
        known_minutes = 0.0
        previous_project_timestamp: str | None = None
        for event in project_events:
            chars = int(event.get("real_chars", 0)) + max(0, -int(event.get("edit_delta", 0)))
            days = production_days(event.get("interval_start"), event["timestamp"])
            interval_start = event.get("interval_start")
            commit_gap = minutes_between(event["timestamp"], interval_start, timeout_minutes) if interval_start else 0
            continuous_project_commit = (
                interval_start is not None
                and previous_project_timestamp == interval_start
                and 0 < commit_gap <= timeout_minutes
            )
            if continuous_project_commit:
                start = datetime.fromisoformat(interval_start)
                end = datetime.fromisoformat(event["timestamp"])
                for day, minutes in split_minutes_by_day(start, end):
                    daily[(day, project)]["temps_minutes"] += minutes
                    project_minutes[project] += minutes
                if chars > 0:
                    known_chars += chars
                    known_minutes += commit_gap
                previous_project_timestamp = event["timestamp"]
                continue

            if chars <= 0 or known_chars <= 0 or known_minutes <= 0:
                for day in days:
                    daily[(day, project)]["temps_inconnu"] = True
                previous_project_timestamp = event["timestamp"]
                continue
            project_rate = known_chars * 60 / known_minutes
            estimated_minutes = chars * 60 / project_rate
            # Le travail visible est nécessairement contenu dans l'intervalle
            # Git. Si la moyenne historique exige davantage de temps, elle ne
            # permet pas d'estimer ce commit : on expose alors une inconnue.
            if not interval_start or estimated_minutes > commit_gap:
                for day in days:
                    daily[(day, project)]["temps_inconnu"] = True
                previous_project_timestamp = event["timestamp"]
                continue
            for day, minutes in split_float_over_days(estimated_minutes, days):
                daily[(day, project)]["temps_minutes"] += minutes
                daily[(day, project)]["temps_estime"] = True
                project_minutes[project] += minutes
            previous_project_timestamp = event["timestamp"]

    daily_rows = [
        {
            "periode": period,
            "projet": project,
            "signes_reels": int(values["signes_reels"]),
            "signes_supprimes": int(values["signes_supprimes"]),
            "temps_minutes": None if values["temps_inconnu"] else round(values["temps_minutes"], 2),
            "temps_estime": values["temps_estime"] and not values["temps_inconnu"],
            "dossiers": sorted(values["dossiers"]),
        }
        for (period, project), values in sorted(daily.items())
    ]

    def rollup(kind: str) -> list[dict[str, Any]]:
        rolled: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"signes_reels": 0, "signes_supprimes": 0, "temps_minutes": 0.0, "temps_inconnu": False, "temps_estime": False, "dossiers": set()}
        )
        for row in daily_rows:
            day = datetime.fromisoformat(row["periode"])
            if kind == "week":
                iso = day.isocalendar()
                period = f"{iso.year}-W{iso.week:02d}"
            else:
                period = f"{day.year:04d}-{day.month:02d}"
            values = rolled[(period, row["projet"])]
            values["signes_reels"] += row["signes_reels"]
            values["signes_supprimes"] += row["signes_supprimes"]
            if row["temps_minutes"] is None:
                values["temps_inconnu"] = True
            else:
                values["temps_minutes"] += row["temps_minutes"]
            values["temps_estime"] = values["temps_estime"] or row.get("temps_estime", False)
            values["dossiers"].update(row.get("dossiers", []))
        return [
            {"periode": p, "projet": project, "signes_reels": int(v["signes_reels"]), "signes_supprimes": int(v["signes_supprimes"]), "temps_minutes": None if v["temps_inconnu"] else round(v["temps_minutes"], 2), "temps_estime": v["temps_estime"] and not v["temps_inconnu"], "dossiers": sorted(v["dossiers"])}
            for (p, project), v in sorted(rolled.items())
        ]

    projects: list[dict[str, Any]] = []
    all_projects = sorted(active_projects)
    metadata_folded = {str(key).casefold(): value for key, value in metadata.items()}
    for project in all_projects:
        project_events = events_by_project.get(project, [])
        custom = metadata.get(project, metadata_folded.get(project.casefold(), {}))
        custom = custom if isinstance(custom, dict) else {}
        real_total = sum(int(event["real_chars"]) for event in project_events)
        deleted_total = sum(max(0, -int(event.get("edit_delta", 0))) for event in project_events)
        time_unknown = any(
            values["temps_inconnu"]
            for (day, candidate), values in daily.items()
            if candidate == project
        )
        projects.append({
            **custom,
            "id": project,
            "title": custom.get("title", project),
            "temps_minutes_total": None if time_unknown else round(project_minutes.get(project, 0.0), 2),
            "signes_reels_total": real_total,
            "signes_supprimes_total": deleted_total,
            "taille_actuelle": int(state["project_sizes"].get(project, 0)),
            "date_creation": project_events[0]["timestamp"] if project_events else None,
            "derniere_activite": project_events[-1]["timestamp"] if project_events else None,
        })

    # Cette série mesure la production cumulée, pas la taille physique du dossier :
    # imports, copies et déplacements n'y entrent donc jamais.
    cumulative: dict[str, int] = defaultdict(int)
    sizes: list[dict[str, Any]] = []
    for row in daily_rows:
        project = row["projet"]
        cumulative[project] += int(row["signes_reels"])
        sizes.append({
            "date": row["periode"],
            "projet": project,
            # Nom conservé pour compatibilité avec le dashboard existant.
            "taille_signes": cumulative[project],
        })

    total_real = sum(project["signes_reels_total"] for project in projects)
    total_deleted = sum(project["signes_supprimes_total"] for project in projects)
    total_minutes = (
        None
        if any(project["temps_minutes_total"] is None for project in projects)
        else sum(project["temps_minutes_total"] for project in projects)
    )
    cutoff = datetime.now().astimezone().date() - timedelta(days=30)
    recent_scores: dict[str, float] = defaultdict(float)
    for row in daily_rows:
        if datetime.fromisoformat(row["periode"]).date() >= cutoff:
            recent_scores[row["projet"]] += row["signes_reels"]
    most_active = max(recent_scores, key=recent_scores.get) if recent_scores else None
    title_by_id = {project["id"]: project["title"] for project in projects}
    overview = {
        "temps_minutes_total": None if total_minutes is None else round(total_minutes, 2),
        "signes_reels_total": total_real,
        "signes_supprimes_total": total_deleted,
        "nombre_projets": len(projects),
        "projet_plus_actif_30_jours": most_active,
        "projet_plus_actif_30_jours_titre": title_by_id.get(most_active) if most_active else None,
        "derniere_mise_a_jour": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dernier_commit": state.get("last_commit"),
    }
    duplications = [
        {
            "timestamp": event["timestamp"],
            "commit": event["commit"],
            "projet": event["project"],
            "signes": int(event.get("internal_chars", 0)),
            "blocs": event.get("duplication_sources", []),
        }
        for event in events
        if event.get("duplication_sources")
    ]
    return {
        "overview": overview,
        "projects": projects,
        "daily": daily_rows,
        "weekly": rollup("week"),
        "monthly": rollup("month"),
        "size_evolution": sizes,
        "duplications": duplications,
    }


def archived_project_candidates(
    vault: Path,
    state: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Liste les anciens projets correspondant à un dossier actuel d'Archives."""
    selected = {str(project).casefold() for project in metadata}
    history = {event["project"] for event in state["events"]}
    inactive = {project for project in history if int(state["project_sizes"].get(project, 0)) == 0}

    archive_root = next(
        (item for item in vault.iterdir() if item.is_dir() and item.name.casefold() == "archives"),
        None,
    )
    archive_folders: dict[str, str] = {}
    if archive_root:
        for item in archive_root.iterdir():
            if item.is_dir():
                normalized = re.sub(r"^\d+\s*[-_–—]\s*", "", item.name).casefold()
                archive_folders[normalized] = f"{archive_root.name}/{item.name}"

    candidates: dict[str, dict[str, Any]] = {}
    moves = state.get("archive_moves", {})
    for project in sorted(inactive, key=str.casefold):
        if project.casefold() in selected:
            continue
        destination = moves.get(project, {}).get("archive_folder")
        if not destination:
            normalized = re.sub(r"^\d+\s*[-_–—]\s*", "", project).casefold()
            destination = archive_folders.get(normalized)
        if destination:
            destination_path = vault / destination
            manuscript = next(
                (item.name for item in destination_path.iterdir() if item.is_dir() and item.name.casefold() == "manuscrit"),
                None,
            ) if destination_path.is_dir() else None
            folder = f"{destination}/{manuscript}" if manuscript else destination
            candidates[project] = {"title": project, "folder": folder}
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyse l'activité d'écriture d'un vault Git")
    parser.add_argument("mode", nargs="?", choices=("full", "incremental"), default="incremental", help="full rejoue tout ; incremental traite uniquement les nouveaux commits")
    parser.add_argument("--config", default=None, help="Chemin de config.yaml")
    parser.add_argument("--full-rebuild", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Analyser sans écrire les fichiers JSON")
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
        output = (base_dir / config["output_dir"]).resolve()
        archived_projects_path = (base_dir / config["archived_projects_file"]).resolve()
        fingerprint_db = (base_dir / config["fingerprint_db"]).resolve()
        checkpoint_path = base_dir / ".cache" / "full-analysis.checkpoint"
        data_dir = output / "data"
        if not history_repo.exists() and config.get("history_repo"):
            raise ValueError(
                f"Le miroir historique {history_repo} n'existe pas. "
                "Lancez d'abord : python scripts/cache_history.py"
            )
        ensure_repository(history_repo)
        fingerprint = config_fingerprint(config, metadata)
        full_rebuild = args.mode == "full" or args.full_rebuild
        state = load_state(data_dir / "state.json", fingerprint, full_rebuild, config)
        processed, relevant = process_history_diff(
            history_repo,
            state,
            config,
            metadata,
            fingerprint_db,
            checkpoint_path=checkpoint_path if full_rebuild else None,
            analysis_fingerprint=fingerprint,
            persist_index=full_rebuild or not args.dry_run,
        )
        exports = aggregate(state, config, metadata)
        if not args.dry_run:
            for name, payload in exports.items():
                write_json(data_dir / f"{name}.json", payload)
            write_json(data_dir / "state.json", state)
            write_yaml(archived_projects_path, archived_project_candidates(vault, state, metadata))
            if full_rebuild:
                checkpoint_path.unlink(missing_ok=True)
    except (GitError, OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    suffix = " (simulation, aucun fichier écrit)" if args.dry_run else ""
    print(f"Analyse {args.mode} terminée{suffix} : {processed} nouveau(x) commit(s), {relevant} pertinent(s), {len(exports['projects'])} projet(s).")
    if not args.dry_run:
        print(f"Données écrites dans {data_dir}")
        print(f"Projets archivés proposés dans {archived_projects_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
