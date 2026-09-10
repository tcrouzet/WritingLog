#!/usr/bin/env python3
"""Analyse l'historique Git d'un vault et produit les JSON du dashboard."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
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

from git_utils import BlobReader, GitError, changed_paths_batch, ensure_repository, list_commits, list_paths


STATE_VERSION = 18


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


def common_character_count(left: str, right: str) -> int:
    if not left or not right:
        return 0
    if left in right:
        return len(left)
    if right in left:
        return len(right)
    left_raw, left_keys = diff_tokens(left)
    _, right_keys = diff_tokens(right)
    matcher = SequenceMatcher(None, left_keys, right_keys, autojunk=True)
    return sum(
        sum(len(token) for token in left_raw[block.a : block.a + block.size])
        for block in matcher.get_matching_blocks()
        if block.size
    )


def movement_blocks(text: str, minimum: int) -> list[str]:
    """Découpe transitoirement un fragment en paragraphes assez significatifs."""
    blocks = [block.strip() for block in re.split(r"(?:\r?\n\s*){2,}", text)]
    return [block for block in blocks if len(block) >= minimum]


def block_anchors(text: str) -> set[tuple[str, ...]]:
    """Ancres de mots directes, limitées au commit courant et jamais persistées."""
    words = re.findall(r"\w+(?:['’]\w+)*", text.casefold(), re.UNICODE)
    width = 4
    if len(words) < width:
        return set()
    return {tuple(words[index : index + width]) for index in range(len(words) - width + 1)}


def reconcile_internal_moves(
    added: list[tuple[str, str]],
    removed: list[tuple[str | None, str]],
    minimum: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """Rapproche les blocs d'un commit en temps quasi linéaire."""
    removed_units: list[tuple[str | None, str, set[tuple[str, ...]]]] = []
    exact: dict[str, list[int]] = defaultdict(list)
    anchor_index: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for project, fragment in removed:
        for block in movement_blocks(fragment, minimum):
            anchors = block_anchors(block)
            unit_index = len(removed_units)
            removed_units.append((project, block, anchors))
            exact[block.casefold()].append(unit_index)
            for anchor in anchors:
                anchor_index[anchor].append(unit_index)

    moved_by_project: dict[str, int] = defaultdict(int)
    removed_credit: dict[str, int] = defaultdict(int)
    credited_units: dict[int, int] = defaultdict(int)
    for project, fragment in added:
        for block in movement_blocks(fragment, minimum):
            anchors = block_anchors(block)
            candidates = exact.get(block.casefold(), [])
            best_index = candidates[0] if candidates else None
            common = len(block) if best_index is not None else 0
            if best_index is None and anchors:
                votes: dict[int, int] = defaultdict(int)
                for anchor in anchors:
                    for unit_index in anchor_index.get(anchor, []):
                        votes[unit_index] += 1
                if votes:
                    candidate, shared = max(votes.items(), key=lambda item: item[1])
                    candidate_anchors = removed_units[candidate][2]
                    overlap = shared / max(1, min(len(anchors), len(candidate_anchors)))
                    if shared >= 2 and overlap >= 0.25:
                        best_index = candidate
                        common = common_character_count(block, removed_units[candidate][1])
            if best_index is None or common < minimum:
                continue
            moved_by_project[project] += common
            source_project, source_block, _ = removed_units[best_index]
            if source_project:
                available = max(0, len(source_block) - credited_units[best_index])
                credit = min(common, available)
                removed_credit[source_project] += credit
                credited_units[best_index] += credit
    return moved_by_project, removed_credit


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
        "excluded_folders": list(raw.get("excluded_folders", [])),
        "file_extensions": list(raw.get("file_extensions", [".md"])),
        "import_detection": {
            "threshold_chars": raw.get("import_detection", {}).get("threshold_chars", 1000),
            "threshold_window_minutes": raw.get("import_detection", {}).get("threshold_window_minutes", 15),
        },
        "internal_detection": {
            "minimum_move_chars": raw.get("internal_detection", {}).get("minimum_move_chars", 200),
        },
        "session": {
            "timeout_minutes": raw.get("session", {}).get("timeout_minutes", 45),
        },
    }
    if result["import_detection"]["threshold_chars"] < 0:
        raise ValueError("import_detection.threshold_chars doit être positif.")
    if result["import_detection"]["threshold_window_minutes"] <= 0:
        raise ValueError("import_detection.threshold_window_minutes doit être supérieur à zéro.")
    if result["internal_detection"]["minimum_move_chars"] < 1:
        raise ValueError("internal_detection.minimum_move_chars doit être supérieur à zéro.")
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
        "seen_blobs": [],
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


