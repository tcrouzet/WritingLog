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

# Surlignage façon stabilo selon le nombre de fois où un mot a été réécrit :
# 0 fois -> pas de surlignage (texte noir simple), 1 -> jaune, 2 -> vert,
# 3 et plus -> rouge. Le texte lui-même reste toujours noir, bien plus
# visible qu'un texte coloré sur fond blanc.
HIGHLIGHT_COLORS = [None, "#ffe066", "#63e6be", "#ff8787"]


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

def layout_rows(
    doc_state: list[dict[str, Any]],
    font: "ImageFont.FreeTypeFont",
    column_width_px: float,
) -> list[list[tuple[float, str, str | None]]]:
    """Transforme la liste de mots en lignes de page (retour à la ligne
    automatique, paragraphes séparés par une ligne vide). Chaque ligne est
    une liste de (x en pixels depuis le début de colonne, mot, couleur de
    surlignage ou None).

    Important : le retour à la ligne se calcule avec la largeur RÉELLE en
    pixels de chaque mot (`font.getlength`), pas en comptant les
    caractères. Un simple compte de caractères suppose que tous les
    glyphes font exactement la même largeur que "M" ; or des caractères
    français (accents, « », —, ’, …) peuvent être rendus avec une police
    de secours légèrement différente, ce qui décale le calcul et fait
    déborder certains mots sur la colonne voisine.

    Cas particulier géré explicitement : un "mot" (au sens de WORD_RE, donc
    sans espace) peut très bien être plus large à lui seul que la colonne
    entière — un lien markdown collé `[texte](http://...)`, une très longue
    URL, un mot à rallonge avec tirets. Dans ce cas on le découpe lettre par
    lettre (toujours en largeur réelle en pixels) pour qu'il ne déborde
    jamais, même posé en tout début de ligne."""
    rows: list[list[tuple[float, str, str | None]]] = []
    current: list[tuple[float, str, str | None]] = []
    cursor_px = 0.0
    space_w = font.getlength(" ") or (font.size * 0.5)

    def split_oversized(token: str) -> list[str]:
        pieces: list[str] = []
        piece = ""
        for ch in token:
            candidate = piece + ch
            if piece and font.getlength(candidate) > column_width_px:
                pieces.append(piece)
                piece = ch
            else:
                piece = candidate
        if piece:
            pieces.append(piece)
        return pieces

    def place(token: str, highlight: str | None, force_break_after: bool) -> None:
        nonlocal current, cursor_px
        token_w = font.getlength(token)
        needed = token_w + (space_w if current else 0.0)
        if current and cursor_px + needed > column_width_px:
            rows.append(current)
            current = []
            cursor_px = 0.0
        if current:
            cursor_px += space_w  # espace avant le mot
        current.append((cursor_px, token, highlight))
        cursor_px += token_w
        if force_break_after:
            rows.append(current)
            current = []
            cursor_px = 0.0

    for entry in doc_state:
        if entry["text"] == PARAGRAPH_BREAK:
            rows.append(current)
            rows.append([])
            current = []
            cursor_px = 0.0
            continue
        word = entry["text"]
        highlight = HIGHLIGHT_COLORS[min(entry["edits"], len(HIGHLIGHT_COLORS) - 1)]
        word_w = font.getlength(word)
        if word_w > column_width_px:
            pieces = split_oversized(word)
            for index, piece in enumerate(pieces):
                place(piece, highlight, force_break_after=index < len(pieces) - 1)
        else:
            place(word, highlight, force_break_after=False)
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
    rows: list[list[tuple[float, str, str | None]]],
    frame_width: int,
    frame_height: int,
    margin_x: int,
    text_top: int,
    row_height: float,
    font: "ImageFont.FreeTypeFont",
    header_font: "ImageFont.FreeTypeFont",
    sub_font: "ImageFont.FreeTypeFont",
    physical_columns: int,
    column_width_px: float,
    column_gap_px: float,
    rows_per_column: int,
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
        column = row_index // rows_per_column
        if column >= physical_columns:
            break  # texte plus long que l'espace total des 3 colonnes (ne devrait pas arriver)
        row_in_column = row_index % rows_per_column
        column_x = margin_x + column * (column_width_px + column_gap_px)
        y = text_top + row_in_column * row_height
        for col_start_px, word, highlight in row:
            x = column_x + col_start_px
            if highlight:
                word_w = font.getlength(word)
                draw.rectangle(
                    [x - 1, y + row_height * 0.08, x + word_w + 1, y + row_height * 0.92],
                    fill=highlight,
                )
            draw.text((x, y), word, font=font, fill="#111111")

    return np.asarray(image)


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

