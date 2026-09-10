#!/usr/bin/env python3
"""Construit le site statique sans analyser les données ni lancer de serveur."""

from __future__ import annotations

from pathlib import Path
import shutil


FILES = (
    Path("index.html"),
    Path("style.css"),
    Path("dashboard.js"),
    Path("assets/images/favicon.svg"),
    Path("assets/images/download-button.svg"),
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "web"
    destination = root / "site"
    for relative in FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(source / relative, temporary)
        temporary.replace(target)
    print(f"Site web généré dans {destination} (données JSON conservées).")


if __name__ == "__main__":
    main()
