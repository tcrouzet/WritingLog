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
    `<output_dir>/animations/<nom-du-fichier>.gif`.

Mise en page verticale, pleine largeur, en trois blocs empilés :
  A) le titre qui défile (date, commit, +/- signes) ;
  B) le graphique de taille cumulée, avec un repère qui avance ;
  C) la feuille qui se remplit de haut en bas (comme on écrit) : sa hauteur
     suit la taille du texte, et chaque zone se colore selon qu'elle vient
     d'être modifiée, avec un dégradé façon Photoshop — bleu clair (calme)
     → vert → jaune → rouge (vient d'être touché). Par défaut les zones
     éditées restent colorées pour toujours (voir --decay-frames pour
     tester un estompage progressif).

Dépendances :
    pip install matplotlib pillow

Pour un export MP4 (plus léger, meilleure qualité), installez ffmpeg et
changez OUTPUT_FORMAT ci-dessous, ou passez --output animation.mp4.

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
from matplotlib.colors import LinearSegmentedColormap


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
    """Fichier de sortie par défaut : <output_dir>/animations/<nom>.gif,
    <output_dir> étant celui déclaré dans config.yaml (par défaut "site")."""
    if output_arg is not None:
        return output_arg
    output_dir = config_value(config_path, "output_dir", "site") or "site"
    stem = Path(file_path).stem
    target_dir = config_path.parent / output_dir / "animations"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{stem}.gif"


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
# Calcul des différences
# ---------------------------------------------------------------------------

# Mots, ponctuation et espaces comme unités de comparaison : bien plus fiable
# qu'une comparaison caractère par caractère sur de la prose réécrite, qui a
# tendance à "perdre le fil" sur des coïncidences de quelques lettres et à
# déclarer un remplacement géant là où il n'y a qu'une petite retouche.
TOKEN_RE = re.compile(r"\s+|\w+(?:['’]\w+)*|[^\w\s]", re.UNICODE)

# Une insertion contenant plus d'une phrase complète (ponctuation de fin de
# phrase) est considérée comme une vraie page d'écriture -> bleu. Une
# insertion plus courte (un mot, une précision, une seule phrase) -> vert.
# Peu importe où ça atterrit dans le document (même juste avant des notes en
# vrac laissées à la fin).
SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s|$)")


def count_sentences(segment: str) -> int:
    return len(SENTENCE_END_RE.findall(segment))


def _tokenize_with_offsets(text: str) -> tuple[list[str], list[int]]:
    """Découpe en tokens et donne, pour chaque indice de token, son offset
    en caractères dans le texte d'origine (pour replacer précisément chaque
    différence sur l'échelle 0..1 du document)."""
    tokens = TOKEN_RE.findall(text)
    offsets = [0] * (len(tokens) + 1)
    total = 0
    for index, token in enumerate(tokens):
        offsets[index] = total
        total += len(token)
    offsets[len(tokens)] = total
    return tokens, offsets


def diff_events(old_text: str, new_text: str) -> list[dict[str, Any]]:
    """Position relative (0..1) et ampleur de chaque ajout/suppression."""
    if old_text == new_text:
        return []
    old_len = max(len(old_text), 1)
    new_len = max(len(new_text), 1)
    old_tokens, old_offsets = _tokenize_with_offsets(old_text)
    new_tokens, new_offsets = _tokenize_with_offsets(new_text)
    matcher = SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    events: list[dict[str, Any]] = []

    def add_event(kind: str, start: int, end: int, length: int, category: str) -> None:
        amount = end - start
        if amount <= 0:
            return
        events.append({
            "type": kind,
            "rel": ((start + end) / 2) / length,
            "amount": amount,
            # "growth"  : prolongation, on continue d'écrire à la suite -> bleu
            # "insert"  : insertion de texte neuf au milieu de l'existant,
            #             rien n'est supprimé -> vert
            # "edit"    : remplacement ou suppression de texte déjà écrit -> rouge
            "category": category,
        })

    def classify_addition(start: int, end: int) -> str:
        return "growth" if count_sentences(new_text[start:end]) > 1 else "insert"

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            category = classify_addition(new_offsets[j1], new_offsets[j2])
            add_event("add", new_offsets[j1], new_offsets[j2], new_len, category)
        elif tag == "delete":
            # Une suppression touche forcément du texte déjà écrit : rouge.
            add_event("del", old_offsets[i1], old_offsets[i2], old_len, "edit")
        elif tag == "replace":
            old_count = i2 - i1
            new_count = j2 - j1
            if new_count > old_count:
                # Cas fréquent en écriture : on retouche la fin d'une phrase
                # tout en continuant derrière (ou en insérant avant des notes
                # en vrac). Git fusionne ça en un seul "replace" : on sépare
                # la part réellement réécrite (rouge, même longueur que
                # l'ancien texte remplacé) de la part excédentaire, classée
                # selon son nombre de phrases comme une vraie prolongation ou
                # un simple ajout ponctuel.
                split = j1 + old_count
                add_event("add", new_offsets[j1], new_offsets[split], new_len, "edit")
                category = classify_addition(new_offsets[split], new_offsets[j2])
                add_event("add", new_offsets[split], new_offsets[j2], new_len, category)
            else:
                # Un remplacement substitue des mots à d'autres : c'est une
                # édition, pas une insertion neutre -> rouge.
                add_event("add", new_offsets[j1], new_offsets[j2], new_len, "edit")
            add_event("del", old_offsets[i1], old_offsets[i2], old_len, "edit")
    return events