def transient_file_ranges(repo: Path, commits: list[Any]) -> dict[str, list[tuple[int, int]]]:
    """Repère les fichiers créés puis supprimés, hors renommages exacts."""
    births: dict[str, int] = {}
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for batch_start in range(0, len(commits), 200):
        batch = commits[batch_start : batch_start + 200]
        changes_by_commit = changed_paths_batch(
            repo,
            [commit.sha for commit in batch],
            detect_renames=True,
        )
        for offset, commit in enumerate(batch):
            index = batch_start + offset
            changes = changes_by_commit.get(commit.sha, [])
            for change in changes:
                if change.status == "R" and change.old_path and change.new_path:
                    if change.old_path in births:
                        births[change.new_path] = births.pop(change.old_path)
            additions_by_oid: dict[str, list[str]] = defaultdict(list)
            for change in changes:
                if change.status == "A" and change.new_path and change.new_oid:
                    additions_by_oid[change.new_oid].append(change.new_path)
            renamed_targets: set[str] = set()
            for change in changes:
                if change.status != "D" or not change.old_path:
                    continue
                targets = additions_by_oid.get(change.old_oid or "", [])
                target = next((path for path in targets if path not in renamed_targets), None)
                if target:
                    renamed_targets.add(target)
                    if change.old_path in births:
                        births[target] = births.pop(change.old_path)
                    continue
                born = births.pop(change.old_path, None)
                if born is not None:
                    ranges[change.old_path].append((born, index))
            for change in changes:
                if change.status == "A" and change.new_path and change.new_path not in renamed_targets:
                    births[change.new_path] = index
    return dict(ranges)


