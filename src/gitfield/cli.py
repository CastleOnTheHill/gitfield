"""Command line interface for gitfield."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import __version__
from .db import (
    Database,
    files_for_employee,
    files_for_field,
    file_detail,
    list_repos,
    open_database,
    require_repo,
    summary as summary_query,
)
from .errors import DatabaseError, GitfieldError
from .git_history import inspect_repo
from .scanner import perform_scan


def _display_width(value: str) -> int:
    width = 0
    for char in value:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - _display_width(value))


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def render_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    formatted = [[_cell(value) for value in row] for row in rows]
    widths = [_display_width(header) for header in headers]
    for row in formatted:
        for index, value in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], _display_width(value))

    def line(values: Sequence[str]) -> str:
        return "  ".join(
            _pad(value, widths[index]) for index, value in enumerate(values)
        ).rstrip()

    output = [line(list(headers))]
    output.append("  ".join("-" * width for width in widths).rstrip())
    output.extend(line(row) for row in formatted)
    return "\n".join(output)


def _modifier_label(row: Dict[str, Any]) -> str:
    if row.get("top_employee_id"):
        name = row.get("top_employee_name")
        return f"{row['top_employee_id']} ({name})" if name else str(row["top_employee_id"])
    if row.get("top_author_name") or row.get("top_author_email"):
        name = row.get("top_author_name") or row.get("top_author_email")
        email = row.get("top_author_email")
        suffix = f" <{email}>" if email and email != name else ""
        return f"{name}{suffix} [unmapped]"
    return "-"


def _field_rows(files: List[Dict[str, Any]]) -> List[List[Any]]:
    return [
        [
            row["path"],
            _modifier_label(row),
            row["assignment"],
            row["fallback_source_path"],
            row["fallback_distance"],
            row["churn"],
        ]
        for row in files
    ]


def _employee_rows(files: List[Dict[str, Any]]) -> List[List[Any]]:
    return [
        [
            row["path"],
            row["field_name"] or "-",
            row.get("responsibility", "-"),
            row["assignment"],
            row["fallback_source_path"],
            row["churn"],
        ]
        for row in files
    ]


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_field(payload: Dict[str, Any]) -> None:
    field_value = payload["field"]
    print(f"Field: {field_value['field_name']} ({field_value['field_id']})")
    print(f"Owner: {field_value['owner_id']}")
    print(f"Files: {payload['count']}")
    print()
    if payload["files"]:
        print(
            render_table(
                [
                    "PATH",
                    "TOP_MODIFIER",
                    "ASSIGNMENT",
                    "FALLBACK_SOURCE",
                    "DIST",
                    "CHURN",
                ],
                _field_rows(payload["files"]),
            )
        )
    else:
        print("(no files)")


def _print_employee(payload: Dict[str, Any]) -> None:
    employee = payload["employee"]
    name = f" ({employee['name']})" if employee.get("name") else ""
    print(f"Employee: {employee['employee_id']}{name}")
    print(f"Field: {employee['field_name']} ({employee['field_id']})")
    print(
        "Scope: direct modifications"
        + (" plus field-owner fallback files" if not payload["direct_only"] else "")
    )
    print(f"Files: {payload['count']}")
    print()
    if payload["files"]:
        print(
            render_table(
                [
                    "PATH",
                    "FIELD",
                    "ROLE",
                    "ASSIGNMENT",
                    "FALLBACK_SOURCE",
                    "CHURN",
                ],
                _employee_rows(payload["files"]),
            )
        )
    else:
        print("(no files)")


def _print_file(payload: Dict[str, Any]) -> None:
    field_label = (
        f"{payload['field_name']} ({payload['field_id']})"
        if payload.get("field_id")
        else "-"
    )
    owner = (
        f"{payload['field_owner_id']} ({payload['field_owner_name']})"
        if payload.get("field_owner_id") and payload.get("field_owner_name")
        else payload.get("field_owner_id") or "-"
    )
    responsible = (
        f"{payload['responsible_employee_id']} ({payload['responsible_employee_name']})"
        if payload.get("responsible_employee_id")
        and payload.get("responsible_employee_name")
        else payload.get("responsible_employee_id") or "-"
    )
    values: List[tuple] = [
        ("Repository", payload.get("repo_key", "-")),
        ("Path", payload["path"]),
        ("Field", field_label),
        ("Field owner", owner),
        ("Top modifier", _modifier_label(payload)),
        ("Top author email", payload.get("top_author_email") or "-"),
        ("Churn", payload["churn"]),
        ("Top commits", payload["top_commit_count"]),
        ("Last commit", payload.get("last_commit_at") or "-"),
        ("Assignment", payload["assignment"]),
        ("Fallback source", payload.get("fallback_source_path") or "-"),
        ("Fallback distance", payload.get("fallback_distance")),
        ("Responsible", responsible),
    ]
    width = max(_display_width(label) for label, _value in values)
    for label, value in values:
        print(f"{_pad(label + ':', width + 1)}  {_cell(value)}")


def _print_summary(payload: Dict[str, Any], database_path: Optional[str] = None) -> None:
    repo = payload["repo"]
    scan = payload["scan"]
    window = payload.get("window") or {
        "since": scan["since_ts"],
        "until": scan["until_ts"],
    }
    print(f"Repository: {repo['display_name']} ({repo['repo_key']})")
    print(f"Source: {repo['source']}")
    print(f"Head: {scan['head_sha']}")
    print(f"Scan: #{scan['id']} at {scan['finished_at']}")
    print(f"Window: {window['since']} -> {window['until']}")
    print(f"Commits analysed: {scan['commit_count']}")
    print(
        "Files: {files} (direct {direct}, fallback {fallback}, unassigned {unassigned})".format(
            files=scan["file_count"],
            direct=scan["direct_count"],
            fallback=scan["fallback_count"],
            unassigned=scan["unassigned_count"],
        )
    )
    if database_path:
        print(f"Database: {database_path}")
    if "duration_seconds" in payload:
        print(f"Duration: {payload['duration_seconds']}s")
    print()

    print("Fields:")
    if payload["fields"]:
        print(
            render_table(
                ["FIELD_ID", "FIELD_NAME", "OWNER", "FILES", "DIRECT", "FALLBACK"],
                [
                    [
                        item["field_id"],
                        item["field_name"],
                        item["owner_id"],
                        item["files"],
                        item["direct_files"],
                        item["fallback_files"],
                    ]
                    for item in payload["fields"]
                ],
            )
        )
    else:
        print("(none)")

    if payload["unmapped_top_authors"]:
        print()
        print("Unmapped top authors (their files use nearest-file fallback):")
        print(
            render_table(
                ["AUTHOR", "EMAIL", "FILES", "CHURN"],
                [
                    [
                        item["top_author_name"] or "-",
                        item["top_author_email"] or "-",
                        item["files"],
                        item["churn"],
                    ]
                    for item in payload["unmapped_top_authors"]
                ],
            )
        )


def _db_path(args: argparse.Namespace) -> Path:
    value = getattr(args, "db", None) or os.environ.get("GITFIELD_DB") or "gitfield.db"
    return Path(value).expanduser()


def _select_repo(database: Database, repo_argument: Optional[str]) -> Dict[str, Any]:
    if not repo_argument:
        return require_repo(database, None)

    info = inspect_repo(Path(repo_argument))
    for repo in list_repos(database):
        if repo["repo_key"] == info.key:
            return repo
        try:
            if Path(repo["local_path"]).resolve() == info.root:
                return repo
        except OSError:
            continue
    raise DatabaseError(
        f"repository {info.key!r} has not been scanned into {database.path}"
    )


def _command_scan(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).expanduser()
    to_stderr = sys.stderr.isatty()
    if to_stderr:
        print(f"Scanning {repo_path} ...", file=sys.stderr)
    result = perform_scan(
        repo_path,
        Path(args.roster).expanduser(),
        _db_path(args),
        since=args.since,
        until=args.until,
        author_map_path=(
            Path(args.author_map).expanduser() if args.author_map else None
        ),
    )
    if args.json:
        _print_json(result)
    else:
        _print_summary(result, result["database"])
    return 0


def _read_database(args: argparse.Namespace) -> Database:
    return open_database(_db_path(args), create=False)


def _command_field(args: argparse.Namespace) -> int:
    with _read_database(args) as database:
        repo = _select_repo(database, args.repo)
        field_value, files = files_for_field(
            database, repo["repo_key"], args.identifier
        )
        payload = {
            "repo": repo["repo_key"],
            "field": field_value,
            "count": len(files),
            "files": files,
        }
    if args.json:
        _print_json(payload)
    else:
        _print_field(payload)
    return 0


def _command_employee(args: argparse.Namespace) -> int:
    with _read_database(args) as database:
        repo = _select_repo(database, args.repo)
        employee, files = files_for_employee(
            database,
            repo["repo_key"],
            args.employee_id,
            direct_only=args.direct_only,
        )
        payload = {
            "repo": repo["repo_key"],
            "employee": employee,
            "direct_only": args.direct_only,
            "count": len(files),
            "files": files,
        }
    if args.json:
        _print_json(payload)
    else:
        _print_employee(payload)
    return 0


def _command_file(args: argparse.Namespace) -> int:
    with _read_database(args) as database:
        repo = _select_repo(database, args.repo)
        result = file_detail(database, repo["repo_key"], repo, args.path)
        payload = {"repo": repo["repo_key"], **result}
    if args.json:
        _print_json(payload)
    else:
        payload["repo_key"] = payload["repo"]
        _print_file(payload)
    return 0


def _command_summary(args: argparse.Namespace) -> int:
    with _read_database(args) as database:
        repo = _select_repo(database, args.repo)
        payload = summary_query(database, repo["repo_key"])
    if args.json:
        _print_json(payload)
    else:
        _print_summary(payload, str(database.path) if database.path else None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitfield",
        description=(
            "Attribute every file in a git repository to a responsibility field "
            "using two years of contribution history."
        ),
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help="SQLite database path (default: GITFIELD_DB or ./gitfield.db)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_database_option(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--db",
            metavar="PATH",
            default=argparse.SUPPRESS,
            help="SQLite database path (may also be placed before the command)",
        )

    scan_parser = subparsers.add_parser(
        "scan", help="scan a repository and replace its ownership snapshot"
    )
    add_database_option(scan_parser)
    scan_parser.add_argument("--repo", required=True, help="path to the git repository")
    scan_parser.add_argument(
        "--roster", required=True, help="responsibility roster CSV/TSV/XLSX"
    )
    scan_parser.add_argument(
        "--author-map",
        help="optional CSV/TSV mapping employee ids to git emails/names",
    )
    scan_parser.add_argument(
        "--since",
        default="2y",
        help="history window start: 2y, 18mo, 730d or an ISO date (default: 2y)",
    )
    scan_parser.add_argument(
        "--until",
        help="history window end as an ISO date (default: now)",
    )
    scan_parser.add_argument("--json", action="store_true", help="emit JSON")
    scan_parser.set_defaults(handler=_command_scan)

    field_parser = subparsers.add_parser(
        "field", help="list all files assigned to a responsibility field"
    )
    add_database_option(field_parser)
    field_parser.add_argument("identifier", help="field id or field name")
    field_parser.add_argument("--repo", help="repository path used to select the snapshot")
    field_parser.add_argument("--json", action="store_true", help="emit JSON")
    field_parser.set_defaults(handler=_command_field)

    employee_parser = subparsers.add_parser(
        "employee", help="list files directly modified by an employee"
    )
    add_database_option(employee_parser)
    employee_parser.add_argument("employee_id", help="employee id")
    employee_parser.add_argument("--repo", help="repository path used to select the snapshot")
    employee_parser.add_argument(
        "--direct-only",
        action="store_true",
        help="omit fallback files accountable to this employee as field owner",
    )
    employee_parser.add_argument("--json", action="store_true", help="emit JSON")
    employee_parser.set_defaults(handler=_command_employee)

    file_parser = subparsers.add_parser(
        "file", help="show the ownership of one file"
    )
    add_database_option(file_parser)
    file_parser.add_argument("path", help="repository-relative file path")
    file_parser.add_argument("--repo", help="repository path used to select the snapshot")
    file_parser.add_argument("--json", action="store_true", help="emit JSON")
    file_parser.set_defaults(handler=_command_file)

    summary_parser = subparsers.add_parser(
        "summary", help="show scan, field and fallback summaries"
    )
    add_database_option(summary_parser)
    summary_parser.add_argument("--repo", help="repository path used to select the snapshot")
    summary_parser.add_argument("--json", action="store_true", help="emit JSON")
    summary_parser.set_defaults(handler=_command_summary)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BrokenPipeError:
        return 0
    except (GitfieldError, sqlite3.Error, OSError) as exc:
        if os.environ.get("GITFIELD_DEBUG"):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