def build_timeline(repo: Path, file_path: str, max_commits: int | None) -> list[dict[str, Any]]:
    commits = commits_for_file(repo, file_path)
    if not commits:
        raise RuntimeError(
            f"Aucun commit trouvé pour {file_path!r}. Vérifiez --repo et --file."
        )
    if max_commits:
        commits = commits[-max_commits:]

    timeline: list[dict[str, Any]] = []
    prev_text = ""
    cum_added = 0
    cum_removed = 0
    for commit in commits:
        text = file_content_at(repo, commit)
        events = diff_events(prev_text, text)
        added = sum(e["amount"] for e in events if e["type"] == "add")
        removed = sum(e["amount"] for e in events if e["type"] == "del")
        cum_added += added
        cum_removed += removed
        timeline.append(
            {
                "sha": commit.sha[:8],
                "date": commit.date,
                "size": len(text),
                "events": events,
                "added": added,
                "removed": removed,
                "cum_added": cum_added,
                "cum_removed": cum_removed,
            }
        )
        prev_text = text
    return timeline


# ---------------------------------------------------------------------------
# Bandeau vertical "chaleur d'édition"
# ---------------------------------------------------------------------------

NUM_BINS = 160  # résolution verticale du bandeau (du début à la fin du texte)
BACKGROUND_RGB = (0.055, 0.067, 0.09)  # assorti au fond de la figure, pour le texte "pas encore écrit"

# Dégradé façon Photoshop : bleu clair (calme) -> vert -> jaune -> rouge (chaud).
HEAT_CMAP = LinearSegmentedColormap.from_list(
    "writing_heat", ["#04ebf9", "#00e676", "#ffee58", "#ff1744"]
)


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

