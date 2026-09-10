#!/usr/bin/env python3
"""Crée ou synchronise un miroir Git local dédié à WritingLog."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("PyYAML manque. Lancez : python -m pip install -r scripts/requirements.txt", file=sys.stderr)
    raise SystemExit(2)


def git_output(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def valid_mirror(target: Path, source: Path) -> bool:
    return (
        git_output(target, "rev-parse", "--is-bare-repository") == "true"
        and git_output(target, "remote", "get-url", "origin") == str(source)
        and int(git_output(target, "rev-list", "--all", "--count") or "0") > 0
    )


def remove_cache(target: Path, source: Path, base_dir: Path) -> None:
    forbidden = {Path("/").resolve(), Path.home().resolve(), source, base_dir.resolve()}
    if target in forbidden or len(target.parts) < 3:
        raise ValueError(f"Chemin de cache dangereux, suppression refusée : {target}")
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Met en cache l'historique Git du vault")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("rebuild", "update"),
        default="rebuild",
        help="rebuild supprime et recrée le cache ; update le synchronise",
    )
    parser.add_argument("--config", default=None, help="Chemin de config.yaml")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve() if args.config else project_root / "config.yaml"
    base_dir = config_path.parent
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    source = (base_dir / str(config.get("vault_path", "."))).resolve()
    target = (base_dir / str(config.get("history_repo", ".cache/vault-history.git"))).resolve()
    if target == source:
        print("Erreur : history_repo doit être distinct de vault_path.", file=sys.stderr)
        return 1

    try:
        if args.action == "update":
            if not target.exists() or not valid_mirror(target, source):
                print(
                    "Erreur : miroir absent ou invalide. Lancez d'abord : ./cache.sh",
                    file=sys.stderr,
                )
                return 1
            print(f"Synchronisation du miroir : {target}", flush=True)
            subprocess.run(
                ["git", "-C", str(target), "remote", "update", "--prune"],
                check=True,
            )
        else:
            if target.exists() or target.is_symlink():
                print(f"Suppression de l'ancien cache : {target}", flush=True)
                remove_cache(target, source, base_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f"Création du miroir depuis : {source}", flush=True)
            print(f"Destination : {target}", flush=True)
            subprocess.run(
                ["git", "clone", "--mirror", "--no-hardlinks", "--progress", str(source), str(target)],
                check=True,
            )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Erreur pendant la synchronisation : {exc}", file=sys.stderr)
        return 1

    source_commits = int(git_output(source, "rev-list", "--all", "--count") or "0")
    cached_commits = int(git_output(target, "rev-list", "--all", "--count") or "0")
    if source_commits == 0 or cached_commits != source_commits:
        print(
            f"Erreur : miroir incomplet ({cached_commits}/{source_commits} commits).",
            file=sys.stderr,
        )
        return 1

    size = subprocess.run(
        ["du", "-sh", str(target)],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.split("\t", 1)[0]
    print(f"Miroir prêt : {cached_commits} commits, {size or 'taille inconnue'}.")
    if args.action == "rebuild":
        print("Lancez ensuite : ./analyse.sh full")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
