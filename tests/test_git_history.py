import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from gitfield.git_history import inspect_repo, list_current_files, read_history
from tests.support import create_demo_repo


class GitHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = create_demo_repo(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_repository_inventory_and_churn(self):
        info = inspect_repo(self.fixture.repo)
        files = list_current_files(info.root, info.head)
        self.assertEqual(len(files), 8)
        self.assertIn("services/search/ranker.py", files)
        self.assertNotIn("services/search/tmp_ranker.py", files)

        history = read_history(
            info.root,
            info.head,
            self.fixture.now - timedelta(days=730),
            self.fixture.now + timedelta(days=1),
            current_files=files,
        )
        self.assertEqual(history.commit_count, 7)
        self.assertEqual(history.rename_count, 1)

        charge = history.contributors_for("services/payment/charge.py")
        self.assertEqual(len(charge), 1)
        alice = next(iter(charge.values()))
        self.assertEqual(alice.email, "1001@corp.example")
        self.assertEqual(alice.additions, 4)
        self.assertNotIn("7777@corp.example", {item.email for item in charge.values()})

        legacy = history.contributors_for("services/search/legacy_ranker.py")
        dave = next(iter(legacy.values()))
        self.assertEqual(dave.email, "9999@corp.example")
        self.assertEqual(dave.additions, 30)

        self.assertNotIn(
            "services/search/notes.md",
            history.contributors,
            "files whose only commits are outside the window must have no stats",
        )

        ranker = history.contributors_for("services/search/ranker.py")
        self.assertEqual(
            {item.email for item in ranker.values()}, {"2001@corp.example"}
        )

    def test_remote_url_does_not_leak_credentials(self):
        subprocess.run(
            [
                "git",
                "-C",
                str(self.fixture.repo),
                "remote",
                "add",
                "origin",
                "https://user:secret-token@github.com/example/private-repo.git",
            ],
            check=True,
            capture_output=True,
        )
        info = inspect_repo(self.fixture.repo)
        self.assertEqual(
            info.key, "remote:github.com/example/private-repo"
        )
        self.assertNotIn("secret-token", info.source)
        self.assertNotIn("user", info.source)

    def test_remote_key_is_stable(self):
        subprocess_result = subprocess.run(
            [
                "git",
                "-C",
                str(self.fixture.repo),
                "remote",
                "add",
                "origin",
                "git@github.com:example/demo-repo.git",
            ],
            check=True,
            capture_output=True,
        )
        self.assertEqual(subprocess_result.returncode, 0)
        info = inspect_repo(self.fixture.repo)
        self.assertEqual(info.key, "remote:github.com/example/demo-repo")


if __name__ == "__main__":
    unittest.main()
