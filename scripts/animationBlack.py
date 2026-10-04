#!/usr/bin/env python3
"""
Anime l'évolution d'un fichier à partir de son historique Git (ne dépend
d'aucune base WritingLog : tout est recalculé directement depuis
`git log` / `git show`).

Fonctionne sans aucun paramètre : placez ce script à la racine de votre
dossier WritingLog (à côté de config.yaml et projet.yml) et lancez-le tel
quel :

    python writinglog_text_animation.py

Il va lire tout seul :
  - `history_repo` dans config.yaml, pour trouver le mirroir local des
    commits déjà téléchargé (`.cache/vault-history.git`) ;
  - la clé `Animation:` dans projet.yml, pour savoir quel fichier animer,
    par exemple :

        Animation: "tcrouzet/2026/10/hypocrisie-editoriale.md"

  - `output_dir` dans config.yaml, pour écrire le résultat dans
    `<output_dir>/animations/<nom-du-fichier>.mp4`.

Le document est reconstruit mot par mot, pas approximé par des bandes de
couleur abstraites : chaque mot garde en mémoire le nombre de fois où il a
été réécrit d'un commit à l'autre, et c'est ce compteur qui fixe sa couleur,
façon dégradé Photoshop :
  - jamais touché (texte neuf, où qu'il atterrisse) -> bleu clair
  - réécrit une fois -> vert
  - réécrit deux fois -> jaune
  - réécrit trois fois ou plus -> rouge

Format vidéo 16:9 (type YouTube) : juste la date, le +/- signes, et la page
qui se remplit de haut en bas (comme on écrit), avec un vrai retour à la
ligne et des paragraphes, pour respecter la forme réelle du texte plutôt
qu'une simple barre étirée. Pas de nom de fichier affiché, pas de graphique
de progression (sacrifié au profit du format vidéo).

Dépendances :
    pip install matplotlib
Export MP4 : nécessite ffmpeg installé et accessible dans le PATH.

Si le fichier a été renommé ou déplacé pendant son histoire, le script suit
les renommages Git automatiquement (comme `git log --follow`) : pas besoin
de lister les anciens chemins.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter


# ---------------------------------------------------------------------------
# Accès Git
# ---------------------------------------------------------------------------

def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def config_value(path: Path, name: str, default: str | None = None) -> str | None:
    """Lit un scalaire simple de config.yaml sans dépendre de PyYAML
    (même logique que celle utilisée par WritingLog lui-même)."""
    prefix = f"{name}:"
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith(prefix):
            value = line[len(prefix):].strip()
            if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
                value = value[1:-1]
            return value
    return default


# Le script est pensé pour vivre à côté de config.yaml et projet.yml, dans
# le dossier WritingLog. Tout se déduit de là, sans paramètre.
SCRIPT_DIR = Path(__file__).resolve().parent


def find_writinglog_dir(start: Path) -> Path:
    """Cherche config.yaml + projet.yml à côté du script, sinon dans les
    dossiers parents (au cas où le script serait rangé ailleurs, ex: scripts/)."""
    for candidate in [start, *start.parents]:
        if (candidate / "config.yaml").exists() and (candidate / "projet.yml").exists():
            return candidate
    raise RuntimeError(
        "config.yaml et projet.yml introuvables à côté de ce script. "
        "Placez writinglog_text_animation.py à la racine de votre dossier WritingLog, "
        "ou utilisez --config / --projet / --repo / --file pour forcer les chemins."
    )


def resolve_repo(repo_arg: Path | None, config_path: Path) -> Path:
    """Détermine le dépôt Git à utiliser.

    Par défaut, on préfère le mirroir local déjà téléchargé par WritingLog
    (`history_repo` dans son config.yaml, typiquement
    `.cache/vault-history.git`) plutôt que le vault en direct : c'est plus
    rapide, ça ne dépend pas du montage du vault, et c'est la source
    exacte que WritingLog utilise pour son propre historique.
    """
    if repo_arg is not None:
        return repo_arg
    history_repo = config_value(config_path, "history_repo")
    if not history_repo:
        raise RuntimeError(f"history_repo introuvable dans {config_path}.")
    candidate = Path(history_repo)
    resolved = candidate if candidate.is_absolute() else (config_path.parent / candidate)
    if not resolved.exists():
        raise RuntimeError(
            f"Le mirroir {resolved} n'existe pas encore. "
            "Lancez d'abord ./cache.sh (ou ./cache.sh update) dans WritingLog."
        )
    return resolved


def resolve_animation_file(file_arg: str | None, projet_path: Path) -> str:
    """Trouve le fichier à animer via la clé `Animation:` de projet.yml,
    sauf si --file force explicitement un chemin."""
    if file_arg is not None:
        return file_arg
    value = config_value(projet_path, "Animation")
    if not value:
        raise RuntimeError(
            f'Clé "Animation:" introuvable dans {projet_path}. '
            'Ajoutez par exemple :\n\nAnimation: "tcrouzet/2026/10/hypocrisie-editoriale.md"'
        )
    return value


def resolve_output(output_arg: Path | None, config_path: Path, file_path: str) -> Path:
    """Fichier de sortie par défaut : <output_dir>/animations/<nom>.mp4,
    <output_dir> étant celui déclaré dans config.yaml (par défaut "site")."""
    if output_arg is not None:
        return output_arg
    output_dir = config_value(config_path, "output_dir", "site") or "site"
    stem = Path(file_path).stem
    target_dir = config_path.parent / output_dir / "animations"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{stem}.mp4"


@dataclass
class CommitRef:
    sha: str
    date: datetime
    path: str
    status: str  # 'A', 'M', 'D' ou 'R100' (renommage)


def commits_for_file(repo: Path, file_path: str) -> list[CommitRef]:
    """Liste, du plus ancien au plus récent, les commits ayant touché le
    fichier, en suivant les renommages (équivalent de `git log --follow`).

    Important : on NE PASSE PAS --reverse à Git. La combinaison
    `--follow --reverse` est un bug connu de Git qui peut tronquer
    l'historique suivi (parfois un seul commit retourné). On récupère donc
    l'historique dans l'ordre naturel (le plus récent d'abord) et on
    l'inverse nous-mêmes en Python, à la fin.
    """
    result = git(
        repo,
        "log",
        "--follow",
        "--name-status",
        "--format=COMMIT\t%H\t%cI",
        "--",
        file_path,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git log a échoué :\n{result.stderr}")

    commits: list[CommitRef] = []
    current_sha: str | None = None
    current_date: datetime | None = None
    pending_lines: list[str] = []

    def flush() -> None:
        if current_sha is None:
            return
        status = "M"
        path = None
        for line in pending_lines:
            parts = line.split("\t")
            status = parts[0]
            path = parts[2] if status.startswith("R") else parts[1]
        if path:
            commits.append(CommitRef(current_sha, current_date, path, status))

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if line.startswith("COMMIT\t"):
            flush()
            pending_lines = []
            _, sha, iso_date = line.split("\t")
            current_sha = sha
            current_date = datetime.fromisoformat(iso_date)
        else:
            pending_lines.append(line)
    flush()
    commits.reverse()  # Git nous les a donnés du plus récent au plus ancien.
    return commits


def file_content_at(repo: Path, commit: CommitRef) -> str:
    if commit.status == "D":
        return ""
    result = git(repo, "show", f"{commit.sha}:{commit.path}")
    if result.returncode != 0:
        return ""
    return result.stdout


# ---------------------------------------------------------------------------
# Reconstruction du texte, mot par mot, avec un compteur d'édition persistant
# ---------------------------------------------------------------------------
#
# Plutôt que de deviner après coup "est-ce une édition ou de la croissance ?"
# à partir d'un diff isolé entre deux commits, on reconstruit le document
# comme une vraie structure qui vit d'un commit à l'autre : chaque mot garde
# la mémoire du nombre de fois où il a été réécrit. Un mot jamais touché
# reste bleu (0 édition) ; à chaque réécriture il avance d'un cran vers le
# vert (1), le jaune (2), le rouge (3 et plus). Un mot tout neuf démarre
# toujours à 0, qu'il soit ajouté à la fin, au milieu, ou juste avant des
# notes en vrac laissées en bas de page.

PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
WORD_RE = re.compile(r"\S+")
PARAGRAPH_BREAK = "¶"

# Couleur selon le nombre de fois où un mot a été réécrit : 0, 1, 2, 3+.
EDIT_LEVEL_COLORS = ["#04ebf9", "#00e676", "#ffee58", "#ff1744"]
BACKGROUND_RGB = (0.055, 0.067, 0.09)  # assorti au fond de la figure, pour l'espace "pas encore écrit"


def tokenize_doc(text: str) -> list[str]:
    """Découpe le texte en mots (ponctuation collée), avec un marqueur de
    saut de paragraphe pour préserver la structure du document."""
    tokens: list[str] = []
    paragraphs = PARAGRAPH_SPLIT_RE.split(text.strip())
    for index, paragraph in enumerate(paragraphs):
        if index > 0:
            tokens.append(PARAGRAPH_BREAK)
        tokens.extend(WORD_RE.findall(paragraph))
    return tokens


def update_doc_state(
    doc_state: list[dict[str, Any]], new_text: str
) -> tuple[list[dict[str, Any]], int, int]:
    """Fait évoluer l'état persistant du document (liste de mots avec leur
    compteur d'édition) vers `new_text`, et renvoie (nouvel état, signes
    ajoutés, signes supprimés) pour ce commit."""
    old_tokens = [entry["text"] for entry in doc_state]
    new_tokens = tokenize_doc(new_text)
    matcher = SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)

    new_state: list[dict[str, Any]] = []
    added_chars = 0
    removed_chars = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            new_state.extend(doc_state[i1:i2])
        elif tag == "insert":
            for token in new_tokens[j1:j2]:
                new_state.append({"text": token, "edits": 0})
                added_chars += len(token)
        elif tag == "delete":
            for entry in doc_state[i1:i2]:
                removed_chars += len(entry["text"])
        elif tag == "replace":
            old_count = i2 - i1
            new_count = j2 - j1
            common = min(old_count, new_count)
            # Les `common` premiers mots du remplacement héritent (et
            # incrémentent) le compteur d'édition des mots qu'ils
            # remplacent : c'est une vraie réécriture -> plus chaud.
            for k in range(common):
                old_entry = doc_state[i1 + k]
                new_token = new_tokens[j1 + k]
                new_state.append({"text": new_token, "edits": old_entry["edits"] + 1})
                removed_chars += len(old_entry["text"])
                added_chars += len(new_token)
            # S'il reste des mots neufs au-delà (cas fréquent : on retouche
            # la fin d'une phrase tout en continuant à écrire derrière),
            # ils démarrent à 0 -> bleu, peu importe où ils atterrissent.
            for token in new_tokens[j1 + common:j2]:
                new_state.append({"text": token, "edits": 0})
                added_chars += len(token)
            # S'il reste des mots de l'ancien texte non remplacés, ils sont
            # simplement supprimés.
            for entry in doc_state[i1 + common:i2]:
                removed_chars += len(entry["text"])

    return new_state, added_chars, removed_chars


def build_timeline(repo: Path, file_path: str, max_commits: int | None) -> list[dict[str, Any]]:
    commits = commits_for_file(repo, file_path)
    if not commits:
        raise RuntimeError(
            f"Aucun commit trouvé pour {file_path!r}. Vérifiez --repo et --file."
        )
    if max_commits:
        commits = commits[-max_commits:]

    timeline: list[dict[str, Any]] = []
    doc_state: list[dict[str, Any]] = []
    cum_added = 0
    cum_removed = 0
    for commit in commits:
        text = file_content_at(repo, commit)
        doc_state, added, removed = update_doc_state(doc_state, text)
        cum_added += added
        cum_removed += removed
        timeline.append(
            {
                "sha": commit.sha[:8],
                "date": commit.date,
                "size": len(text),
                "doc_state": doc_state,
                "added": added,
                "removed": removed,
                "cum_added": cum_added,
                "cum_removed": cum_removed,
            }
        )
    return timeline


# ---------------------------------------------------------------------------
# Mise en page : reconstituer la vraie forme du texte (mots, lignes, pages)
# ---------------------------------------------------------------------------

PAGE_COLUMNS = 120  # largeur de page, en caractères, pour le retour à la ligne (image au format plus large que haut)


def layout_rows(doc_state: list[dict[str, Any]], columns: int = PAGE_COLUMNS) -> list[list[str | None]]:
    """Transforme la liste de mots en lignes de page (retour à la ligne
    automatique, paragraphes séparés par une ligne vide), chaque case de la
    ligne contenant soit la couleur du mot, soit None (espace/fond)."""
    rows: list[list[str | None]] = []
    current: list[str | None] = []
    for entry in doc_state:
        if entry["text"] == PARAGRAPH_BREAK:
            rows.append(current)
            rows.append([])
            current = []
            continue
        word = entry["text"]
        color = EDIT_LEVEL_COLORS[min(entry["edits"], len(EDIT_LEVEL_COLORS) - 1)]
        # Un espace réservé (1 caractère) entre deux mots sur la même ligne,
        # mais coloré comme le mot qui suit plutôt que laissé en fond sombre :
        # ça évite les points noirs parasites entre les mots tout en gardant
        # une légère séparation visuelle.
        needed = len(word) + (1 if current else 0)
        if len(current) + needed > columns:
            rows.append(current)
            current = []
        if current:
            current.append(color)
        current.extend([color] * len(word))
    if current:
        rows.append(current)
    return rows


def rows_to_grid(rows: list[list[str | None]], total_rows: int, columns: int = PAGE_COLUMNS) -> np.ndarray:
    grid = np.tile(np.array(BACKGROUND_RGB), (total_rows, columns, 1))
    for row_index, row in enumerate(rows[:total_rows]):
        for col_index, color in enumerate(row[:columns]):
            if color is not None:
                grid[row_index, col_index] = matplotlib.colors.to_rgb(color)
    return grid


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

def render(
    timeline: list[dict[str, Any]],
    output: Path,
    hold_frames: int,
    fps: int,
    dpi: int,
    columns: int,
) -> None:
    # On précalcule la mise en page (retour à la ligne) de chaque commit une
    # bonne fois pour toutes, et la hauteur totale de page nécessaire pour
    # contenir la version la plus longue : le bandeau grandit sur une échelle
    # fixe, comme une vraie page qui se remplit.
    all_rows = [layout_rows(point["doc_state"], columns) for point in timeline]
    total_rows = max((len(rows) for rows in all_rows), default=1) or 1
    # Précalculé une bonne fois pour toutes : évite de refaire la mise en
    # page à chaque image pendant les `hold_frames` images où un même commit
    # reste affiché.
    all_grids = [rows_to_grid(rows, total_rows, columns) for rows in all_rows]

    # Format vidéo 16:9 (YouTube), indépendant du nombre de lignes : plus le
    # texte est long, plus chaque ligne est tassée (rendue petite), mais
    # l'image elle-même garde toujours ce même format.
    fig = plt.figure(figsize=(16, 9), facecolor="#0e1117")
    grid = fig.add_gridspec(1, 1, top=0.8, bottom=0.04, left=0.05, right=0.97)
    ax_page = fig.add_subplot(grid[0])
    ax_page.set_facecolor("#0e1117")

    # --- La page qui se remplit, mot par mot, de haut en bas --------------
    ax_page.set_xlim(0, columns)
    ax_page.set_ylim(total_rows, 0)  # ligne 0 = début du texte, en haut
    ax_page.set_xticks([])
    ax_page.set_yticks([])
    for spine in ax_page.spines.values():
        spine.set_color("#44505f")
    page_image = np.tile(np.array(BACKGROUND_RGB), (total_rows, columns, 1))
    page_im = ax_page.imshow(
        page_image, extent=[0, columns, total_rows, 0], aspect="auto", interpolation="nearest",
    )
    size_label = ax_page.text(
        0.5, 1.012, "", transform=ax_page.transAxes, ha="center", va="bottom",
        color="white", fontsize=11, fontweight="bold",
    )

    header_date = fig.text(
        0.5, 0.96, "", color="white", fontsize=26, fontweight="bold",
        ha="center", va="top",
    )
    header_delta = fig.text(
        0.5, 0.905, "", color="#cfd8e3", fontsize=14,
        ha="center", va="top",
    )

    total_frames = len(timeline) * hold_frames

    def frame_to_commit(frame: int) -> int:
        return min(frame // hold_frames, len(timeline) - 1)

    def update(frame: int):
        commit_index = frame_to_commit(frame)
        point = timeline[commit_index]
        page_im.set_data(all_grids[commit_index])

        size_label.set_text(f"{point['size']:,}".replace(",", " ") + " signes")

        header_date.set_text(point["date"].strftime("%Y-%m-%d %H:%M"))
        header_delta.set_text(
            f"+{int(point['added'])} / -{int(point['removed'])} signes "
            f"(total ajouté {int(point['cum_added'])}, supprimé {int(point['cum_removed'])})"
        )
        return page_im, size_label, header_date, header_delta

    anim = FuncAnimation(fig, update, frames=total_frames, interval=1000 / fps, blit=False)

    output = Path(output)
    if output.suffix.lower() == ".gif":
        anim.save(output, writer=PillowWriter(fps=fps), dpi=dpi)
    else:
        anim.save(output, fps=fps, dpi=dpi)
    plt.close(fig)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    # Tout est optionnel : par défaut, rien à passer en ligne de commande.
    # Le script se débrouille avec config.yaml (history_repo, output_dir)
    # et projet.yml (clé "Animation:") trouvés à côté de lui.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=None, help="Forcer le dépôt Git (sinon : history_repo de config.yaml)")
    parser.add_argument("--config", type=Path, default=None, help="Forcer le chemin de config.yaml")
    parser.add_argument("--projet", type=Path, default=None, help="Forcer le chemin de projet.yml")
    parser.add_argument("--file", default=None, help="Forcer le fichier à animer (sinon : clé Animation: de projet.yml)")
    parser.add_argument("--output", default=None, type=Path, help="Forcer le fichier de sortie (.gif ou .mp4)")
    parser.add_argument("--hold-frames", type=int, default=12, help="Images tenues par commit (vitesse)")
    parser.add_argument("--fps", type=int, default=20, help="Images par seconde de la vidéo finale")
    parser.add_argument("--dpi", type=int, default=120)
    parser.add_argument("--columns", type=int, default=PAGE_COLUMNS, help="Largeur de page en caractères (retour à la ligne)")
    parser.add_argument("--max-commits", type=int, default=None, help="Limiter aux N derniers commits touchant le fichier")
    args = parser.parse_args()

    writinglog_dir = find_writinglog_dir(SCRIPT_DIR)
    config_path = args.config or (writinglog_dir / "config.yaml")
    projet_path = args.projet or (writinglog_dir / "projet.yml")

    repo = resolve_repo(args.repo, config_path)
    file_path = resolve_animation_file(args.file, projet_path)
    output = resolve_output(args.output, config_path, file_path)

    print(f"Lecture de l'historique de {file_path!r} dans {repo} …")
    timeline = build_timeline(repo, file_path, args.max_commits)
    print(f"{len(timeline)} commits trouvés, du {timeline[0]['date'].date()} au {timeline[-1]['date'].date()}.")

    render(
        timeline,
        output,
        hold_frames=args.hold_frames,
        fps=args.fps,
        dpi=args.dpi,
        columns=args.columns,
    )
    print(f"Animation écrite dans {output}")


if __name__ == "__main__":
    main()
