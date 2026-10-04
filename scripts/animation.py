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

Le document est reconstruit mot par mot, en vraies lettres (police à chasse
fixe, dessinée avec Pillow), sur fond papier blanc — pas une mosaïque de
blocs de couleur. Chaque mot garde en mémoire le nombre de fois où il a été
réécrit d'un commit à l'autre, et c'est ce compteur qui fixe sa couleur :
  - jamais touché (texte neuf, où qu'il atterrisse) -> noir
  - réécrit une fois -> bleu
  - réécrit deux fois -> fuchsia
  - réécrit trois fois ou plus -> rouge

Format vidéo 16:9 (type YouTube) : la date, le +/- signes, et la page qui se
remplit de haut en bas (comme on écrit), avec un vrai retour à la ligne et
des paragraphes, pour respecter la forme réelle du texte. Pas de nom de
fichier affiché, pas de graphique de progression (sacrifié au profit du
format vidéo).

Dépendances :
    pip install matplotlib pillow
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
from PIL import Image, ImageDraw, ImageFont


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
# reste noir, comme une vraie page écrite (0 édition) ; à chaque réécriture
# il avance d'un cran vers le bleu (1), le fuchsia (2), le rouge (3 et
# plus). Un mot tout neuf démarre toujours à 0, qu'il soit ajouté à la fin,
# au milieu, ou juste avant des notes en vrac laissées en bas de page.

PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")
WORD_RE = re.compile(r"\S+")
PARAGRAPH_BREAK = "¶"

# Couleur selon le nombre de fois où un mot a été réécrit : 0, 1, 2, 3+.
# Fond papier blanc, texte initial en noir, puis bleu -> fuchsia -> rouge.
EDIT_LEVEL_COLORS = ["#111111", "#1455ff", "#d6189c", "#e3121b"]


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

PAGE_COLUMNS = 100  # largeur de page, en caractères, pour le retour à la ligne


def layout_rows(doc_state: list[dict[str, Any]], columns: int = PAGE_COLUMNS) -> list[list[tuple[int, str, str]]]:
    """Transforme la liste de mots en lignes de page (retour à la ligne
    automatique, paragraphes séparés par une ligne vide). Chaque ligne est
    une liste de (colonne de départ, mot, couleur) — on garde le vrai mot,
    pas une couleur par caractère, pour pouvoir l'écrire en vraies lettres."""
    rows: list[list[tuple[int, str, str]]] = []
    current: list[tuple[int, str, str]] = []
    cursor = 0
    for entry in doc_state:
        if entry["text"] == PARAGRAPH_BREAK:
            rows.append(current)
            rows.append([])
            current = []
            cursor = 0
            continue
        word = entry["text"]
        color = EDIT_LEVEL_COLORS[min(entry["edits"], len(EDIT_LEVEL_COLORS) - 1)]
        needed = len(word) + (1 if current else 0)
        if cursor + needed > columns:
            rows.append(current)
            current = []
            cursor = 0
        if current:
            cursor += 1  # espace avant le mot
        current.append((cursor, word, color))
        cursor += len(word)
    if current:
        rows.append(current)
    return rows


# ---------------------------------------------------------------------------
# Rendu "vraies lettres" sur fond papier, via Pillow
# ---------------------------------------------------------------------------

PAPER_RGB = (255, 255, 255)


def find_monospace_font() -> str:
    from matplotlib import font_manager
    return font_manager.findfont(font_manager.FontProperties(family="monospace"))


def fit_monospace_font(font_path: str, target_char_width: float, size_hint: int = 40) -> "ImageFont.FreeTypeFont":
    """Cherche la taille de police telle que la largeur d'un caractère
    (police à chasse fixe) corresponde à `target_char_width` pixels."""
    probe = ImageFont.truetype(font_path, size_hint)
    probe_width = probe.getlength("M") or 1
    size = max(6, int(size_hint * target_char_width / probe_width))
    return ImageFont.truetype(font_path, size)


def render_frame_image(
    point: dict[str, Any],
    rows: list[list[tuple[int, str, str]]],
    frame_width: int,
    frame_height: int,
    margin_x: int,
    text_top: int,
    char_width: float,
    row_height: float,
    font: "ImageFont.FreeTypeFont",
    header_font: "ImageFont.FreeTypeFont",
    sub_font: "ImageFont.FreeTypeFont",
) -> np.ndarray:
    image = Image.new("RGB", (frame_width, frame_height), PAPER_RGB)
    draw = ImageDraw.Draw(image)

    date_text = point["date"].strftime("%Y-%m-%d %H:%M")
    draw.text((frame_width / 2, 36), date_text, font=header_font, fill="#111111", anchor="ma")
    delta_text = (
        f"+{int(point['added'])} / -{int(point['removed'])} signes "
        f"(total ajouté {int(point['cum_added'])}, supprimé {int(point['cum_removed'])})"
    )
    draw.text((frame_width / 2, 36 + header_font.size + 10), delta_text, font=sub_font, fill="#555555", anchor="ma")

    for row_index, row in enumerate(rows):
        y = text_top + row_index * row_height
        if y > frame_height - 4:
            break
        for col_start, word, color in row:
            x = margin_x + col_start * char_width
            draw.text((x, y), word, font=font, fill=color)

    return np.asarray(image)


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

def render(
    timeline: list[dict[str, Any]],
    output: Path,
    hold_frames: int,
    fps: int,
    columns: int,
) -> None:
    # Format vidéo 16:9 (YouTube), en pixels, indépendant du nombre de
    # lignes : plus le texte est long, plus chaque ligne est tassée (police
    # plus petite), mais l'image elle-même garde toujours ce même format.
    frame_width, frame_height = 1600, 900
    margin_x = 90
    text_top = 150

    all_rows = [layout_rows(point["doc_state"], columns) for point in timeline]
    total_rows = max((len(rows) for rows in all_rows), default=1) or 1

    font_path = find_monospace_font()
    available_w = frame_width - 2 * margin_x
    available_h = frame_height - text_top - 40
    char_width = available_w / columns
    # Pas de plancher ici : on connaît la taille maximale (total_rows, sur
    # la version la plus longue), donc on calcule la police pour que TOUT le
    # texte rentre dans le cadre, même si ça doit être tout petit.
    row_height = min(30.0, available_h / total_rows)
    font = fit_monospace_font(font_path, char_width, size_hint=max(int(row_height), 4))
    # La police à chasse fixe peut donner un caractère un peu plus étroit ou
    # large que prévu selon sa taille entière la plus proche : on réajuste
    # la largeur de colonne sur la vraie mesure de la police choisie.
    char_width = font.getlength("M") or char_width
    header_font = ImageFont.truetype(font_path, 34)
    sub_font = ImageFont.truetype(font_path, 18)

    # Précalculé une bonne fois pour toutes : évite de refaire le rendu à
    # chaque image pendant les `hold_frames` images où un même commit reste
    # affiché.
    all_frames = [
        render_frame_image(
            point, rows, frame_width, frame_height, margin_x, text_top,
            char_width, row_height, font, header_font, sub_font,
        )
        for point, rows in zip(timeline, all_rows)
    ]

    fig = plt.figure(figsize=(frame_width / 100, frame_height / 100), facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    im = ax.imshow(all_frames[0])

    total_frames = len(timeline) * hold_frames

    def frame_to_commit(frame: int) -> int:
        return min(frame // hold_frames, len(timeline) - 1)

    def update(frame: int):
        im.set_data(all_frames[frame_to_commit(frame)])
        return (im,)

    anim = FuncAnimation(fig, update, frames=total_frames, interval=1000 / fps, blit=False)

    output = Path(output)
    if output.suffix.lower() == ".gif":
        anim.save(output, writer=PillowWriter(fps=fps), dpi=100)
    else:
        anim.save(output, fps=fps, dpi=100)
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
        columns=args.columns,
    )
    print(f"Animation écrite dans {output}")


if __name__ == "__main__":
    main()
