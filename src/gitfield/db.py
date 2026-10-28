"""SQLite persistence and read queries for gitfield."""

from __future__ import annotations

import posixpath
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import DatabaseError
from .git_history import RepoInfo
from .ownership import AssignmentResult
from .roster import Roster
from .timewindow import now_iso

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    repo_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source TEXT NOT NULL,
    local_path TEXT NOT NULL,
    head_sha TEXT,
    last_scan_id INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fields (
    repo_key TEXT NOT NULL,
    field_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    PRIMARY KEY (repo_key, field_id),
    UNIQUE (repo_key, field_name),
    FOREIGN KEY (repo_key) REFERENCES repos(repo_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS employees (
    repo_key TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    name TEXT,
    is_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
    PRIMARY KEY (repo_key, employee_id),
    FOREIGN KEY (repo_key, field_id)
        REFERENCES fields(repo_key, field_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT NOT NULL,
    since_ts TEXT NOT NULL,
    until_ts TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    roster_sha256 TEXT NOT NULL,
    author_map_sha256 TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    commit_count INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    direct_count INTEGER NOT NULL,
    fallback_count INTEGER NOT NULL,
    unassigned_count INTEGER NOT NULL,
    unmapped_author_count INTEGER NOT NULL,
    FOREIGN KEY (repo_key) REFERENCES repos(repo_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS file_ownership (
    repo_key TEXT NOT NULL,
    path TEXT NOT NULL,
    field_id TEXT,
    top_employee_id TEXT,
    top_author_key TEXT,
    top_author_name TEXT,
    top_author_email TEXT,
    top_additions INTEGER NOT NULL DEFAULT 0,
    top_deletions INTEGER NOT NULL DEFAULT 0,
    top_commit_count INTEGER NOT NULL DEFAULT 0,
    last_commit_at TEXT,
    assignment TEXT NOT NULL,
    fallback_source_path TEXT,
    fallback_distance INTEGER,
    responsible_employee_id TEXT,
    scan_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo_key, path),
    FOREIGN KEY (repo_key) REFERENCES repos(repo_key) ON DELETE CASCADE,
    FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE,
    FOREIGN KEY (repo_key, field_id)
        REFERENCES fields(repo_key, field_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS file_authors (
    repo_key TEXT NOT NULL,
    path TEXT NOT NULL,
    author_key TEXT NOT NULL,
    employee_id TEXT,
    author_name TEXT,
    author_email TEXT,
    additions INTEGER NOT NULL,
    deletions INTEGER NOT NULL,
    commit_count INTEGER NOT NULL,
    last_commit_at TEXT,
    PRIMARY KEY (repo_key, path, author_key),
    FOREIGN KEY (repo_key) REFERENCES repos(repo_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ownership_field
    ON file_ownership(repo_key, field_id);
CREATE INDEX IF NOT EXISTS idx_ownership_top_employee
    ON file_ownership(repo_key, top_employee_id);
CREATE INDEX IF NOT EXISTS idx_ownership_responsible
    ON file_ownership(repo_key, responsible_employee_id);
CREATE INDEX IF NOT EXISTS idx_authors_employee
    ON file_authors(repo_key, employee_id);

PRAGMA user_version = 1;
"""

_SELECT_FILE = """
SELECT
    o.path,
    o.field_id,
    f.field_name,
    f.owner_id AS field_owner_id,
    field_emp.name AS field_owner_name,
    o.top_employee_id,
    top_emp.name AS top_employee_name,
    o.top_author_name,
    o.top_author_email,
    o.top_additions,
    o.top_deletions,
    o.top_additions + o.top_deletions AS churn,
    o.top_commit_count,
    o.last_commit_at,
    o.assignment,
    o.fallback_source_path,
    o.fallback_distance,
    o.responsible_employee_id,
    resp_emp.name AS responsible_employee_name
FROM file_ownership o
LEFT JOIN fields f
    ON f.repo_key = o.repo_key AND f.field_id = o.field_id
LEFT JOIN employees field_emp
    ON field_emp.repo_key = o.repo_key
   AND field_emp.employee_id = f.owner_id
LEFT JOIN employees top_emp
    ON top_emp.repo_key = o.repo_key
   AND top_emp.employee_id = o.top_employee_id
LEFT JOIN employees resp_emp
    ON resp_emp.repo_key = o.repo_key
   AND resp_emp.employee_id = o.responsible_employee_id
"""


class Database:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self.connection = connection

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _table_names(connection: sqlite3.Connection) -> List[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [row[0] for row in rows]


def open_database(path: Path, *, create: bool) -> Database:
    db_path = path.expanduser().resolve()
    if not create and not db_path.exists():
        raise DatabaseError(
            f"database not found: {db_path}; run 'gitfield scan' first"
        )
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        connection = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot open database {db_path}: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    try:
        if version == 0:
            if not create:
                raise DatabaseError(
                    f"{db_path} is not a gitfield database; run 'gitfield scan' "
                    "to initialise a new database"
                )
            tables = _table_names(connection)
            if tables:
                raise DatabaseError(
                    f"{db_path} is an existing SQLite database with unrecognised "
                    "tables; choose a new --db path"
                )
            connection.executescript(_SCHEMA)
            connection.commit()
        elif version != SCHEMA_VERSION:
            raise DatabaseError(
                f"database schema version {version} is not supported "
                f"(expected {SCHEMA_VERSION}): {db_path}"
            )
        connection.execute("PRAGMA journal_mode = WAL")
    except Exception:
        connection.close()
        raise
    return Database(db_path, connection)


def store_scan(
    database: Database,
    repo: RepoInfo,
    roster: Roster,
    assignment: AssignmentResult,
    *,
    since_iso: str,
    until_iso: str,
    author_map_sha256: Optional[str],
    history_commit_count: int,
    started_at: str,
    finished_at: str,
) -> int:
    connection = database.connection
    updated_at = now_iso()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO repos(
                    repo_key, display_name, source, local_path, head_sha,
                    last_scan_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(repo_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    source = excluded.source,
                    local_path = excluded.local_path,
                    head_sha = excluded.head_sha,
                    updated_at = excluded.updated_at
                """,
                (
                    repo.key,
                    repo.display_name,
                    repo.source,
                    str(repo.root),
                    repo.head,
                    updated_at,
                ),
            )

            connection.execute(
                "DELETE FROM file_authors WHERE repo_key = ?", (repo.key,)
            )
            connection.execute(
                "DELETE FROM file_ownership WHERE repo_key = ?", (repo.key,)
            )
            connection.execute("DELETE FROM employees WHERE repo_key = ?", (repo.key,))
            connection.execute("DELETE FROM fields WHERE repo_key = ?", (repo.key,))

            connection.executemany(
                """
                INSERT INTO fields(repo_key, field_id, field_name, owner_id)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        repo.key,
                        field_value.field_id,
                        field_value.field_name,
                        field_value.owner_id,
                    )
                    for field_value in sorted(
                        roster.fields.values(), key=lambda item: item.field_id
                    )
                ],
            )
            connection.executemany(
                """
                INSERT INTO employees(
                    repo_key, employee_id, field_id, name, is_owner
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo.key,
                        employee.employee_id,
                        employee.field_id,
                        employee.name,
                        1 if employee.is_owner else 0,
                    )
                    for employee in sorted(
                        roster.employees.values(), key=lambda item: item.employee_id
                    )
                ],
            )

            cursor = connection.execute(
                """
                INSERT INTO scans(
                    repo_key, since_ts, until_ts, head_sha, roster_sha256,
                    author_map_sha256, started_at, finished_at, commit_count,
                    file_count, direct_count, fallback_count, unassigned_count,
                    unmapped_author_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo.key,
                    since_iso,
                    until_iso,
                    repo.head,
                    roster.sha256,
                    author_map_sha256,
                    started_at,
                    finished_at,
                    history_commit_count,
                    len(assignment.files),
                    assignment.direct_count,
                    assignment.fallback_count,
                    assignment.unassigned_count,
                    len(assignment.unmapped_top_authors),
                ),
            )
            scan_id = int(cursor.lastrowid)

            connection.executemany(
                """
                INSERT INTO file_ownership(
                    repo_key, path, field_id, top_employee_id, top_author_key,
                    top_author_name, top_author_email, top_additions,
                    top_deletions, top_commit_count, last_commit_at, assignment,
                    fallback_source_path, fallback_distance,
                    responsible_employee_id, scan_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo.key,
                        record.path,
                        record.field_id,
                        record.top_employee_id,
                        record.top_author_key,
                        record.top_author_name,
                        record.top_author_email,
                        record.top_additions,
                        record.top_deletions,
                        record.top_commit_count,
                        record.last_commit_at,
                        record.assignment,
                        record.fallback_source_path,
                        record.fallback_distance,
                        record.responsible_employee_id,
                        scan_id,
                        updated_at,
                    )
                    for record in assignment.files
                ],
            )
            connection.executemany(
                """
                INSERT INTO file_authors(
                    repo_key, path, author_key, employee_id, author_name,
                    author_email, additions, deletions, commit_count, last_commit_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo.key,
                        item.path,
                        item.author_key,
                        item.employee_id,
                        item.author_name,
                        item.author_email,
                        item.additions,
                        item.deletions,
                        item.commit_count,
                        item.last_commit_at,
                    )
                    for item in assignment.contributors
                ],
            )
            connection.execute(
                """
                UPDATE repos
                SET last_scan_id = ?, updated_at = ?
                WHERE repo_key = ?
                """,
                (scan_id, updated_at, repo.key),
            )
        return scan_id
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot save scan to {database.path}: {exc}") from exc


def _repo_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def list_repos(database: Database) -> List[Dict[str, Any]]:
    rows = database.connection.execute(
        "SELECT * FROM repos ORDER BY updated_at DESC, repo_key"
    ).fetchall()
    return [_repo_dict(row) for row in rows]


def require_repo(database: Database, repo_key: Optional[str] = None) -> Dict[str, Any]:
    repos = list_repos(database)
    if repo_key is not None:
        for repo in repos:
            if repo["repo_key"] == repo_key:
                return repo
        raise DatabaseError(
            f"repository {repo_key!r} has not been scanned into {database.path}"
        )
    if not repos:
        raise DatabaseError(
            f"no scans found in {database.path}; run 'gitfield scan' first"
        )
    if len(repos) > 1:
        identifiers = ", ".join(repo["repo_key"] for repo in repos)
        raise DatabaseError(
            "the database contains multiple repositories; pass --repo PATH "
            f"to choose one ({identifiers})"
        )
    return repos[0]


def latest_scan(database: Database, repo_key: str) -> Dict[str, Any]:
    row = database.connection.execute(
        "SELECT * FROM scans WHERE repo_key = ? ORDER BY id DESC LIMIT 1",
        (repo_key,),
    ).fetchone()
    if row is None:
        raise DatabaseError("the repository has no completed scan")
    return {key: row[key] for key in row.keys()}


def get_field(
    database: Database, repo_key: str, identifier: str
) -> Optional[Dict[str, Any]]:
    rows = database.connection.execute(
        """
        SELECT * FROM fields
        WHERE repo_key = ?
          AND (field_id = ? COLLATE NOCASE OR field_name = ? COLLATE NOCASE)
        """,
        (repo_key, identifier, identifier),
    ).fetchall()
    if len(rows) > 1:
        matches = ", ".join(row["field_id"] for row in rows)
        raise DatabaseError(
            f"field identifier {identifier!r} is ambiguous ({matches}); use the field id"
        )
    return {key: rows[0][key] for key in rows[0].keys()} if rows else None


def get_employee(
    database: Database, repo_key: str, employee_id: str
) -> Optional[Dict[str, Any]]:
    row = database.connection.execute(
        """
        SELECT e.*, f.field_name
        FROM employees e
        JOIN fields f ON f.repo_key = e.repo_key AND f.field_id = e.field_id
        WHERE e.repo_key = ? AND e.employee_id = ?
        """,
        (repo_key, employee_id),
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def _rows(rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def files_for_field(
    database: Database, repo_key: str, identifier: str
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    field_value = get_field(database, repo_key, identifier)
    if field_value is None:
        raise DatabaseError(f"field not found: {identifier}")
    rows = database.connection.execute(
        _SELECT_FILE + " WHERE o.repo_key = ? AND o.field_id = ? ORDER BY o.path",
        (repo_key, field_value["field_id"]),
    ).fetchall()
    return field_value, _rows(rows)


def files_for_employee(
    database: Database,
    repo_key: str,
    employee_id: str,
    *,
    direct_only: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    employee = get_employee(database, repo_key, employee_id)
    if employee is None:
        raise DatabaseError(f"employee not found in the roster: {employee_id}")

    condition = "o.top_employee_id = ?"
    params: List[Any] = [repo_key, employee_id]
    if not direct_only:
        condition = """
        (
            o.top_employee_id = ?
            OR (o.assignment LIKE 'fallback_%' AND f.owner_id = ?)
        )
        """
        params = [repo_key, employee_id, employee_id]

    rows = database.connection.execute(
        _SELECT_FILE + f" WHERE o.repo_key = ? AND {condition} ORDER BY o.path",
        tuple(params),
    ).fetchall()
    result = _rows(rows)
    for item in result:
        item["responsibility"] = (
            "top_modifier"
            if item["top_employee_id"] == employee_id
            else "field_owner_fallback"
        )
    return employee, result


def normalize_path(repo: Dict[str, Any], value: str) -> str:
    raw = value.strip()
    if not raw:
        raise DatabaseError("file path is empty")
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        try:
            relative = candidate.resolve().relative_to(
                Path(repo["local_path"]).resolve()
            )
        except ValueError as exc:
            raise DatabaseError(
                f"file path {value!r} is outside the scanned repository"
            ) from exc
        raw = relative.as_posix()
    else:
        raw = raw.replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        raw = posixpath.normpath(raw)
    if raw in ("", ".") or raw.startswith("../") or raw == "..":
        raise DatabaseError(f"invalid repository-relative path: {value!r}")
    return raw


def file_detail(
    database: Database,
    repo_key: str,
    repo: Dict[str, Any],
    path: str,
) -> Dict[str, Any]:
    normalised = normalize_path(repo, path)
    row = database.connection.execute(
        _SELECT_FILE + " WHERE o.repo_key = ? AND o.path = ?",
        (repo_key, normalised),
    ).fetchone()
    if row is None:
        raise DatabaseError(f"file not found in the ownership database: {normalised}")
    return {key: row[key] for key in row.keys()}


def summary(database: Database, repo_key: str) -> Dict[str, Any]:
    repo = require_repo(database, repo_key)
    scan = latest_scan(database, repo_key)
    fields = database.connection.execute(
        """
        SELECT
            f.field_id,
            f.field_name,
            f.owner_id,
            COUNT(o.path) AS files,
            COALESCE(SUM(CASE WHEN o.assignment LIKE 'fallback_%' THEN 1 ELSE 0 END), 0)
                AS fallback_files,
            COALESCE(SUM(CASE WHEN o.assignment = 'direct' THEN 1 ELSE 0 END), 0)
                AS direct_files
        FROM fields f
        LEFT JOIN file_ownership o
            ON o.repo_key = f.repo_key AND o.field_id = f.field_id
        WHERE f.repo_key = ?
        GROUP BY f.repo_key, f.field_id, f.field_name, f.owner_id
        ORDER BY f.field_name, f.field_id
        """,
        (repo_key,),
    ).fetchall()
    unmapped = database.connection.execute(
        """
        SELECT
            top_author_key,
            top_author_name,
            top_author_email,
            COUNT(*) AS files,
            COALESCE(SUM(top_additions + top_deletions), 0) AS churn
        FROM file_ownership
        WHERE repo_key = ?
          AND top_employee_id IS NULL
          AND top_author_key IS NOT NULL
        GROUP BY top_author_key, top_author_name, top_author_email
        ORDER BY files DESC, churn DESC, top_author_key
        """,
        (repo_key,),
    ).fetchall()
    return {
        "repo": repo,
        "scan": scan,
        "fields": _rows(fields),
        "unmapped_top_authors": _rows(unmapped),
    }
