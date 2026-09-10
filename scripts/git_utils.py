"""Petits wrappers Git, sans dépendance à GitPython."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Iterable


class GitError(RuntimeError):
    pass


def _run(repo: Path, args: list[str], *, text: bool = True) -> str | bytes:
    command = ["git", "-C", str(repo), *args]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=text)
    except FileNotFoundError as exc:
        raise GitError("Git est introuvable. Installez Git puis relancez l'analyse.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace").strip()
        raise GitError(f"La commande {' '.join(command)} a échoué : {stderr}") from exc
    return result.stdout


def ensure_repository(repo: Path) -> None:
    worktree = str(_run(repo, ["rev-parse", "--is-inside-work-tree"])).strip()
    bare = str(_run(repo, ["rev-parse", "--is-bare-repository"])).strip()
    if worktree != "true" and bare != "true":
        raise GitError(f"{repo} n'est pas un dépôt Git.")


@dataclass(frozen=True)
class Commit:
    sha: str
    timestamp: str


@dataclass(frozen=True)
class Change:
    status: str
    old_path: str | None
    new_path: str | None
    old_oid: str | None = None
    new_oid: str | None = None


def list_commits(repo: Path) -> list[Commit]:
    """Retourne tous les commits dans un ordre topologique stable, racines d'abord."""
    raw = str(
        _run(
            repo,
            ["log", "--all", "--reverse", "--topo-order", "--format=%H%x00%cI"],
        )
    )
    commits: list[Commit] = []
    for line in raw.splitlines():
        if not line:
            continue
        sha, timestamp = line.split("\0", 1)
        commits.append(Commit(sha=sha, timestamp=timestamp))
    return commits


def list_commits_after(repo: Path, sha: str) -> list[Commit]:
    """Retourne seulement les commits qui ne sont pas atteignables depuis sha."""
    raw = str(
        _run(
            repo,
            ["log", "--all", "--reverse", "--topo-order", "--format=%H%x00%cI", f"{sha}.."],
        )
    )
    commits: list[Commit] = []
    for line in raw.splitlines():
        if not line:
            continue
        commit_sha, timestamp = line.split("\0", 1)
        commits.append(Commit(sha=commit_sha, timestamp=timestamp))
    return commits


def commit_timestamp(repo: Path, sha: str) -> str:
    return str(_run(repo, ["show", "-s", "--format=%cI", sha])).strip()


def list_paths(repo: Path, ref: str = "HEAD") -> list[str]:
    """Liste les fichiers d'un instantané Git sans dépendre du worktree."""
    raw = bytes(_run(repo, ["ls-tree", "-r", "-z", "--name-only", ref], text=False))
    return [path for path in raw.decode("utf-8", errors="surrogateescape").split("\0") if path]


def commit_exists(repo: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    return result.returncode == 0


def changed_paths(repo: Path, sha: str) -> list[Change]:
    """Compare un commit à son premier parent avec les identifiants de blobs Git."""
    parents = str(_run(repo, ["rev-list", "--parents", "-n", "1", sha])).strip().split()
    if len(parents) > 1:
        args = ["diff-tree", "-r", "--no-renames", "--raw", "--abbrev=40", "-z", parents[1], sha]
    else:
        args = ["diff-tree", "--root", "-r", "--no-renames", "--no-commit-id", "--raw", "--abbrev=40", "-z", sha]
    raw = bytes(_run(repo, args, text=False))
    fields = raw.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        header = fields[index]
        index += 1
        metadata = header.split()
        if len(metadata) < 5 or not metadata[0].startswith(":"):
            raise GitError(f"Entrée diff-tree inattendue : {header!r}")
        old_oid = metadata[2]
        new_oid = metadata[3]
        code = metadata[4][:1]
        old_oid = None if set(old_oid) == {"0"} else old_oid
        new_oid = None if set(new_oid) == {"0"} else new_oid
        if code in {"R", "C"}:
            old_path, new_path = fields[index], fields[index + 1]
            index += 2
            changes.append(Change(code, old_path, new_path, old_oid, new_oid))
        else:
            path = fields[index]
            index += 1
            changes.append(
                Change(
                    code,
                    path if code != "A" else None,
                    None if code == "D" else path,
                    old_oid,
                    new_oid,
                )
            )
    return changes


def changed_paths_batch(
    repo: Path,
    shas: list[str],
    *,
    detect_renames: bool = False,
) -> dict[str, list[Change]]:
    """Lit les changements de nombreux commits avec un seul processus Git."""
    result = {sha: [] for sha in shas}
    if not shas:
        return result
    rename_option = "-M" if detect_renames else "--no-renames"
    command = [
        "git", "-C", str(repo), "diff-tree", "--stdin", "--root", "--first-parent",
        "-r", rename_option, "--raw", "--abbrev=40", "-z",
    ]
    try:
        completed = subprocess.run(
            command,
            input=("\n".join(shas) + "\n").encode("ascii"),
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise GitError(f"La commande {' '.join(command)} a échoué : {stderr}") from exc

    fields = completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    wanted = set(shas)
    current: str | None = None
    index = 0
    while index < len(fields):
        token = fields[index].lstrip("\n")
        index += 1
        if not token:
            continue
        if token in wanted:
            current = token
            continue
        metadata = token.split()
        if current is None or len(metadata) < 5 or not metadata[0].startswith(":"):
            raise GitError(f"Entrée diff-tree groupée inattendue : {token!r}")
        old_oid = None if set(metadata[2]) == {"0"} else metadata[2]
        new_oid = None if set(metadata[3]) == {"0"} else metadata[3]
        code = metadata[4][:1]
        if code in {"R", "C"}:
            old_path, new_path = fields[index], fields[index + 1]
            index += 2
            result[current].append(Change(code, old_path, new_path, old_oid, new_oid))
        else:
            path = fields[index]
            index += 1
            result[current].append(Change(
                code,
                path if code != "A" else None,
                None if code == "D" else path,
                old_oid,
                new_oid,
            ))
    return result


class BlobReader:
    """Lecteur persistant de `git cat-file --batch`, bien plus rapide que git show."""

    def __init__(self, repo: Path):
        self.process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def text(self, sha: str, path: str) -> str | None:
        assert self.process.stdin is not None and self.process.stdout is not None
        spec = f"{sha}:{path}\n".encode("utf-8", errors="surrogateescape")
        self.process.stdin.write(spec)
        self.process.stdin.flush()
        header = self.process.stdout.readline().decode("ascii", errors="replace").strip()
        if header.endswith(" missing"):
            return None
        try:
            size = int(header.rsplit(" ", 1)[1])
        except (IndexError, ValueError) as exc:
            raise GitError(f"Réponse inattendue de git cat-file : {header}") from exc
        payload = self.process.stdout.read(size)
        self.process.stdout.read(1)  # saut de ligne ajouté par --batch
        return payload.decode("utf-8", errors="replace")

    def text_length(self, sha: str, path: str) -> int | None:
        content = self.text(sha, path)
        return len(content) if content is not None else None

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        self.process.wait(timeout=10)

    def __enter__(self) -> "BlobReader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