def render(
    timeline: list[dict[str, Any]],
    output: Path,
    hold_frames: int,
    fps: int,
) -> None:
    # Format vidéo 16:9 (YouTube), en pixels, indépendant du nombre de
    # lignes : plus le texte est long, plus chaque ligne est tassée (police
    # plus petite), mais l'image elle-même garde toujours ce même format.
    frame_width, frame_height = 1600, 900
    margin_x = 90
    text_top = 150

    font_path = find_monospace_font()
    available_w = frame_width - 2 * margin_x
    available_h = frame_height - text_top - 40
    line_spacing = 1.3
    final_doc_state = timeline[-1]["doc_state"]

    # Mise en page façon journal : 3 colonnes plutôt qu'une seule pleine
    # largeur, pour beaucoup moins de blanc et une police plus grande.
    physical_columns = 3
    column_gap_px = 50
    column_width_px = (available_w - (physical_columns - 1) * column_gap_px) / physical_columns

    # La taille de police et le nombre de lignes par colonne se déterminent
    # l'un l'autre : plus la police est petite, plus de texte tient par
    # ligne, donc moins de lignes par colonne sont nécessaires en hauteur,
    # donc la police pourrait être plus grande... On fait converger les
    # deux ensemble, sur la version la plus longue atteinte répartie sur
    # les 3 colonnes, pour utiliser toute la largeur ET toute la hauteur
    # sans jamais déborder. Le retour à la ligne lui-même se fait toujours
    # avec la largeur RÉELLE en pixels des mots (voir layout_rows), jamais
    # en comptant des caractères, pour ne jamais déborder sur la colonne
    # voisine à cause d'un glyphe plus large que prévu (accents, « »,
    # tirets, etc.).
    font_size = 24
    for _ in range(8):
        probe_font = ImageFont.truetype(font_path, font_size)
        total_rows_guess = len(layout_rows(final_doc_state, probe_font, column_width_px)) or 1
        rows_per_column_guess = max(1, -(-total_rows_guess // physical_columns))  # ceil
        needed_size = max(4, int((available_h / rows_per_column_guess) / line_spacing))
        if needed_size == font_size:
            break
        font_size = needed_size

    font = ImageFont.truetype(font_path, font_size)
    row_height = font_size * line_spacing

    all_rows = [layout_rows(point["doc_state"], font, column_width_px) for point in timeline]
    total_rows = max((len(rows) for rows in all_rows), default=1) or 1
    rows_per_column = max(1, -(-total_rows // physical_columns))  # ceil
    # Au cas où le nombre réel de lignes (sur tout l'historique, pas
    # seulement la version finale) dépasserait légèrement l'estimation : on
    # retasse une dernière fois l'espacement pour que ça rentre.
    row_height = min(row_height, available_h / rows_per_column)
    header_font = ImageFont.truetype(font_path, 34)
    sub_font = ImageFont.truetype(font_path, 18)

    # Précalculé une bonne fois pour toutes : évite de refaire le rendu à
    # chaque image pendant les `hold_frames` images où un même commit reste
    # affiché.
    all_frames = [
        render_frame_image(
            point, rows, frame_width, frame_height, margin_x, text_top,
            row_height, font, header_font, sub_font,
            physical_columns, column_width_px, column_gap_px, rows_per_column,
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
    )
    print(f"Animation écrite dans {output}")


if __name__ == "__main__":
    main()
