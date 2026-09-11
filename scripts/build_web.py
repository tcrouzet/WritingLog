#!/usr/bin/env python3
"""Construit le site statique sans analyser les données ni lancer de serveur."""

from __future__ import annotations

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


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "web"
    destination = root / "site"
    build_version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    for relative in FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        if relative == Path("index.html"):
            html = (source / relative).read_text(encoding="utf-8")
            temporary.write_text(
                html.replace("__BUILD_VERSION__", build_version),
                encoding="utf-8",
            )
        else:
            shutil.copyfile(source / relative, temporary)
        temporary.replace(target)
    print(
        f"Site web généré dans {destination} "
        f"(version {build_version}, données JSON conservées)."
    )


if __name__ == "__main__":
    main()
