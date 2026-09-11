from __future__ import annotations

import json
import io
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr


ROOT = Path(__file__).resolve().parents[1]
ANALYZER = ROOT / "scripts" / "analyze_vault.py"
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_vault import (
    aggregate,
    character_changes,
    classification_blocks,
)


class AnalyzerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "config.yaml").write_text(
            """vault_path: vault
excluded_folders: [journal]
file_extensions: [.md]
internal_detection:
  gram_chars: 36
  selection_chars: 180
  overlap_threshold: 0.85
output_dir: site
""",
            encoding="utf-8",
        )
        (self.root / "projet.yml").write_text(
            "Alpha:\n  title: Premier projet\n  genre: roman\n  folder: manuscrit\n  history_folders: [Alpha/ancienne-version]\n  date_debut: 2025-01-10\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str, env: dict[str, str] | None = None) -> None:
        subprocess.run(["git", "-C", str(self.vault), *args], check=True, capture_output=True, env=env)

    def commit(self, message: str, timestamp: str) -> None:
        self.git("add", "-A")
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp})
        self.git("commit", "-q", "-m", message, env=env)

    def analyze(self, *args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(ANALYZER), "--config", str(self.root / "config.yaml"), *args],
            text=True,
            capture_output=True,
        )
        if expect_success and result.returncode:
            self.fail(result.stderr or result.stdout)
        return result

    def load(self, name: str):
        return json.loads((self.root / "site" / "data" / name).read_text(encoding="utf-8"))

    @staticmethod
    def text(length: int, seed: int = 1) -> str:
        randomizer = random.Random(seed)
        alphabet = "abcdefghijklmnopqrstuvwxyz     ,.;!?"
        return "".join(randomizer.choice(alphabet) for _ in range(length))

    def test_full_history_import_sessions_move_delete_and_incremental(self) -> None:
        alpha = self.vault / "Alpha"
        alpha.mkdir()
        manuscript = alpha / "Manuscrit"
        manuscript.mkdir()
        note = manuscript / "note.md"
        manuscript_text = self.text(2100)
        note.write_text(manuscript_text[:100], encoding="utf-8")
        (alpha / "bible.md").write_text("x" * 5000, encoding="utf-8")
        journal = self.vault / "Journal"
        journal.mkdir()
        (journal / "ignored.md").write_text("x" * 5000, encoding="utf-8")
        self.commit("create", "2026-01-01T10:00:00+01:00")
        note.write_text(manuscript_text[:600], encoding="utf-8")
        self.commit("write", "2026-01-01T10:15:00+01:00")
        note.write_text(manuscript_text, encoding="utf-8")
        self.commit("import", "2026-01-01T10:30:00+01:00")

        beta = self.vault / "Beta"
        beta.mkdir()
        note.rename(beta / "note.md")
        (manuscript / "current.md").write_text("c" * 10, encoding="utf-8")
        self.commit("move", "2026-01-01T10:40:00+01:00")
        (beta / "note.md").unlink()
        self.commit("delete", "2026-01-01T10:50:00+01:00")

        self.analyze("full")
        projects = {item["id"]: item for item in self.load("projects.json")}
        self.assertEqual(projects["Alpha"]["signes_reels_total"], 2110)
        self.assertNotIn("signes_importes_total", projects["Alpha"])
        self.assertNotIn("deplacements_internes_total", projects["Alpha"])
        self.assertEqual(projects["Alpha"]["taille_actuelle"], 10)
        self.assertEqual(projects["Alpha"]["genre"], "roman")
        self.assertEqual(projects["Alpha"]["date_debut"], "2025-01-10")
        self.assertNotIn("temps_minutes_total", projects["Alpha"])
        self.assertNotIn("Beta", projects)
        self.assertNotIn("Journal", projects)
        alpha_curve = [row for row in self.load("size_evolution.json") if row["projet"] == "Alpha"]
        # Tous les commits sont du même jour : la courbe conserve la dernière
        # taille physique, indépendamment des signes classés comme produits.
        self.assertEqual(alpha_curve[-1]["taille_signes"], 10)
        state = self.load("state.json")
        self.assertEqual(
            sum(event["import_chars"] for event in state["events"] if event["project"] == "Alpha"),
            0,
        )
        self.assertEqual(len(state["files"]), 1)

        fresh = beta / "fresh.md"
        duplicated_text = "un deux trois quatre cinq six sept huit neuf dix"
        fresh.write_text(duplicated_text, encoding="utf-8")
        self.commit("new writing", "2026-01-02T10:00:00+01:00")
        result = self.analyze()
        self.assertIn("1 nouveau", result.stdout)
        projects = {item["id"]: item for item in self.load("projects.json")}
        self.assertEqual(projects["Beta"]["signes_reels_total"], len(duplicated_text))
        self.assertEqual(projects["Beta"]["taille_actuelle"], len(duplicated_text))
        self.assertNotIn("temps_minutes_total", projects["Beta"])

        reused = manuscript / "reused.md"
        reused.write_text(duplicated_text, encoding="utf-8")
        self.commit("reuse existing paragraph", "2026-01-02T10:05:00+01:00")
        self.analyze("incremental")
        projects = {item["id"]: item for item in self.load("projects.json")}
        self.assertEqual(projects["Alpha"]["signes_reels_total"], 2110)

        state = self.load("state.json")
        self.assertEqual(
            sum(event["internal_chars"] for event in state["events"] if event["project"] == "Alpha"),
            len(duplicated_text),
        )
        self.assertIn("Beta/fresh.md", json.dumps(state))
        result = self.analyze()
        self.assertIn("0 nouveau", result.stdout)

        archived_beta = self.vault / "Archives" / "Beta"
        archived_beta.mkdir(parents=True)
        fresh.rename(archived_beta / "fresh.md")
        self.commit("archive Beta", "2026-01-02T10:15:00+01:00")
        self.analyze()
        candidates = (self.root / "projets_archives.yml").read_text(encoding="utf-8")
        self.assertIn("Beta:", candidates)
        self.assertIn("Archives/Beta", candidates)
        self.assertNotIn("Beta", {item["id"] for item in self.load("projects.json")})

        with (self.root / "projet.yml").open("a", encoding="utf-8") as handle:
            handle.write("Beta:\n  title: Projet archivé\n  folder: Archives/Beta\n")
        self.analyze("full")
        projects = {item["id"]: item for item in self.load("projects.json")}
        self.assertEqual(projects["Beta"]["title"], "Projet archivé")
        self.assertEqual(projects["Beta"]["taille_actuelle"], len(duplicated_text))

    def test_writing_in_an_old_project_location_is_credited(self) -> None:
        old_folder = self.vault / "Alpha" / "ancienne-version"
        old_folder.mkdir(parents=True)
        passage = " ".join(f"mot{index}" for index in range(80))
        old_file = old_folder / "chapitre.md"
        old_file.write_text(passage, encoding="utf-8")
        self.commit("write in old location", "2026-01-01T10:00:00+01:00")

        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir()
        old_file.rename(manuscript / "chapitre.md")
        self.commit("move into current location", "2026-01-02T10:15:00+01:00")

        self.analyze("full")
        projects = {item["id"]: item for item in self.load("projects.json")}
        self.assertEqual(projects["Alpha"]["signes_reels_total"], len(passage))
        self.assertEqual(projects["Alpha"]["taille_actuelle"], len(passage))
        curve = [
            row["taille_signes"]
            for row in self.load("size_evolution.json")
            if row["projet"] == "Alpha"
        ]
        self.assertEqual(curve, [len(passage), len(passage)])

    def test_word_edit_counts_only_the_changed_characters(self) -> None:
        source = "un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze"
        edited = source.replace("huit", "HUIT-CORRIGE")
        added, removed = character_changes(source, edited)
        # La casse seule conserve l'identité du mot ; seul le suffixe est neuf.
        self.assertEqual("".join(added), "-CORRIGE")
        self.assertEqual("".join(removed), "")

    def test_distant_edits_in_a_large_manuscript_stay_local(self) -> None:
        lines = [(f"Paragraphe {index} " + "texte " * 80 + "\n") for index in range(100)]
        old = "".join(lines)
        changed = lines.copy()
        changed[10] = changed[10].replace("texte", "AJOUT", 1)
        changed[90] = changed[90].replace("texte", "CORRECTION", 1)
        added, removed = character_changes(old, "".join(changed))
        self.assertEqual(sum(map(len, added)), len("AJOUT") + len("CORRECTION"))
        self.assertEqual(sum(map(len, removed)), 2 * len("texte"))

    def test_sparse_commits_are_distributed_without_tracking_time(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        long_file = manuscript / "long.md"
        text = self.text(6001)
        long_file.write_text(text[:1], encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        long_file.write_text(text, encoding="utf-8")
        self.commit("large but progressive writing", "2026-01-05T10:00:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], 6001)
        self.assertNotIn("temps_minutes_total", project)
        self.assertTrue(all("temps_minutes" not in row for row in self.load("daily.json")))

    def test_sparse_commit_keeps_folders_without_time_fields(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        note = manuscript / "chapter.md"
        text = self.text(1601)
        note.write_text(text[:1], encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        note.write_text(text[:401], encoding="utf-8")
        self.commit("observed writing", "2026-01-01T10:10:00+01:00")
        note.write_text(text, encoding="utf-8")
        self.commit("later accumulated writing", "2026-01-03T10:10:00+01:00")

        self.analyze("full")
        rows = {
            row["periode"]: row
            for row in self.load("daily.json")
            if row["projet"] == "Alpha"
        }
        self.assertNotIn("temps_minutes", rows["2026-01-03"])
        self.assertNotIn("temps_estime", rows["2026-01-03"])
        self.assertEqual(rows["2026-01-03"]["dossiers"], ["Alpha/manuscrit"])

    def test_commit_spacing_never_reclassifies_new_text_as_import(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        note = manuscript / "chapter.md"
        text = self.text(2011)
        note.write_text(text[:1], encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        note.write_text(text[:11], encoding="utf-8")
        self.commit("slow observed edit", "2026-01-01T10:10:00+01:00")
        note.write_text(text, encoding="utf-8")
        self.commit("large accumulated edit", "2026-01-02T12:10:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], 2000)
        self.assertEqual(event["import_chars"], 0)
        self.assertNotIn("rate_chars_per_minute", event)

    def test_filled_new_file_remains_production_across_a_commit_gap(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        (manuscript / "start.md").write_text("début", encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")

        written = "texte original " * 250
        (manuscript / "new-chapter.md").write_text(written, encoding="utf-8")
        self.commit("several days of writing", "2026-01-05T10:00:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] != state["events"][0]["commit"])
        self.assertEqual(event["interval_start"], "2026-01-01T10:00:00+01:00")
        self.assertEqual(event["real_chars"], len(written))
        self.assertEqual(event["import_chars"], 0)
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], len("début") + len(written))

    def test_fast_new_file_is_production_when_its_hashes_are_new(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        observed = self.text(401, seed=10)
        note = manuscript / "start.md"
        note.write_text(observed[:1], encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        note.write_text(observed, encoding="utf-8")
        self.commit("observed writing", "2026-01-01T10:10:00+01:00")

        imported = self.text(4000, seed=11)
        (manuscript / "import.md").write_text(imported, encoding="utf-8")
        self.commit("large new file", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], len(imported))
        self.assertEqual(event["import_chars"], 0)

    def test_recent_unrelated_commit_does_not_turn_new_text_into_import(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        observed = self.text(401, seed=20)
        note = manuscript / "start.md"
        note.write_text(observed[:1], encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        note.write_text(observed, encoding="utf-8")
        self.commit("observed writing", "2026-01-01T10:10:00+01:00")

        unrelated = self.vault / "Beta"
        unrelated.mkdir()
        (unrelated / "note.md").write_text("petite note", encoding="utf-8")
        self.commit("unrelated recent commit", "2026-01-05T09:55:00+01:00")

        imported = self.text(4000, seed=21)
        (manuscript / "import.md").write_text(imported, encoding="utf-8")
        self.commit("large new file", "2026-01-05T10:00:00+01:00")
        self.analyze("full")

        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], len(imported))
        self.assertEqual(event["import_chars"], 0)

    def test_incremental_ignores_legacy_state_version(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        (manuscript / "start.md").write_text("début", encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        written = "texte original " * 250
        (manuscript / "new-chapter.md").write_text(written, encoding="utf-8")
        self.commit("several days of writing", "2026-01-05T10:00:00+01:00")
        self.analyze("full")

        rows = {
            row["periode"]: row["signes_reels"]
            for row in self.load("daily.json")
            if row["projet"] == "Alpha"
        }
        quotient, remainder = divmod(len(written), 4)
        self.assertEqual(rows["2026-01-02"], quotient)
        self.assertEqual(rows["2026-01-03"], quotient)
        self.assertEqual(rows["2026-01-04"], quotient + (1 if remainder >= 2 else 0))
        self.assertEqual(rows["2026-01-05"], quotient + (1 if remainder >= 1 else 0))

        state_path = self.root / "site" / "data" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["version"] = 13
        state_path.write_text(json.dumps(state), encoding="utf-8")

        result = self.analyze()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("version", self.load("state.json"))

    def test_disappeared_fingerprints_remain_indexed(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        (manuscript / "chapter.md").write_text("texte durable", encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")

        temporary = manuscript / "compiled-temporary.md"
        temporary_text = self.text(1000, seed=30)
        temporary.write_text(temporary_text, encoding="utf-8")
        self.commit("temporary text", "2026-01-02T10:15:00+01:00")
        temporary.unlink()
        self.commit("remove text", "2026-01-02T10:30:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], len("texte durable") + len(temporary_text))
        self.assertEqual(project["signes_supprimes_total"], 0)
        database = sqlite3.connect(self.root / ".cache" / "fingerprints.sqlite3")
        try:
            count = database.execute(
                "SELECT COUNT(*) FROM fingerprints "
                "WHERE file = ? AND removed_at_commit IS NOT NULL",
                ("Alpha/manuscrit/compiled-temporary.md",),
            ).fetchone()[0]
        finally:
            database.close()
        self.assertGreater(count, 0)

        restored = manuscript / "restored.md"
        restored.write_text(temporary_text, encoding="utf-8")
        self.commit("restore old text elsewhere", "2026-01-03T10:30:00+01:00")
        self.analyze("incremental")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], 0)
        self.assertEqual(event["internal_chars"], len(temporary_text))
        self.assertEqual(
            event["duplication_sources"][0]["origins"][0]["file"],
            "Alpha/manuscrit/compiled-temporary.md",
        )

    def test_true_editorial_deletions_are_exported(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        note = manuscript / "chapter.md"
        note.write_text("abcdef", encoding="utf-8")
        self.commit("start", "2026-01-01T10:00:00+01:00")
        note.write_text("abXY", encoding="utf-8")
        self.commit("cut and rewrite", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_supprimes_total"], 4)
        row = next(item for item in self.load("daily.json") if item["projet"] == "Alpha")
        self.assertEqual(row["signes_supprimes"], 4)

    def test_deletion_already_present_in_another_file_is_excluded(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        passage = self.text(1200, seed=40)
        source = manuscript / "chapter.md"
        source.write_text(f"Début.\n{passage}\nFin.", encoding="utf-8")
        (manuscript / "copy.md").write_text(passage, encoding="utf-8")
        self.commit("two occurrences", "2026-01-01T10:00:00+01:00")
        source.write_text("Début.\nFin.", encoding="utf-8")
        self.commit("remove copied passage", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_supprimes_total"], 0)

    def test_duplicate_added_in_same_commit_counts_only_once(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        passage = self.text(1200, seed=43)
        (manuscript / "a.md").write_text(passage, encoding="utf-8")
        (manuscript / "b.md").write_text(passage, encoding="utf-8")
        self.commit("two simultaneous copies", "2026-01-01T10:00:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], len(passage))
        state = self.load("state.json")
        self.assertEqual(
            sum(event["internal_chars"] for event in state["events"]),
            len(passage),
        )

    def test_duplicate_in_same_file_is_neither_produced_twice_nor_deleted(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        passage = self.text(1200, seed=44)
        note = manuscript / "chapter.md"
        note.write_text(f"{passage}\n\n{passage}", encoding="utf-8")
        self.commit("duplicate paragraph", "2026-01-01T10:00:00+01:00")
        note.write_text(passage, encoding="utf-8")
        self.commit("remove duplicate paragraph", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], len(passage))
        self.assertEqual(project["signes_supprimes_total"], 0)

    def test_future_reappearance_retroactively_cancels_a_deletion(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        passage = self.text(1200, seed=41)
        source = manuscript / "chapter.md"
        source.write_text(f"Début.\n{passage}\nFin.", encoding="utf-8")
        self.commit("original passage", "2026-01-01T10:00:00+01:00")
        source.write_text("Début.\nFin.", encoding="utf-8")
        self.commit("passage disappears", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertGreater(project["signes_supprimes_total"], 0)
        state = self.load("state.json")
        self.assertNotIn("hashes", state["deletion_candidates"][0])
        database = sqlite3.connect(self.root / ".cache" / "fingerprints.sqlite3")
        try:
            stored_hashes = database.execute(
                "SELECT COUNT(*) FROM deletion_candidate_hashes"
            ).fetchone()[0]
        finally:
            database.close()
        self.assertGreater(stored_hashes, 0)

        (manuscript / "restored.md").write_text(passage, encoding="utf-8")
        self.commit("passage reappears elsewhere", "2026-01-02T10:00:00+01:00")
        self.analyze("incremental")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_supprimes_total"], 0)

    def test_past_occurrence_in_another_file_excludes_a_later_deletion(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        passage = self.text(1200, seed=45)
        first = manuscript / "first.md"
        second = manuscript / "second.md"
        first.write_text(passage, encoding="utf-8")
        self.commit("first occurrence", "2026-01-01T10:00:00+01:00")
        second.write_text(passage, encoding="utf-8")
        self.commit("copied elsewhere", "2026-01-01T10:15:00+01:00")
        second.unlink()
        self.commit("old copy disappears", "2026-01-01T10:30:00+01:00")
        first.write_text("", encoding="utf-8")
        self.commit("original disappears later", "2026-01-01T10:45:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_supprimes_total"], 0)

    def test_source_file_history_does_not_hide_a_true_deletion(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        passage = self.text(1200, seed=46)
        source = manuscript / "chapter.md"
        source.write_text(passage, encoding="utf-8")
        self.commit("original passage", "2026-01-01T10:00:00+01:00")
        source.write_text("", encoding="utf-8")
        self.commit("true deletion", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertGreater(project["signes_supprimes_total"], 0)

    def test_size_evolution_is_physical_and_can_decrease(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        note = manuscript / "chapter.md"
        text = self.text(150, seed=42)
        note.write_text(text[:100], encoding="utf-8")
        self.commit("size 100", "2026-01-01T10:00:00+01:00")
        note.write_text(text, encoding="utf-8")
        self.commit("size 150", "2026-01-02T10:00:00+01:00")
        note.write_text(text[:120], encoding="utf-8")
        self.commit("size 120", "2026-01-03T10:00:00+01:00")

        self.analyze("full")
        sizes = [
            row["taille_signes"]
            for row in self.load("size_evolution.json")
            if row["projet"] == "Alpha"
        ]
        self.assertEqual(sizes, [100, 150, 120])

    def test_incoherent_deleted_total_emits_a_warning(self) -> None:
        state = {
            "last_commit": "commit-1",
            "project_sizes": {"Alpha": 1},
            "events": [{
                "timestamp": "2026-01-01T10:00:00+01:00",
                "interval_start": None,
                "commit": "commit-1",
                "project": "Alpha",
                "real_chars": 1,
                "import_chars": 0,
                "internal_chars": 0,
                "edit_delta": -2,
                "folders": ["Alpha/manuscrit"],
            }],
            "size_points": [],
        }
        error = io.StringIO()
        with redirect_stderr(error):
            aggregate(state, {}, {"Alpha": {}})
        self.assertIn("Avertissement : Alpha", error.getvalue())

    def test_edited_move_reuses_index_and_keeps_new_delta_as_production(self) -> None:
        old_folder = self.vault / "Alpha" / "ancienne-version"
        old_folder.mkdir(parents=True)
        source = " ".join(f"ancien{index}" for index in range(80))
        old_file = old_folder / "chapitre.md"
        old_file.write_text(source, encoding="utf-8")
        self.commit("old draft", "2026-01-01T10:00:00+01:00")

        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir()
        addition = " Voici le passage réellement écrit pendant la réorganisation."
        old_file.rename(manuscript / "chapitre-renomme.md")
        (manuscript / "chapitre-renomme.md").write_text(source + addition, encoding="utf-8")
        self.commit("move and continue writing", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], len(source) + len(addition))
        self.assertEqual(project["taille_actuelle"], len(source + addition))

    def test_compiled_file_from_existing_chapters_is_internal_duplication(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        chapter = "\n\n".join(
            " ".join(f"mot{paragraph}-{word}" for word in range(80))
            for paragraph in range(4)
        )
        source = manuscript / "chapter.md"
        source.write_text(chapter, encoding="utf-8")
        self.commit("write chapters", "2026-01-01T10:00:00+01:00")

        (manuscript / "compiled.md").write_text(chapter, encoding="utf-8")
        self.commit("compile manuscript", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], 0)
        self.assertEqual(event["internal_chars"], len(chapter))
        duplication = self.load("duplications.json")[-1]
        self.assertEqual(duplication["signes"], len(chapter))
        self.assertEqual(duplication["blocs"][0]["origins"][0]["file"], "Alpha/manuscrit/chapter.md")

    def test_compilation_with_small_unknown_margins_is_entirely_excluded(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        paragraphs = [
            " ".join(f"paragraphe-{paragraph}-mot-{word}" for word in range(100))
            for paragraph in range(10)
        ]
        source_text = "\n\n".join(paragraphs)
        (manuscript / "chapters.md").write_text(source_text, encoding="utf-8")
        self.commit("source chapters", "2026-01-01T10:00:00+01:00")

        changed = paragraphs.copy()
        changed[5] = self.text(len(changed[5]), seed=46)
        compiled = "\n\n".join(changed)
        (manuscript / "compiled.md").write_text(compiled, encoding="utf-8")
        self.commit("compiled manuscript with edited margin", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], 0)
        self.assertEqual(event["internal_chars"], len(compiled))

    def test_reappearing_compilation_with_heavy_edits_is_excluded(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        sentences = [
            " ".join(f"phrase{index}-mot{word}" for word in range(24)) + ". "
            for index in range(50)
        ]
        source_text = "".join(sentences)
        (manuscript / "chapters.md").write_text(source_text, encoding="utf-8")
        self.commit("source chapters", "2026-01-01T10:00:00+01:00")

        compiled = manuscript / "compiled.md"
        compiled.write_text(source_text, encoding="utf-8")
        self.commit("first compilation", "2026-01-01T10:15:00+01:00")
        compiled.unlink()
        self.commit("remove compilation", "2026-01-01T10:30:00+01:00")

        edited_compilation = "".join(sentences[:40]) + self.text(2000, seed=200)
        compiled.write_text(edited_compilation, encoding="utf-8")
        self.commit("recreate edited compilation", "2026-01-01T10:45:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertEqual(event["real_chars"], 0)
        self.assertEqual(event["internal_chars"], len(edited_compilation))

    def test_unique_compilation_is_reclassified_on_later_deletion(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        sentences = [
            " ".join(f"source{index}-mot{word}" for word in range(24)) + ". "
            for index in range(50)
        ]
        source_text = "".join(sentences)
        (manuscript / "chapters.md").write_text(source_text, encoding="utf-8")
        self.commit("source chapters", "2026-01-01T10:00:00+01:00")

        compilation = manuscript / "fusion-unique-1015.md"
        compiled_text = "".join(sentences[:40]) + self.text(2000, seed=201)
        compilation.write_text(compiled_text, encoding="utf-8")
        self.commit("temporary compilation", "2026-01-02T10:15:00+01:00")
        compilation.unlink()
        self.commit("delete temporary compilation", "2026-01-04T10:30:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], len(source_text))
        state = self.load("state.json")
        creation = next(event for event in state["events"] if event["commit"] != state["last_commit"] and event.get("temporary_compilations"))
        self.assertEqual(creation["real_chars"], 0)
        self.assertGreater(creation["temporary_compilations"][0]["reclassified_chars"], 0)
        sizes = [
            row["taille_signes"]
            for row in self.load("size_evolution.json")
            if row["projet"] == "Alpha"
        ]
        self.assertEqual(sizes, [len(source_text), len(source_text), len(source_text)])

    def test_edited_add_delete_pair_is_treated_as_a_rename(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        paragraphs = [
            " ".join(f"paragraphe{index}-mot{word}" for word in range(80))
            for index in range(20)
        ]
        old_text = "\n".join(paragraphs)
        old_file = manuscript / "99-temp.md"
        old_file.write_text(old_text, encoding="utf-8")
        self.commit("old path", "2026-01-01T10:00:00+01:00")

        edited = [paragraph + " corrigé" for paragraph in paragraphs]
        new_text = "\n".join(edited)
        old_file.unlink()
        (manuscript / "28-final.md").write_text(new_text, encoding="utf-8")
        self.commit("edited rename", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        state = self.load("state.json")
        event = next(item for item in state["events"] if item["commit"] == state["last_commit"])
        self.assertLess(event["real_chars"], len(new_text) // 10)
        self.assertLessEqual(event["real_chars"], len(" corrigé") * len(paragraphs))

    def test_classification_blocks_keep_text_but_limit_correction_scope(self) -> None:
        source = "".join(
            f"Voici la phrase numéro {index}, avec assez de texte pour être reconnue correctement. "
            for index in range(30)
        )
        blocks = classification_blocks([source])
        self.assertEqual("".join(blocks), source)
        self.assertGreater(len(blocks), 1)
        self.assertLess(max(map(len, blocks)), len(source) // 2)

    def test_one_word_correction_keeps_the_text_lineage(self) -> None:
        sources = self.vault / "Sources"
        sources.mkdir()
        original = "".join(
            f"Cette phrase numéro {index} contient un passage original suffisamment long pour produire plusieurs empreintes. "
            for index in range(30)
        )
        (sources / "journal.md").write_text(original, encoding="utf-8")
        self.commit("source text", "2026-01-01T10:00:00+01:00")

        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        corrected = original.replace("passage original", "passage corrigé", 1)
        (manuscript / "chapter.md").write_text(corrected, encoding="utf-8")
        self.commit("reuse with one correction", "2026-01-01T10:15:00+01:00")

        self.analyze("full")
        project = next(item for item in self.load("projects.json") if item["id"] == "Alpha")
        self.assertEqual(project["signes_reels_total"], 0)

    def test_incremental_dry_run_does_not_advance_sqlite_index(self) -> None:
        manuscript = self.vault / "Alpha" / "manuscrit"
        manuscript.mkdir(parents=True)
        note = manuscript / "chapter.md"
        note.write_text(self.text(500, seed=40), encoding="utf-8")
        self.commit("initial", "2026-01-01T10:00:00+01:00")
        self.analyze("full")

        database_path = self.root / ".cache" / "fingerprints.sqlite3"
        database = sqlite3.connect(database_path)
        before_commit = database.execute(
            "SELECT commit_hash FROM commits ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        before_count = database.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        database.close()

        note.write_text(self.text(900, seed=40), encoding="utf-8")
        self.commit("more text", "2026-01-01T10:15:00+01:00")
        self.analyze("incremental", "--dry-run")

        database = sqlite3.connect(database_path)
        after_commit = database.execute(
            "SELECT commit_hash FROM commits ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        after_count = database.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        database.close()
        self.assertEqual(after_commit, before_commit)
        self.assertEqual(after_count, before_count)


if __name__ == "__main__":
    unittest.main()
