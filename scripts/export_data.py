#!/usr/bin/env python3
"""Exporte les vues JSON du dashboard depuis l'état analytique SQLite."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable


def config_value(path: Path, name: str, default: str) -> str:
    """Lit un chemin scalaire de config.yaml sans dépendance YAML."""
    prefix = f"{name}:"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
                value = value[1:-1]
            return value
    return default


def production_days(interval_start: str | None, timestamp: str) -> list[str]:
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
    quotient, remainder = divmod(value, len(days))
    first_extra = len(days) - remainder
    for index, day in enumerate(days):
        yield day, quotient + (1 if index >= first_extra else 0)


def aggregate(state: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    events = sorted(
        state["events"],
        key=lambda item: (item["timestamp"], item["commit"], item["project"]),
    )
    active_projects = {
        project for project, size in state["project_sizes"].items() if int(size) > 0
    }
    known_projects = {event["project"] for event in events}
    selected_projects = {str(project).casefold() for project in metadata}
    active_projects.update(
        project for project in known_projects if project.casefold() in selected_projects
    )
    daily: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"signes_reels": 0, "signes_supprimes": 0, "dossiers": set()}
    )
    events_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event["project"] not in active_projects:
            continue
        if int(event.get("real_chars", 0)) <= 0 and int(event.get("edit_delta", 0)) >= 0:
            continue
        events_by_project[event["project"]].append(event)
        days = production_days(event.get("interval_start"), event["timestamp"])
        for day, chars in split_integer_over_days(int(event["real_chars"]), days):
            daily[(day, event["project"])]["signes_reels"] += chars
            daily[(day, event["project"])]["dossiers"].update(event.get("folders", []))
        deleted_chars = max(0, -int(event.get("edit_delta", 0)))
        for day, chars in split_integer_over_days(deleted_chars, days):
            daily[(day, event["project"])]["signes_supprimes"] += chars

    daily_rows = [
        {
            "periode": period,
            "projet": project,
            "signes_reels": int(values["signes_reels"]),
            "signes_supprimes": int(values["signes_supprimes"]),
            "dossiers": sorted(values["dossiers"]),
        }
        for (period, project), values in sorted(daily.items())
    ]

    def rollup(kind: str) -> list[dict[str, Any]]:
        rolled: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"signes_reels": 0, "signes_supprimes": 0, "dossiers": set()}
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
            values["dossiers"].update(row.get("dossiers", []))
        return [
            {
                "periode": period,
                "projet": project,
                "signes_reels": int(values["signes_reels"]),
                "signes_supprimes": int(values["signes_supprimes"]),
                "dossiers": sorted(values["dossiers"]),
            }
            for (period, project), values in sorted(rolled.items())
        ]

    projects: list[dict[str, Any]] = []
    metadata_folded = {str(key).casefold(): value for key, value in metadata.items()}
    for project in sorted(active_projects):
        project_events = events_by_project.get(project, [])
        custom = metadata.get(project, metadata_folded.get(project.casefold(), {}))
        custom = custom if isinstance(custom, dict) else {}
        real_total = sum(int(event["real_chars"]) for event in project_events)
        deleted_total = sum(
            max(0, -int(event.get("edit_delta", 0))) for event in project_events
        )
        historical_added_total = sum(
            int(event.get("real_chars", 0))
            + int(event.get("import_chars", 0))
            + int(event.get("internal_chars", 0))
            for event in project_events
        )
        if deleted_total > historical_added_total:
            print(
                f"Avertissement : {project} totalise {deleted_total} signes supprimés, "
                f"davantage que les {historical_added_total} signes ajoutés.",
                file=sys.stderr,
            )
        active_size_root = state.get("active_size_roots", {}).get(project)
        excluded_size = sum(
            int(active.get("size", 0))
            for lifecycle in state.get("file_lifecycles", {}).values()
            if (active := lifecycle.get("active_creation"))
            and active.get("project") == project
            and active.get("excluded_from_size")
            and active.get("size_root") == active_size_root
        )
        projects.append({
            **custom,
            "id": project,
            "title": custom.get("title", project),
            "signes_reels_total": real_total,
            "signes_supprimes_total": deleted_total,
            "taille_actuelle": max(
                0, int(state["project_sizes"].get(project, 0)) - excluded_size
            ),
            "date_creation": project_events[0]["timestamp"] if project_events else None,
            "derniere_activite": project_events[-1]["timestamp"] if project_events else None,
        })

    sizes = [
        {
            "date": point["timestamp"],
            "commit": point["commit"],
            "projet": point["project"],
            "taille_signes": int(point["size"]),
            "taille_brute": int(point.get("raw_size", point["size"])),
            "modifie": bool(point.get("touched", False)),
        }
        for point in sorted(
            state.get("size_points", []),
            key=lambda item: (item["timestamp"], item["commit"], item["project"]),
        )
        if point["project"] in active_projects
    ]

    total_real = sum(project["signes_reels_total"] for project in projects)
    total_deleted = sum(project["signes_supprimes_total"] for project in projects)
    cutoff = datetime.now().astimezone().date() - timedelta(days=30)
    recent_scores: dict[str, int] = defaultdict(int)
    for row in daily_rows:
        if datetime.fromisoformat(row["periode"]).date() >= cutoff:
            recent_scores[row["projet"]] += row["signes_reels"]
    most_active = max(recent_scores, key=recent_scores.get) if recent_scores else None
    title_by_id = {project["id"]: project["title"] for project in projects}
    overview = {
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
    selected = {str(project).casefold() for project in metadata}
    history = {event["project"] for event in state["events"]}
    inactive = {
        project
        for project in history
        if int(state["project_sizes"].get(project, 0)) == 0
    }
    archive_root = next(
        (
            item
            for item in vault.iterdir()
            if item.is_dir() and item.name.casefold() == "archives"
        ),
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
        if not destination:
            continue
        destination_path = vault / destination
        manuscript = (
            next(
                (
                    item.name
                    for item in destination_path.iterdir()
                    if item.is_dir() and item.name.casefold() == "manuscrit"
                ),
                None,
            )
            if destination_path.is_dir()
            else None
        )
        folder = f"{destination}/{manuscript}" if manuscript else destination
        candidates[project] = {"title": project, "folder": folder}
    return candidates


def write_archived_projects(path: Path, projects: dict[str, dict[str, Any]]) -> None:
    lines: list[str] = []
    for project, fields in projects.items():
        yaml_key = (
            project
            if re.fullmatch(r"[A-Za-z0-9_-]+", project)
            else json.dumps(project, ensure_ascii=False)
        )
        lines.append(f"{yaml_key}:")
        for name, value in fields.items():
            lines.append(f"  {name}: {json.dumps(value, ensure_ascii=False)}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Exporte les JSON depuis SQLite")
    parser.add_argument("--config", default=None, help="Chemin de config.yaml")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve() if args.config else root / "config.yaml"
    base_dir = config_path.parent
    output = (base_dir / config_value(config_path, "output_dir", "site")).resolve()
    database_path = (
        base_dir / config_value(config_path, "fingerprint_db", ".cache/fingerprints.sqlite3")
    ).resolve()
    if not database_path.exists():
        print(f"Base d'analyse absente : {database_path}. Lancez ./analyse.sh full.", file=sys.stderr)
        return 1
    try:
        with sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True) as database:
            row = database.execute(
                "SELECT payload FROM analysis_state WHERE id = 1"
            ).fetchone()
            project_commit_rows = database.execute(
                "SELECT c.commit_timestamp, pc.commit_hash, pc.project, pc.size, "
                "pc.raw_size, pc.size_root, pc.touched FROM project_commits AS pc "
                "JOIN commits AS c ON c.commit_hash = pc.commit_hash "
                "ORDER BY c.rowid, pc.project"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        print(f"Impossible de lire l'état analytique : {exc}", file=sys.stderr)
        return 1
    if not row:
        print("État analytique absent de SQLite. Lancez ./analyse.sh full.", file=sys.stderr)
        return 1
    state = json.loads(row[0])
    state["size_points"] = [
        {
            "timestamp": timestamp,
            "commit": commit_hash,
            "project": project,
            "size": int(size),
            "raw_size": int(raw_size),
            "size_root": size_root,
            "touched": bool(touched),
        }
        for timestamp, commit_hash, project, size, raw_size, size_root, touched
        in project_commit_rows
    ]
    exports = aggregate(state, state.get("project_metadata", {}))
    data_directory = output / "data"
    data_directory.mkdir(parents=True, exist_ok=True)
    generated_names = {f"{name}.json" for name in exports}
    for stale in data_directory.glob("*.json"):
        if stale.name not in generated_names:
            stale.unlink()
    for name, payload in exports.items():
        target = data_directory / f"{name}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    vault = (base_dir / config_value(config_path, "vault_path", ".")).resolve()
    archived_path = (
        base_dir
        / config_value(config_path, "archived_projects_file", "projets_archives.yml")
    ).resolve()
    write_archived_projects(
        archived_path,
        archived_project_candidates(vault, state, state.get("project_metadata", {})),
    )
    print(f"{len(exports)} JSON générés dans {data_directory} depuis {database_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
