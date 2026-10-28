import json
import tempfile
import unittest
from pathlib import Path

from tests.support import add_incremental_commit, create_demo_repo, run_cli


def _json_result(result, context=""):
    if result.returncode != 0:
        raise AssertionError(
            f"{context} failed with exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


class CLIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.fixture = create_demo_repo(Path(cls.temp.name))
        cls.scan = _json_result(
            run_cli(
                cls.fixture,
                [
                    "--db",
                    str(cls.fixture.database),
                    "scan",
                    "--repo",
                    str(cls.fixture.repo),
                    "--roster",
                    str(cls.fixture.roster),
                    "--author-map",
                    str(cls.fixture.author_map),
                    "--json",
                ],
            ),
            "initial scan",
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def query(self, *args):
        return _json_result(
            run_cli(
                self.fixture,
                ["--db", str(self.fixture.database), *args, "--json"],
            )
        )

    def test_scan_summary(self):
        scan = self.scan["scan"]
        self.assertEqual(scan["commit_count"], 7)
        self.assertEqual(scan["file_count"], 8)
        self.assertEqual(scan["direct_count"], 5)
        self.assertEqual(scan["fallback_count"], 3)
        self.assertEqual(scan["unassigned_count"], 0)
        self.assertEqual(scan["unmapped_author_count"], 2)
        self.assertEqual(len(scan["roster_sha256"]), 64)

        fields = {item["field_id"]: item for item in self.scan["fields"]}
        self.assertEqual(fields["PAY"]["files"], 4)
        self.assertEqual(fields["PAY"]["fallback_files"], 1)
        self.assertEqual(fields["SEARCH"]["files"], 4)
        self.assertEqual(fields["SEARCH"]["fallback_files"], 2)
        self.assertEqual(fields["DATA"]["files"], 0)

    def test_field_query_by_name(self):
        payload = self.query("field", "支付平台")
        self.assertEqual(payload["field"]["field_id"], "PAY")
        self.assertEqual(payload["count"], 4)
        paths = {item["path"] for item in payload["files"]}
        self.assertEqual(
            paths,
            {
                "services/payment/charge.py",
                "services/payment/refund.py",
                "services/payment/adapters/v2/gateway.py",
                "services/shared_util.py",
            },
        )

    def test_employee_query_and_direct_only(self):
        payload = self.query("employee", "1001")
        self.assertEqual(payload["count"], 3)
        roles = {item["path"]: item["responsibility"] for item in payload["files"]}
        self.assertEqual(roles["services/payment/charge.py"], "top_modifier")
        self.assertEqual(
            roles["services/payment/adapters/v2/gateway.py"], "field_owner_fallback"
        )

        direct = self.query("employee", "1001", "--direct-only")
        self.assertEqual(direct["count"], 2)
        self.assertTrue(
            all(item["responsibility"] == "top_modifier" for item in direct["files"])
        )

    def test_recent_churn_beats_old_history(self):
        payload = self.query("file", "services/payment/charge.py")
        self.assertEqual(payload["assignment"], "direct")
        self.assertEqual(payload["field_id"], "PAY")
        self.assertEqual(payload["top_employee_id"], "1001")
        self.assertEqual(payload["churn"], 4)

    def test_departed_author_uses_sibling_file(self):
        payload = self.query("file", "services/search/legacy_ranker.py")
        self.assertEqual(payload["assignment"], "fallback_unmapped_author")
        self.assertEqual(payload["field_id"], "SEARCH")
        self.assertIsNone(payload["top_employee_id"])
        self.assertEqual(payload["top_author_email"], "9999@corp.example")
        self.assertEqual(payload["fallback_source_path"], "services/search/index.py")
        self.assertEqual(payload["fallback_distance"], 0)
        self.assertEqual(payload["responsible_employee_id"], "2001")

    def test_departed_author_uses_recursive_directory_distance(self):
        payload = self.query("file", "services/payment/adapters/v2/gateway.py")
        self.assertEqual(payload["assignment"], "fallback_unmapped_author")
        self.assertEqual(payload["field_id"], "PAY")
        self.assertEqual(payload["fallback_source_path"], "services/payment/charge.py")
        self.assertEqual(payload["fallback_distance"], 2)

    def test_no_history_file_is_marked_and_falls_back(self):
        payload = self.query("file", "services/search/notes.md")
        self.assertEqual(payload["assignment"], "fallback_no_history")
        self.assertEqual(payload["field_id"], "SEARCH")
        self.assertIsNone(payload["top_employee_id"])
        self.assertIsNone(payload["top_author_name"])
        self.assertEqual(payload["fallback_source_path"], "services/search/index.py")

    def test_rename_attributes_current_path(self):
        payload = self.query("file", "services/search/ranker.py")
        self.assertEqual(payload["assignment"], "direct")
        self.assertEqual(payload["field_id"], "SEARCH")
        self.assertEqual(payload["top_employee_id"], "2001")

    def test_absolute_file_path(self):
        absolute = self.fixture.repo / "services/search/notes.md"
        payload = self.query("file", str(absolute))
        self.assertEqual(payload["path"], "services/search/notes.md")

    def test_summary_query_and_repo_option(self):
        payload = _json_result(
            run_cli(
                self.fixture,
                [
                    "summary",
                    "--repo",
                    str(self.fixture.repo),
                    "--db",
                    str(self.fixture.database),
                    "--json",
                ],
            ),
            "summary",
        )
        self.assertEqual(payload["scan"]["id"], 1)
        self.assertEqual(len(payload["unmapped_top_authors"]), 2)

    def test_human_file_output_marks_fallback(self):
        result = run_cli(
            self.fixture,
            [
                "file",
                "services/search/legacy_ranker.py",
                "--db",
                str(self.fixture.database),
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Assignment:", result.stdout)
        self.assertIn("fallback_unmapped_author", result.stdout)
        self.assertIn("Fallback source:", result.stdout)

    def test_unknown_field_is_an_error(self):
        result = run_cli(
            self.fixture,
            ["field", "NOT_A_FIELD", "--db", str(self.fixture.database)],
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("field not found", result.stderr)


class IncrementalScanTests(unittest.TestCase):
    def test_rescan_updates_ownership_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = create_demo_repo(Path(temp))
            first = _json_result(
                run_cli(
                    fixture,
                    [
                        "--db",
                        str(fixture.database),
                        "scan",
                        "--repo",
                        str(fixture.repo),
                        "--roster",
                        str(fixture.roster),
                        "--author-map",
                        str(fixture.author_map),
                        "--json",
                    ],
                ),
                "first scan",
            )
            self.assertEqual(first["scan"]["id"], 1)

            add_incremental_commit(fixture)
            second = _json_result(
                run_cli(
                    fixture,
                    [
                        "--db",
                        str(fixture.database),
                        "scan",
                        "--repo",
                        str(fixture.repo),
                        "--roster",
                        str(fixture.roster),
                        "--author-map",
                        str(fixture.author_map),
                        "--json",
                    ],
                ),
                "second scan",
            )
            self.assertEqual(second["scan"]["id"], 2)
            self.assertEqual(second["scan"]["file_count"], 9)
            self.assertEqual(second["scan"]["direct_count"], 6)
            self.assertEqual(second["scan"]["fallback_count"], 3)
            self.assertEqual(second["scan"]["commit_count"], 8)

            data_field = _json_result(
                run_cli(
                    fixture,
                    [
                        "--db",
                        str(fixture.database),
                        "field",
                        "DATA",
                        "--json",
                    ],
                ),
                "DATA field",
            )
            self.assertEqual(
                {item["path"] for item in data_field["files"]},
                {"analytics/events.sql", "services/shared_util.py"},
            )

            util = _json_result(
                run_cli(
                    fixture,
                    [
                        "--db",
                        str(fixture.database),
                        "file",
                        "services/shared_util.py",
                        "--json",
                    ],
                ),
                "util file",
            )
            self.assertEqual(util["field_id"], "DATA")
            self.assertEqual(util["top_employee_id"], "3001")
            self.assertEqual(util["churn"], 100)

            third = _json_result(
                run_cli(
                    fixture,
                    [
                        "--db",
                        str(fixture.database),
                        "scan",
                        "--repo",
                        str(fixture.repo),
                        "--roster",
                        str(fixture.roster),
                        "--author-map",
                        str(fixture.author_map),
                        "--json",
                    ],
                ),
                "idempotent rescan",
            )
            self.assertEqual(third["scan"]["id"], 3)
            self.assertEqual(third["scan"]["file_count"], 9)
            self.assertEqual(third["scan"]["direct_count"], 6)


if __name__ == "__main__":
    unittest.main()