def process_history_diff(
    repo: Path,
    state: dict[str, Any],
    config: dict[str, Any],
    metadata: dict[str, Any],
    checkpoint_path: Path | None = None,
    analysis_fingerprint: str = "",
) -> tuple[int, int]:
    """Analyse linéaire par diff, sans indexer le contenu historique."""
    commits = list_commits(repo)
    transient_ranges = transient_file_ranges(repo, commits)
    commit_intervals = {
        commit.sha: (commits[index - 1].timestamp if index else None)
        for index, commit in enumerate(commits)
    }
    # Les anciens états ne conservaient que la date du commit courant. Cette
    # borne permet de répartir le travail sur les jours réellement couverts par
    # un commit espacé, y compris lors d'une migration incrémentale sans diff.
    for event in state.get("events", []):
        event.setdefault("interval_start", commit_intervals.get(event["commit"]))
    full_run = state.get("last_commit") is None
    identity = history_identity(commits, analysis_fingerprint)
    checkpoint = load_checkpoint(checkpoint_path, identity) if full_run and checkpoint_path else None
    start = 0
    relevant_commits = 0
    if checkpoint and checkpoint.get("phase") == "diff-analysis":
        state.clear()
        state.update(checkpoint["state"])
        start = int(checkpoint["next_index"])
        relevant_commits = int(checkpoint["relevant_commits"])
        seen_blobs = set(checkpoint["seen_blobs"])
        print(f"Reprise de l'analyse au commit {start}/{len(commits)}.", file=sys.stderr)
    else:
        seen_blobs = set(state.get("seen_blobs", []))
        previous = state.get("last_commit")
        if previous:
            hashes = [commit.sha for commit in commits]
            if previous not in hashes:
                raise ValueError("Le dernier commit traité n'est plus dans l'historique ; lancez ./analyse.sh full.")
            start = hashes.index(previous) + 1

    pending = commits[start:]
    if not full_run and any(
        born < start <= deleted
        for ranges in transient_ranges.values()
        for born, deleted in ranges
    ):
        raise ValueError(
            "Un fichier transitoire concerne des commits déjà analysés ; "
            "lancez ./analyse.sh full pour corriger les totaux."
        )

    def is_transient(path: str | None, commit_index: int) -> bool:
        return bool(path) and any(
            born <= commit_index <= deleted
            for born, deleted in transient_ranges.get(path, [])
        )
    extensions = {
        str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
        for ext in config["file_extensions"]
    }
    excluded = {str(folder).strip("/").casefold() for folder in config["excluded_folders"]}
    current_paths = configured_project_paths(metadata, include_history=False)
    history_paths = configured_project_paths(metadata, include_history=True)
    base_chars = float(config["import_detection"]["threshold_chars"])
    base_minutes = float(config["import_detection"]["threshold_window_minutes"])
    minimum_move = int(config["internal_detection"]["minimum_move_chars"])
    files = state["files"]
    project_sizes = state["project_sizes"]
    events = state["events"]
    size_points = state["size_points"]
    archive_moves = state["archive_moves"]

    def new_bucket() -> dict[str, Any]:
        return {"added": [], "removed": [], "internal": 0, "folders": set()}

    with BlobReader(repo) as blobs:
        total = len(commits) if full_run else len(pending)
        progress = ProgressBar("Analyse chronologique", total, start if full_run else 0)
        batch_start = -1
        batch_changes: dict[str, list[Any]] = {}
        for relative_index, commit in enumerate(pending, start=1):
            wanted_batch_start = ((relative_index - 1) // 100) * 100
            if wanted_batch_start != batch_start:
                batch_start = wanted_batch_start
                batch = pending[batch_start : batch_start + 100]
                batch_changes = changed_paths_batch(repo, [item.sha for item in batch])
            absolute_index = start + relative_index
            display_index = absolute_index if full_run else relative_index
            work: dict[str, dict[str, Any]] = defaultdict(new_bucket)
            outside_removed: list[str] = []
            additions: list[dict[str, Any]] = []
            deletions: list[dict[str, Any]] = []
            prior_text_pool: list[tuple[str | None, str]] = []
            touched_sizes: set[str] = set()
            commit_new_oids: set[str] = set()

            for change in batch_changes.get(commit.sha, []):
                if change.new_oid:
                    commit_new_oids.add(change.new_oid)
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
                if old_project and old_text:
                    prior_text_pool.append((old_project, old_text))
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

                record = {
                    "change": change,
                    "old": old_text,
                    "new": new_text,
                    "old_project": old_project,
                    "new_project": new_project,
                    "transient": is_transient(new_path or old_path, absolute_index - 1),
                }
                if change.status == "A":
                    additions.append(record)
                elif change.status == "D":
                    deletions.append(record)
                else:
                    added_parts, removed_parts = character_changes(old_text, new_text)
                    if new_project:
                        if record["transient"]:
                            work[new_project]["internal"] += sum(map(len, added_parts))
                        else:
                            work[new_project]["added"].extend(added_parts)
                    if old_project and change.status != "C" and not record["transient"]:
                        work[old_project]["removed"].extend(removed_parts)
                    elif change.status != "C" and not record["transient"]:
                        outside_removed.extend(removed_parts)
                    if change.status in {"R", "C"} and (new_project or old_project):
                        target = new_project or old_project
                        unchanged = min(
                            len(old_text) - sum(map(len, removed_parts)),
                            len(new_text) - sum(map(len, added_parts)),
                        )
                        work[target]["internal"] += max(0, unchanged)

                if old_project and change.status == "R" and new_path:
                    parts = PurePosixPath(new_path).parts
                    if len(parts) >= 2 and parts[0].casefold() == "archives":
                        archive_moves[old_project] = {
                            "archive_folder": "/".join(parts[:2]),
                            "moved_at": commit.timestamp,
                        }

            for record in additions:
                if not record["new_project"]:
                    continue
                change = record["change"]
                if record["transient"]:
                    work[record["new_project"]]["internal"] += len(record["new"])
                elif change.new_oid and change.new_oid in seen_blobs:
                    work[record["new_project"]]["internal"] += len(record["new"])
                else:
                    # Un fichier qui apparaît rempli est suspect, mais ce n'est
                    # pas une preuve d'import. Son texte neuf rejoint les autres
                    # ajouts : les rapprochements internes sont retirés d'abord,
                    # puis le seuil temporel du projet tranche entre écriture et
                    # import. Cela préserve plusieurs jours de travail regroupés
                    # dans un seul commit.
                    work[record["new_project"]]["added"].append(record["new"])

            # Une compilation peut apparaître sans modifier ni supprimer ses
            # chapitres sources. Pour les seuls gros nouveaux fichiers, charger
            # une fois l'état antérieur complet du projet permet de reconnaître
            # ce contenu sans imposer ce coût à chaque commit ordinaire.
            expanded_projects: set[str] = set()
            if commit_intervals.get(commit.sha):
                for record in additions:
                    project = record["new_project"]
                    if not project or len(record["new"]) <= base_chars or project in expanded_projects:
                        continue
                    expanded_projects.add(project)
                    parent_ref = f"{commit.sha}^"
                    for previous_path in list_paths(repo, parent_ref):
                        if project_for(previous_path, extensions, excluded, history_paths) != project:
                            continue
                        previous_text = blobs.text(parent_ref, previous_path) or ""
                        if previous_text:
                            prior_text_pool.append((project, previous_text))
            for record in deletions:
                # La disparition d'un fichier entier sert à détecter un
                # déplacement, mais ne prouve pas une session d'édition.
                outside_removed.append(record["old"])

            removed_pool = [(None, part) for part in outside_removed]
            removed_pool.extend(
                (project, part)
                for project, values in work.items()
                for part in values["removed"]
            )
            added_pool = [
                (project, part)
                for project, values in work.items()
                for part in values["added"]
            ]
            moved_by_project, removed_credit = reconcile_internal_moves(
                added_pool, removed_pool, minimum_move
            )
            duplicated_by_project, _ = reconcile_internal_moves(
                added_pool, prior_text_pool, minimum_move
            )
            for project, values in work.items():
                # Un fragment retiré est aussi présent dans l'ancienne version
                # du fichier : prendre le maximum évite de compter deux fois le
                # même texte comme déplacement puis comme duplication.
                internal_credit = max(
                    moved_by_project[project],
                    duplicated_by_project[project],
                )
                values["internal"] += internal_credit
                added_chars = max(0, sum(map(len, values["added"])) - internal_credit)
                removed_chars = max(
                    0,
                    sum(map(len, values["removed"])) - removed_credit[project],
                )
                gap = minutes_between(
                    commit.timestamp,
                    commit_intervals.get(commit.sha),
                    base_minutes,
                )
                threshold = base_chars * (max(1.0, gap) / base_minutes)
                real_chars = added_chars if added_chars <= threshold else 0
                import_chars = added_chars if added_chars > threshold else 0
                events.append({
                    "timestamp": commit.timestamp,
                    "interval_start": commit_intervals.get(commit.sha),
                    "commit": commit.sha,
                    "project": project,
                    "real_chars": real_chars,
                    "import_chars": import_chars,
                    "internal_chars": values["internal"],
                    "edit_delta": -removed_chars,
                    "folders": sorted(values["folders"]),
                })
                # Le seuil d'import porte sur l'intervalle depuis le commit
                # précédent du Vault : c'est la période pendant laquelle ces
                # modifications ont pu s'accumuler.

            seen_blobs.update(commit_new_oids)

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
                write_checkpoint(checkpoint_path, {
                    "identity": identity,
                    "phase": "diff-analysis",
                    "next_index": absolute_index,
                    "state": state,
                    "seen_blobs": seen_blobs,
                    "relevant_commits": relevant_commits,
                })

    state["seen_blobs"] = sorted(seen_blobs)
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
    return {
        "overview": overview,
        "projects": projects,
        "daily": daily_rows,
        "weekly": rollup("week"),
        "monthly": rollup("month"),
        "size_evolution": sizes,
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
            checkpoint_path=checkpoint_path if full_rebuild else None,
            analysis_fingerprint=fingerprint,
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
