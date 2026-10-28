"""End-to-end scan orchestration."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from .db import open_database, store_scan
from .db import summary as summary_query
from .git_history import inspect_repo, list_current_files, read_history
from .identity import EmployeeResolver
from .ownership import assign_ownership
from .roster import load_author_map, load_roster
from .timewindow import iso_date, now_iso, parse_window


def perform_scan(
    repo_path: Path,
    roster_path: Path,
    db_path: Path,
    *,
    since: str = "2y",
    until: Optional[str] = None,
    author_map_path: Optional[Path] = None,
    now=None,
) -> Dict[str, Any]:
    started_monotonic = time.monotonic()
    started_at = now_iso()

    roster = load_roster(roster_path)
    author_map = (
        load_author_map(author_map_path, roster) if author_map_path else None
    )
    resolver = EmployeeResolver(roster, author_map)

    window_start, window_end = parse_window(since, until, now=now)
    repo = inspect_repo(repo_path)
    current_files = list_current_files(repo.root, repo.head)
    history = read_history(
        repo.root,
        repo.head,
        window_start,
        window_end,
        current_files=current_files,
    )
    assignment = assign_ownership(current_files, history, roster, resolver)

    finished_at = now_iso()
    with open_database(db_path, create=True) as database:
        scan_id = store_scan(
            database,
            repo,
            roster,
            assignment,
            since_iso=iso_date(window_start),
            until_iso=iso_date(window_end),
            author_map_sha256=author_map.sha256 if author_map else None,
            history_commit_count=history.commit_count,
            started_at=started_at,
            finished_at=finished_at,
        )
        database_path = str(database.path)
        result = summary_query(database, repo.key)

    result["scan_id"] = scan_id
    result["database"] = database_path
    result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
    result["window"] = {
        "since": iso_date(window_start),
        "until": iso_date(window_end),
    }
    return result
