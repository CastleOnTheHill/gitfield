"""Parsing for responsibility rosters and optional author mappings."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import RosterError
from .xlsx import read_first_sheet_rows

_HEADER_SPLIT_RE = re.compile(r"[\s\-]+")
_MEMBER_SPLIT_RE = re.compile(r"[,;，；、\s]+")
_EMAIL_SPLIT_RE = re.compile(r"[;，、\s]+")
_NAME_SPLIT_RE = re.compile(r"[;，、]+")


def _normalise_header(value: str) -> str:
    text = value.replace("\ufeff", "").strip().lower()
    text = _HEADER_SPLIT_RE.sub("_", text)
    return text


def _normalise_email(value: str) -> str:
    return value.strip().lower()


# Header aliases are deliberately permissive because rosters are commonly
# exported from Chinese spreadsheets with slightly different column titles.
_HEADER_ALIASES: Dict[str, str] = {}
for canonical, aliases in {
    "field_name": (
        "field",
        "field_name",
        "team",
        "team_name",
        "domain",
        "area",
        "责任田",
        "责任田名称",
        "领域",
        "领域名称",
        "团队",
        "团队名称",
        "业务领域",
    ),
    "field_id": (
        "field_id",
        "team_id",
        "domain_id",
        "area_id",
        "责任田id",
        "责任田编号",
        "领域id",
        "团队id",
    ),
    "owner_id": (
        "owner",
        "owner_id",
        "field_owner",
        "field_owner_id",
        "责任田主",
        "责任田主工号",
        "负责人",
        "负责人工号",
    ),
    "member_id": (
        "member",
        "member_id",
        "employee",
        "employee_id",
        "员工",
        "员工工号",
        "成员",
        "成员工号",
    ),
    "member_ids": (
        "members",
        "member_ids",
        "employees",
        "employee_ids",
        "员工列表",
        "员工工号列表",
        "成员工号列表",
        "成员列表",
    ),
    "employee_name": (
        "name",
        "employee_name",
        "member_name",
        "姓名",
        "员工姓名",
        "成员姓名",
    ),
    "member_emails": (
        "git_email",
        "git_emails",
        "email",
        "emails",
        "邮箱",
        "git邮箱",
        "员工邮箱",
        "成员邮箱",
    ),
    "owner_emails": (
        "owner_email",
        "owner_emails",
        "owner_git_email",
        "责任田主邮箱",
        "负责人邮箱",
    ),
    "member_git_names": (
        "git_name",
        "git_names",
        "git姓名",
    ),
}.items():
    for alias in aliases:
        _HEADER_ALIASES[_normalise_header(alias)] = canonical


@dataclass(frozen=True)
class FieldDefinition:
    field_id: str
    field_name: str
    owner_id: str


@dataclass(frozen=True)
class RosterEmployee:
    employee_id: str
    field_id: str
    name: Optional[str]
    is_owner: bool


@dataclass
class Roster:
    path: Path
    sha256: str
    fields: Dict[str, FieldDefinition]
    employees: Dict[str, RosterEmployee]
    email_aliases: Dict[str, str] = field(default_factory=dict)
    name_aliases: Dict[str, str] = field(default_factory=dict)


@dataclass
class AuthorMap:
    path: Path
    sha256: str
    email_aliases: Dict[str, str]
    name_aliases: Dict[str, str]


@dataclass
class _EmployeeBuilder:
    employee_id: str
    field_id: str
    name: Optional[str] = None
    is_owner: bool = False


class _Builder:
    def __init__(self, path: Path, sha256: str) -> None:
        self.path = path
        self.sha256 = sha256
        self.fields: Dict[str, FieldDefinition] = {}
        self.field_names: Dict[str, str] = {}
        self.employees: Dict[str, _EmployeeBuilder] = {}
        self.email_aliases: Dict[str, str] = {}
        self.name_aliases: Dict[str, str] = {}

    def add_field(self, field_id: str, field_name: str, owner_id: str) -> None:
        existing = self.fields.get(field_id)
        if existing is not None:
            if existing.field_name != field_name:
                raise RosterError(
                    f"field id {field_id!r} is used for both "
                    f"{existing.field_name!r} and {field_name!r}"
                )
            if existing.owner_id != owner_id:
                raise RosterError(
                    f"field {field_name!r} has conflicting owners "
                    f"{existing.owner_id!r} and {owner_id!r}"
                )
            return
        other_id = self.field_names.get(field_name)
        if other_id is not None and other_id != field_id:
            raise RosterError(
                f"field name {field_name!r} is used by ids "
                f"{other_id!r} and {field_id!r}"
            )
        self.fields[field_id] = FieldDefinition(field_id, field_name, owner_id)
        self.field_names[field_name] = field_id

    def add_employee(
        self,
        employee_id: str,
        field_id: str,
        *,
        is_owner: bool = False,
        name: Optional[str] = None,
    ) -> None:
        existing = self.employees.get(employee_id)
        if existing is not None:
            if existing.field_id != field_id:
                raise RosterError(
                    f"employee {employee_id!r} appears in fields "
                    f"{existing.field_id!r} and {field_id!r}"
                )
            if is_owner:
                existing.is_owner = True
            if name and not existing.name:
                existing.name = name
            return
        self.employees[employee_id] = _EmployeeBuilder(
            employee_id=employee_id,
            field_id=field_id,
            name=name or None,
            is_owner=is_owner,
        )

    def add_email_alias(self, email: str, employee_id: str) -> None:
        normalised = _normalise_email(email)
        if not normalised:
            return
        existing = self.email_aliases.get(normalised)
        if existing is not None and existing != employee_id:
            raise RosterError(
                f"git email {normalised!r} maps to both employees "
                f"{existing!r} and {employee_id!r}"
            )
        self.email_aliases[normalised] = employee_id

    def add_name_alias(self, name: str, employee_id: str) -> None:
        normalised = name.strip().casefold()
        if not normalised:
            return
        existing = self.name_aliases.get(normalised)
        if existing is not None and existing != employee_id:
            raise RosterError(
                f"git name {name!r} maps to both employees "
                f"{existing!r} and {employee_id!r}"
            )
        self.name_aliases[normalised] = employee_id

    def build(self) -> Roster:
        if not self.fields:
            raise RosterError("the roster does not define any responsibility fields")
        if not self.employees:
            raise RosterError("the roster does not define any employees")
        fields = dict(self.fields)
        employees = {
            employee_id: RosterEmployee(
                employee_id=employee_id,
                field_id=builder.field_id,
                name=builder.name,
                is_owner=builder.is_owner,
            )
            for employee_id, builder in self.employees.items()
        }
        for field_value in fields.values():
            if field_value.owner_id not in employees:
                raise RosterError(
                    f"owner {field_value.owner_id!r} of field "
                    f"{field_value.field_name!r} is missing from the roster"
                )
        return Roster(
            path=self.path,
            sha256=self.sha256,
            fields=fields,
            employees=employees,
            email_aliases=dict(self.email_aliases),
            name_aliases=dict(self.name_aliases),
        )


def _decode_table(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RosterError(
        f"{path} is not valid UTF-8/GB18030 text; save it as UTF-8 CSV or XLSX"
    )


def _delimiter_for(path: Path, text: str) -> str:
    if path.suffix.lower() == ".tsv":
        return "\t"
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        return dialect.delimiter
    except csv.Error:
        return ","


def read_table_rows(path: Path) -> List[List[str]]:
    """Read a CSV/TSV/XLSX table as rows of strings."""

    if not path.exists():
        raise RosterError(f"table not found: {path}")
    if not path.is_file():
        raise RosterError(f"table path is not a file: {path}")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return read_first_sheet_rows(path)
    if suffix == ".xls":
        raise RosterError(
            "legacy .xls files are not supported; save the sheet as .xlsx or CSV"
        )

    text = _decode_table(path)
    delimiter = _delimiter_for(path, text)
    try:
        return [
            [cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        ]
    except csv.Error as exc:
        raise RosterError(f"cannot parse {path}: {exc}") from exc


def _canonical_columns(
    header: Sequence[str],
) -> Tuple[Dict[str, List[int]], List[str]]:
    columns: Dict[str, List[int]] = {}
    unknown: List[str] = []
    for index, cell in enumerate(header):
        normalised = _normalise_header(cell)
        if not normalised:
            continue
        canonical = _HEADER_ALIASES.get(normalised)
        if canonical is None:
            unknown.append(cell)
        else:
            columns.setdefault(canonical, []).append(index)
    return columns, unknown


def _value(row: Sequence[str], columns: Dict[str, List[int]], kind: str) -> str:
    for index in columns.get(kind, []):
        if index < len(row) and row[index].strip():
            return row[index].strip()
    return ""


def _values(row: Sequence[str], columns: Dict[str, List[int]], kind: str) -> List[str]:
    result: List[str] = []
    for index in columns.get(kind, []):
        if index < len(row) and row[index].strip():
            result.append(row[index].strip())
    return result


def _split_members(value: str) -> List[str]:
    return [part for part in _MEMBER_SPLIT_RE.split(value) if part]


def _default_field_id(field_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", field_name).strip("_").upper()
    if slug:
        return slug
    digest = hashlib.sha1(field_name.encode("utf-8")).hexdigest()[:8].upper()
    return f"FIELD_{digest}"


def _header_row_index(rows: Sequence[Sequence[str]]) -> int:
    for index, row in enumerate(rows):
        if any(cell.strip() for cell in row):
            return index
    raise RosterError("the roster table is empty")


def load_roster(path: Path) -> Roster:
    path = path.expanduser().resolve()
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    rows = read_table_rows(path)
    header_index = _header_row_index(rows)
    columns, _unknown = _canonical_columns(rows[header_index])

    if not columns.get("owner_id"):
        raise RosterError(
            "the roster header needs an owner column, for example "
            "'owner_id' or '责任田主工号'"
        )
    if not (columns.get("member_id") or columns.get("member_ids")):
        raise RosterError(
            "the roster header needs an employee column, for example "
            "'member_id' or '员工工号'"
        )

    builder = _Builder(path, sha256)
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue

        field_name = _value(row, columns, "field_name")
        field_id = _value(row, columns, "field_id")
        owner_id = _value(row, columns, "owner_id")
        if not owner_id:
            raise RosterError(f"row {row_number}: owner id is empty")
        if not field_name and not field_id:
            # A two-column owner/member roster is valid: each owner defines a
            # responsibility field named after that owner.
            field_id = f"OWNER_{re.sub(r'[^A-Za-z0-9]+', '_', owner_id).strip('_') or owner_id}"
            field_name = f"owner:{owner_id}"
        if not field_name:
            field_name = field_id
        if not field_id:
            field_id = _default_field_id(field_name)
        if not field_name:
            raise RosterError(f"row {row_number}: field name/id is empty")

        builder.add_field(field_id, field_name, owner_id)
        builder.add_employee(owner_id, field_id, is_owner=True)

        member_values = _values(row, columns, "member_id")
        member_values.extend(_values(row, columns, "member_ids"))
        members: List[str] = []
        for value in member_values:
            members.extend(_split_members(value))
        members = list(dict.fromkeys(members))

        employee_name = _value(row, columns, "employee_name")
        for member_id in members:
            builder.add_employee(
                member_id,
                field_id,
                name=employee_name if len(members) == 1 else None,
            )

        member_emails: List[str] = []
        for value in _values(row, columns, "member_emails"):
            member_emails.extend(
                part for part in _EMAIL_SPLIT_RE.split(value) if part
            )
        if member_emails:
            if len(members) != 1:
                raise RosterError(
                    f"row {row_number}: email columns require exactly one member "
                    "per row; use the long format for multiple members"
                )
            for email in member_emails:
                builder.add_email_alias(email, members[0])

        owner_emails: List[str] = []
        for value in _values(row, columns, "owner_emails"):
            owner_emails.extend(part for part in _EMAIL_SPLIT_RE.split(value) if part)
        for email in owner_emails:
            builder.add_email_alias(email, owner_id)

        git_names: List[str] = []
        for value in _values(row, columns, "member_git_names"):
            git_names.extend(part.strip() for part in _NAME_SPLIT_RE.split(value) if part.strip())
        if git_names:
            if len(members) != 1:
                raise RosterError(
                    f"row {row_number}: git_name columns require exactly one member "
                    "per row"
                )
            for name in git_names:
                builder.add_name_alias(name, members[0])

    return builder.build()


def load_author_map(path: Path, roster: Roster) -> AuthorMap:
    path = path.expanduser().resolve()
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    rows = read_table_rows(path)
    header_index = _header_row_index(rows)
    header = rows[header_index]

    canonical: Dict[str, List[int]] = {}
    for index, cell in enumerate(header):
        normalised = _normalise_header(cell)
        if normalised in ("employee_id", "employee", "id", "工号", "员工工号"):
            canonical.setdefault("employee_id", []).append(index)
        elif normalised in (
            "email",
            "emails",
            "git_email",
            "git_emails",
            "邮箱",
            "git邮箱",
        ):
            canonical.setdefault("email", []).append(index)
        elif normalised in ("name", "git_name", "git_names", "姓名", "git姓名"):
            canonical.setdefault("name", []).append(index)

    if not canonical.get("employee_id"):
        raise RosterError("the author map needs an employee_id column")
    if not (canonical.get("email") or canonical.get("name")):
        raise RosterError("the author map needs at least an email or name column")

    email_aliases: Dict[str, str] = {}
    name_aliases: Dict[str, str] = {}

    def add_alias(mapping: Dict[str, str], value: str, employee_id: str) -> None:
        if value in mapping and mapping[value] != employee_id:
            raise RosterError(
                f"author alias {value!r} maps to both {mapping[value]!r} "
                f"and {employee_id!r}"
            )
        mapping[value] = employee_id

    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        employee_id = _value(row, canonical, "employee_id")
        if not employee_id:
            raise RosterError(f"author map row {row_number}: employee id is empty")
        if employee_id not in roster.employees:
            raise RosterError(
                f"author map row {row_number}: employee {employee_id!r} "
                "is not present in the roster"
            )

        emails: List[str] = []
        for value in _values(row, canonical, "email"):
            emails.extend(part for part in _EMAIL_SPLIT_RE.split(value) if part)
        for email in emails:
            add_alias(email_aliases, _normalise_email(email), employee_id)

        names: List[str] = []
        for value in _values(row, canonical, "name"):
            names.extend(part.strip() for part in _NAME_SPLIT_RE.split(value) if part.strip())
        for name in names:
            add_alias(name_aliases, name.strip().casefold(), employee_id)

    return AuthorMap(
        path=path,
        sha256=sha256,
        email_aliases=email_aliases,
        name_aliases=name_aliases,
    )
