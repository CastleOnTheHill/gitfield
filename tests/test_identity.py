import tempfile
import unittest
from pathlib import Path

from gitfield.identity import EmployeeResolver
from gitfield.roster import load_author_map, load_roster


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_inference_explicit_mapping_and_unknown(self):
        roster_path = self.base / "roster.csv"
        roster_path.write_text(
            "field_id,field_name,owner_id,member_id\n"
            "A,田A,1001,1001\n"
            "A,田A,1001,1002\n",
            encoding="utf-8",
        )
        map_path = self.base / "map.csv"
        map_path.write_text(
            "employee_id,email,name\n"
            "1002,bob.wang@corp.example,Bob Wang\n",
            encoding="utf-8",
        )
        roster = load_roster(roster_path)
        resolver = EmployeeResolver(roster, load_author_map(map_path, roster))

        alice = resolver.resolve("Alice", "1001@corp.example")
        self.assertEqual(alice.employee_id, "1001")
        self.assertEqual(alice.basis, "inferred")

        bob = resolver.resolve("Someone Else", "bob.wang@corp.example")
        self.assertEqual(bob.employee_id, "1002")
        self.assertEqual(bob.basis, "explicit_email")

        unknown = resolver.resolve("Former", "9999@corp.example")
        self.assertIsNone(unknown.employee_id)
        self.assertEqual(unknown.basis, "unresolved")

    def test_ambiguous_inference_is_not_guessed(self):
        roster_path = self.base / "roster.csv"
        roster_path.write_text(
            "field_id,field_name,owner_id,member_id\n"
            "A,田A,alice,alice\n"
            "B,田B,1001,1001\n",
            encoding="utf-8",
        )
        resolver = EmployeeResolver(load_roster(roster_path))
        resolution = resolver.resolve("Someone", "alice.1001@corp.example")
        self.assertIsNone(resolution.employee_id)
        self.assertEqual(resolution.basis, "ambiguous")
        self.assertEqual(set(resolution.candidates), {"1001", "alice"})


if __name__ == "__main__":
    unittest.main()
