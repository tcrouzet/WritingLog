from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CACHE_SCRIPT = ROOT / "scripts" / "cache_history.py"


class CacheHistoryTest(unittest.TestCase):
    def test_creates_and_updates_complete_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.invalid"], check=True)
            (source / "note.md").write_text("première version", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "note.md"], check=True)
            env = os.environ.copy()
            env.update({"GIT_AUTHOR_DATE": "2026-01-01T10:00:00+01:00", "GIT_COMMITTER_DATE": "2026-01-01T10:00:00+01:00"})
            subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "initial"], check=True, env=env)

            config = root / "config.yaml"
            config.write_text(
                f'vault_path: "{source}"\nhistory_repo: ".cache/history.git"\n',
                encoding="utf-8",
            )
            command = [sys.executable, str(CACHE_SCRIPT), "--config", str(config)]
            first = subprocess.run(command, text=True, capture_output=True, check=True)
            self.assertIn("1 commits", first.stdout)

            second = subprocess.run([*command, "update"], text=True, capture_output=True, check=True)
            self.assertIn("Synchronisation du miroir", second.stdout)
            marker = root / ".cache/history.git/old-cache-marker"
            marker.write_text("ancien", encoding="utf-8")
            rebuilt = subprocess.run(command, text=True, capture_output=True, check=True)
            self.assertIn("Suppression de l'ancien cache", rebuilt.stdout)
            self.assertFalse(marker.exists())
            count = subprocess.run(
                ["git", "-C", str(root / ".cache/history.git"), "rev-list", "--all", "--count"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(count, "1")


if __name__ == "__main__":
    unittest.main()
