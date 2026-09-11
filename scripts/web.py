#!/usr/bin/env python3
"""Génère uniquement les fichiers statiques du site, sans toucher aux données."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil


FILES = (
    Path("index.html"),
    Path("style.css"),
    Path("dashboard.js"),
    Path("assets/images/favicon.svg"),
    Path("assets/images/download-button.svg"),
)


def config_value(path: Path, name: str, default: str) -> str:
    prefix = f"{name}:"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
                value = value[1:-1]
            return value
    return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère les fichiers statiques du site")
    parser.add_argument("--config", default=None, help="Chemin de config.yaml")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve() if args.config else root / "config.yaml"
    destination = (
        config_path.parent / config_value(config_path, "output_dir", "site")
    ).resolve()
    source = root / "web"
    build_version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    for relative in FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        if relative == Path("index.html"):
            html = (source / relative).read_text(encoding="utf-8")
            temporary.write_text(
                html.replace("__BUILD_VERSION__", build_version), encoding="utf-8"
            )
        else:
            shutil.copyfile(source / relative, temporary)
        temporary.replace(target)
    print(f"Site statique généré dans {destination} (version {build_version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
