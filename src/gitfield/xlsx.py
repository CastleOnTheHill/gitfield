"""A dependency-free reader for the first worksheet of an .xlsx file.

Only the subset needed by the roster parser is implemented: cell values,
shared strings, inline strings, and ordinary numeric/text cells.  Formulas use
their cached values, which is what spreadsheet exporters normally write.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from .errors import RosterError

_CELL_REF_RE = re.compile(r"^([A-Za-z]+)")
_NUMBER_WITH_ZERO_DECIMALS_RE = re.compile(r"^(-?\d+)\.0+$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_index(reference: str) -> int:
    match = _CELL_REF_RE.match(reference or "")
    if not match:
        return 0
    value = 0
    for char in match.group(1).upper():
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _normalise_number(text: str) -> str:
    match = _NUMBER_WITH_ZERO_DECIMALS_RE.match(text)
    if match:
        return match.group(1)
    return text


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(payload)
    values: List[str] = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        values.append("".join(node.text or "" for node in item.iter() if _local(node.tag) == "t"))
    return values


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = None
    for node in workbook.iter():
        if _local(node.tag) == "sheet":
            sheet = node
            break
    if sheet is None:
        raise RosterError("the Excel workbook does not contain a worksheet")

    relationship_id = None
    for key, value in sheet.attrib.items():
        if _local(key) == "id":
            relationship_id = value
            break

    targets: Dict[str, str] = {}
    try:
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        rels = None
    if rels is not None:
        for node in rels:
            if _local(node.tag) != "Relationship":
                continue
            identifier = node.attrib.get("Id")
            target = node.attrib.get("Target")
            if identifier and target:
                targets[identifier] = target

    target = targets.get(relationship_id or "")
    if not target:
        target = "worksheets/sheet1.xml"
    if target.startswith("/"):
        path = target.lstrip("/")
    else:
        path = posixpath.normpath(posixpath.join("xl", target))
    return path


def read_first_sheet_rows(path: Path) -> List[List[str]]:
    """Return the first worksheet as a rectangular list of strings."""

    try:
        with zipfile.ZipFile(path) as archive:
            shared = _shared_strings(archive)
            sheet_path = _first_sheet_path(archive)
            try:
                sheet = ET.fromstring(archive.read(sheet_path))
            except KeyError as exc:
                raise RosterError(
                    f"worksheet {sheet_path!r} is missing from {path}"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise RosterError(f"{path} is not a valid .xlsx workbook") from exc
    except KeyError as exc:
        raise RosterError(f"{path} is not a valid .xlsx workbook") from exc

    rows: List[List[str]] = []
    for row_node in sheet.iter():
        if _local(row_node.tag) != "row":
            continue
        values: Dict[int, str] = {}
        for cell in row_node:
            if _local(cell.tag) != "c":
                continue
            reference = cell.attrib.get("r", "")
            index = _column_index(reference)
            cell_type = cell.attrib.get("t")
            raw: Optional[str] = None

            if cell_type == "inlineStr":
                inline = next(
                    (node for node in cell if _local(node.tag) == "is"), None
                )
                if inline is not None:
                    raw = "".join(
                        node.text or ""
                        for node in inline.iter()
                        if _local(node.tag) == "t"
                    )
            else:
                value_node = next(
                    (node for node in cell if _local(node.tag) == "v"), None
                )
                if value_node is not None and value_node.text is not None:
                    raw = value_node.text

            if raw is None:
                values[index] = ""
            elif cell_type == "s":
                try:
                    values[index] = shared[int(raw)]
                except (ValueError, IndexError) as exc:
                    raise RosterError(
                        f"invalid shared string reference {raw!r} in {path}"
                    ) from exc
            elif cell_type in (None, "n", "str"):
                values[index] = _normalise_number(raw)
            else:
                values[index] = raw

        width = max(values) + 1 if values else 0
        rows.append([values.get(index, "") for index in range(width)])

    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    return rows