def render(
    timeline: list[dict[str, Any]],
    output: Path,
    hold_frames: int,
    decay_frames: int,
    fps: int,
    dpi: int,
    title: str,
) -> None:
    max_size = max(point["size"] for point in timeline) or 1

    # Mise en page verticale, pleine largeur, en trois blocs empilés :
    # A) le titre qui défile (fig.suptitle, au-dessus de la grille) ;
    # B) le graphique de taille, pleine largeur ;
    # C) la feuille qui se remplit, pleine largeur.
    fig = plt.figure(figsize=(8, 10), facecolor="#0e1117")
    grid = fig.add_gridspec(2, 1, height_ratios=[1, 3], hspace=0.25, top=0.78, bottom=0.05, left=0.09, right=0.96)
    ax_size = fig.add_subplot(grid[0])
    ax_bar = fig.add_subplot(grid[1])

    for ax in (ax_bar, ax_size):
        ax.set_facecolor("#0e1117")

    # --- Bandeau vertical : le texte, de haut (début) en bas (fin) --------
    ax_bar.set_xlim(0, 1)
    ax_bar.set_ylim(1, 0)  # 0 = début du texte en haut, 1 = fin du texte en bas
    ax_bar.set_xticks([])
    ax_bar.set_yticks([])
    for spine in ax_bar.spines.values():
        spine.set_color("#44505f")
    bar_image = np.tile(np.array(BACKGROUND_RGB), (NUM_BINS, 1, 1))
    bar_im = ax_bar.imshow(
        bar_image, extent=[0, 1, 1, 0], aspect="auto", interpolation="nearest",
    )
    size_label = ax_bar.text(
        0.5, 1.04, "", transform=ax_bar.transAxes, ha="center", va="bottom",
        color="white", fontsize=11, fontweight="bold",
    )

    # --- Courbe de taille ------------------------------------------------
    dates = [point["date"] for point in timeline]
    sizes = [point["size"] for point in timeline]
    ax_size.plot(dates, sizes, color="#04ebf9", linewidth=1.4)
    ax_size.set_ylim(0, max_size * 1.1)
    ax_size.set_ylabel("signes", color="white", fontsize=9)
    ax_size.tick_params(colors="white", labelsize=8)
    for spine in ax_size.spines.values():
        spine.set_color("#44505f")
    size_cursor = ax_size.axvline(dates[0], color="white", linewidth=1, alpha=0.8)

    fig.text(0.5, 0.975, title, color="#8fa3bf", fontsize=10, ha="center")
    header_date = fig.text(
        0.5, 0.945, "", color="white", fontsize=22, fontweight="bold",
        ha="center", va="top",
    )
    header_delta = fig.text(
        0.5, 0.885, "", color="#cfd8e3", fontsize=12,
        ha="center", va="top",
    )

    heat = np.zeros(NUM_BINS)
    # decay_frames <= 0 : la chaleur reste indéfiniment (comportement par défaut).
    # decay_frames > 0 : elle retombe à ~2 % au bout de ce nombre d'images (à tester).
    decay_rate = 0.02 ** (1 / decay_frames) if decay_frames > 0 else 1.0
    total_frames = len(timeline) * hold_frames

    def frame_to_commit(frame: int) -> tuple[int, float]:
        commit_index = min(frame // hold_frames, len(timeline) - 1)
        sub = (frame % hold_frames) / hold_frames
        return commit_index, sub

    def update(frame: int):
        commit_index, sub = frame_to_commit(frame)
        point = timeline[commit_index]
        prev_size = timeline[commit_index - 1]["size"] if commit_index > 0 else 0

        # Nouvelles éditions en tout début du palier dédié à ce commit : on
        # réchauffe les cases du bandeau autour de la position touchée.
        #
        # Important : `event["rel"]` est une proportion de la longueur du
        # document AU MOMENT de ce commit (new_len pour un ajout, old_len
        # pour une suppression) — pas de la taille finale `max_size` sur
        # laquelle le bandeau est étalonné. Au tout début, quand le texte
        # est court, une position "vers la fin du texte à cet instant" doit
        # donc être replacée bien plus tôt sur le bandeau final : on
        # rééchelonne par la fraction déjà remplie à ce moment-là, sans quoi
        # la chaleur se retrouve plaquée trop loin et ne "révèle" son rouge
        # que plus tard, pile quand la page atteint cette profondeur.
        this_fill = min(point["size"] / max_size, 1.0)
        prev_fill = min(prev_size / max_size, 1.0)
        if frame % hold_frames == 0:
            for event in point["events"]:
                category = event.get("category", "edit")
                if category == "growth":
                    # Prolongation : texte neuf écrit à la suite, reste bleu.
                    continue
                scale = this_fill if event["type"] == "add" else prev_fill
                center = int(min(max(event["rel"] * scale, 0.0), 1.0) * (NUM_BINS - 1))
                radius = min(6, 1 + int(event["amount"] / 80))
                if category == "insert":
                    # Insertion de texte neuf au milieu de l'existant : vert.
                    intensity = min(0.55, 0.28 + event["amount"] / 500)
                else:
                    # Remplacement / suppression de texte déjà écrit : rouge.
                    intensity = min(1.0, 0.6 + event["amount"] / 300)
                lo, hi = max(0, center - radius), min(NUM_BINS, center + radius + 1)
                heat[lo:hi] = np.maximum(heat[lo:hi], intensity)

        # Chaque image, la chaleur retombe doucement vers le bleu calme
        # (seulement si decay_frames > 0 ; sinon decay_rate == 1.0, donc
        # aucune perte : les zones éditées restent colorées durablement).
        # (slice-assignment, pas de réassignation du nom : évite que Python
        # ne traite `heat` comme une variable locale non initialisée)
        heat[:] = heat * decay_rate

        # Remplissage interpolé du bandeau entre deux commits.
        interpolated_size = prev_size + (point["size"] - prev_size) * sub
        fill_fraction = min(interpolated_size / max_size, 1.0)
        written_bins = int(round(fill_fraction * NUM_BINS))

        colors = np.tile(np.array(BACKGROUND_RGB), (NUM_BINS, 1))
        if written_bins > 0:
            colors[:written_bins] = HEAT_CMAP(heat[:written_bins])[:, :3]
        bar_im.set_data(colors.reshape(NUM_BINS, 1, 3))

        size_label.set_text(f"{int(interpolated_size):,}".replace(",", " ") + " signes")
        size_cursor.set_xdata([point["date"], point["date"]])

        header_date.set_text(point["date"].strftime("%Y-%m-%d %H:%M"))
        header_delta.set_text(
            f"+{int(point['added'])} / -{int(point['removed'])} signes "
            f"(total ajouté {int(point['cum_added'])}, supprimé {int(point['cum_removed'])})"
        )
        return bar_im, size_label, size_cursor, header_date, header_delta

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
    parser.add_argument(
        "--decay-frames", type=int, default=0,
        help="0 (défaut) : les zones éditées restent colorées pour toujours. "
        "N > 0 : elles retombent au bleu calme au bout de N images (à tester).",
    )
    parser.add_argument("--fps", type=int, default=20, help="Images par seconde de la vidéo finale")
    parser.add_argument("--dpi", type=int, default=120)
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
        decay_frames=args.decay_frames,
        fps=args.fps,
        dpi=args.dpi,
        title=file_path,
    )
    print(f"Animation écrite dans {output}")


if __name__ == "__main__":
    main()
