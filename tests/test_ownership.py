import tempfile
import unittest
from pathlib import Path

from gitfield.git_history import Contributor, History
from gitfield.identity import EmployeeResolver
from gitfield.ownership import assign_ownership
from gitfield.roster import load_roster


class OwnershipUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        roster_path = base / "roster.csv"
        roster_path.write_text(
            "field_id,field_name,owner_id,member_id\n"
            "A,田A,1001,1001\n"
            "B,田B,2001,2001\n",
            encoding="utf-8",
        )
        self.roster = load_roster(roster_path)
        self.resolver = EmployeeResolver(self.roster)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_direct_seed_leaves_file_unassigned(self):
        history = History()
        history.contributors["orphan/x.py"] = {
            "email:9999@corp.example": Contributor(
                name="Former",
                email="9999@corp.example",
                additions=10,
                commits=1,
                last_commit_at="2026-01-01T00:00:00+00:00",
            )
        }
        result = assign_ownership(
            ["orphan/x.py"], history, self.roster, self.resolver
        )
        record = result.files[0]
        self.assertEqual(record.assignment, "unassigned_unmapped_author")
        self.assertIsNone(record.field_id)
        self.assertEqual(result.unassigned_count, 1)

    def test_no_history_inherits_across_directory_distance(self):
        history = History()
        history.contributors["team/a.py"] = {
            "email:1001@corp.example": Contributor(
                name="Alice",
                email="1001@corp.example",
                additions=8,
                commits=1,
                last_commit_at="2026-01-01T00:00:00+00:00",
            )
        }
        history.file_last_commit["team/a.py"] = "2026-01-01T00:00:00+00:00"
        result = assign_ownership(
            ["team/a.py", "orphan/x.py"], history, self.roster, self.resolver
        )
        by_path = {record.path: record for record in result.files}
        self.assertEqual(by_path["team/a.py"].assignment, "direct")
        self.assertEqual(by_path["orphan/x.py"].assignment, "fallback_no_history")
        self.assertEqual(by_path["orphan/x.py"].field_id, "A")
        self.assertEqual(by_path["orphan/x.py"].fallback_source_path, "team/a.py")
        self.assertEqual(by_path["orphan/x.py"].fallback_distance, 2)

    def test_churn_is_additions_plus_deletions(self):
        history = History()
        per_file = history.contributors["team/a.py"] = {}
        per_file["email:1001@corp.example"] = Contributor(
            name="Alice",
            email="1001@corp.example",
            additions=4,
            deletions=0,
            commits=2,
            last_commit_at="2026-01-02T00:00:00+00:00",
        )
        per_file["email:2001@corp.example"] = Contributor(
            name="Bob",
            email="2001@corp.example",
            additions=1,
            deletions=10,
            commits=1,
            last_commit_at="2026-01-01T00:00:00+00:00",
        )
        result = assign_ownership(
            ["team/a.py"], history, self.roster, self.resolver
        )
        record = result.files[0]
        self.assertEqual(record.top_employee_id, "2001")
        self.assertEqual(record.field_id, "B")
        self.assertEqual(record.churn, 11)


if __name__ == "__main__":
    unittest.main()
