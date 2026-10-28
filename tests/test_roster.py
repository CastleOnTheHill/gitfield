import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from gitfield.errors import RosterError
from gitfield.roster import load_author_map, load_roster


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, content):
        path = self.base / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_long_format_auto_adds_owner_and_aliases(self):
        path = self._write(
            "roster.csv",
            "field_id,责任田名称,责任田主工号,员工工号,员工邮箱\n"
            "PAY,支付平台,1001,1001,1001@corp.example\n"
            "PAY,支付平台,1001,1002,1002@corp.example\n",
        )
        roster = load_roster(path)
        self.assertEqual(set(roster.fields), {"PAY"})
        self.assertEqual(roster.fields["PAY"].owner_id, "1001")
        self.assertEqual(set(roster.employees), {"1001", "1002"})
        self.assertTrue(roster.employees["1001"].is_owner)
        self.assertEqual(roster.email_aliases["1002@corp.example"], "1002")

    def test_wide_format_and_owner_folder(self):
        path = self._write(
            "roster.csv",
            "field_name,owner_id,members\n"
            "搜索推荐,2001,\"2002;2003\"\n",
        )
        roster = load_roster(path)
        self.assertEqual(len(roster.fields), 1)
        field_value = next(iter(roster.fields.values()))
        self.assertEqual(field_value.field_name, "搜索推荐")
        self.assertEqual(set(roster.employees), {"2001", "2002", "2003"})

    def test_two_column_owner_member_roster(self):
        path = self._write(
            "roster.csv",
            "owner_id,member_id\n1001,1001\n1001,1002\n",
        )
        roster = load_roster(path)
        self.assertEqual(set(roster.fields), {"OWNER_1001"})
        self.assertEqual(roster.fields["OWNER_1001"].field_name, "owner:1001")
        self.assertEqual(set(roster.employees), {"1001", "1002"})

    def test_employee_cannot_belong_to_two_fields(self):
        path = self._write(
            "roster.csv",
            "field_id,field_name,owner_id,member_id\n"
            "A,田A,1001,2001\n"
            "B,田B,1002,2001\n",
        )
        with self.assertRaises(RosterError):
            load_roster(path)

    def test_author_map(self):
        roster_path = self._write(
            "roster.csv",
            "field_id,field_name,owner_id,member_id\n"
            "PAY,支付平台,1001,1001\n"
            "PAY,支付平台,1001,1002\n",
        )
        map_path = self._write(
            "authors.csv",
            "employee_id,email,name\n"
            "1002,bob.wang@corp.example,Bob Wang\n",
        )
        roster = load_roster(roster_path)
        author_map = load_author_map(map_path, roster)
        self.assertEqual(author_map.email_aliases["bob.wang@corp.example"], "1002")
        self.assertEqual(author_map.name_aliases["bob wang"], "1002")

    def test_xlsx_roster(self):
        path = self.base / "roster.xlsx"
        shared = [
            "field_id",
            "field_name",
            "owner_id",
            "member_id",
            "PAY",
            "支付平台",
            "1001",
            "1002",
        ]
        sheet_rows = [
            [0, 1, 2, 3],
            [4, 5, 6, 7],
        ]
        coordinates = ["A", "B", "C", "D"]
        sheet_xml = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            "<sheetData>",
        ]
        for row_number, row in enumerate(sheet_rows, start=1):
            sheet_xml.append(f'<row r="{row_number}">')
            for index, value in enumerate(row):
                sheet_xml.append(
                    f'<c r="{coordinates[index]}{row_number}" t="s"><v>{value}</v></c>'
                )
            sheet_xml.append("</row>")
        sheet_xml.extend(["</sheetData>", "</worksheet>"])
        shared_xml = (
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'count="{len(shared)}" uniqueCount="{len(shared)}">'
            + "".join(f"<si><t>{escape(value)}</t></si>" for value in shared)
            + "</sst>"
        )
        workbook_xml = (
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        rels_xml = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", workbook_xml)
            archive.writestr("xl/_rels/workbook.xml.rels", rels_xml)
            archive.writestr("xl/sharedStrings.xml", shared_xml)
            archive.writestr("xl/worksheets/sheet1.xml", "".join(sheet_xml))

        roster = load_roster(path)
        self.assertEqual(roster.fields["PAY"].field_name, "支付平台")
        self.assertEqual(set(roster.employees), {"1001", "1002"})


if __name__ == "__main__":
    unittest.main()
