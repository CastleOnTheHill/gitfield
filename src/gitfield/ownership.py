"""Compute file ownership from churn and fall back through the directory tree."""

from __future__ import annotations

import heapq
import posixpath
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .git_history import Contributor, History
from .identity import EmployeeResolver
from .roster import Roster


@dataclass
class FileContributor:
    path: str
    author_key: str
    employee_id: Optional[str]
    author_name: str
    author_email: str
    additions: int
    deletions: int
    commit_count: int
    last_commit_at: Optional[str]


@dataclass
class OwnershipRecord:
    path: str
    field_id: Optional[str]
    top_employee_id: Optional[str]
    top_author_key: Optional[str]
    top_author_name: Optional[str]
    top_author_email: Optional[str]
    top_additions: int
    top_deletions: int
    top_commit_count: int
    last_commit_at: Optional[str]
    assignment: str
    fallback_source_path: Optional[str]
    fallback_distance: Optional[int]
    responsible_employee_id: Optional[str]

    @property
    def churn(self) -> int:
        return self.top_additions + self.top_deletions


@dataclass
class AssignmentResult:
    files: List[OwnershipRecord]
    contributors: List[FileContributor]
    direct_count: int
    fallback_count: int
    unassigned_count: int
    unmapped_top_authors: Set[str] = field(default_factory=set)


@dataclass
class _Group:
    key: str
    employee_id: Optional[str]
    name: str
    email: str
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    last_commit_at: Optional[str] = None

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


def _is_later(candidate: Optional[str], current: Optional[str]) -> bool:
    if candidate is None:
        return False
    if current is None:
        return True
    try:
        from datetime import datetime

        return datetime.fromisoformat(candidate) > datetime.fromisoformat(current)
    except ValueError:
        return candidate > current


def _directory(path: str) -> str:
    return posixpath.dirname(path)


def _ancestors(directory: str) -> Iterable[str]:
    current = directory
    while True:
        yield current
        if not current:
            break
        current = posixpath.dirname(current)


def _parent(directory: str) -> Optional[str]:
    if not directory:
        return None
    return posixpath.dirname(directory)


def _filename_affinity(target: str, source: str) -> int:
    left = posixpath.basename(target).casefold()
    right = posixpath.basename(source).casefold()
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _merge_contributors(
    path: str,
    raw: Dict[str, Contributor],
    resolver: EmployeeResolver,
    roster: Roster,
) -> List[_Group]:
    groups: Dict[str, _Group] = {}
    for raw_key, contributor in raw.items():
        resolution = resolver.resolve(contributor.name, contributor.email)
        if resolution.employee_id and resolution.employee_id in roster.employees:
            group_key = f"employee:{resolution.employee_id}"
            employee_id = resolution.employee_id
        else:
            group_key = f"author:{raw_key}"
            employee_id = None

        group = groups.get(group_key)
        if group is None:
            group = _Group(
                key=group_key,
                employee_id=employee_id,
                name=contributor.name,
                email=contributor.email,
                last_commit_at=contributor.last_commit_at,
            )
            groups[group_key] = group
        elif _is_later(contributor.last_commit_at, group.last_commit_at):
            group.name = contributor.name
            group.email = contributor.email
            group.last_commit_at = contributor.last_commit_at

        group.additions += contributor.additions
        group.deletions += contributor.deletions
        group.commits += contributor.commits
        if _is_later(contributor.last_commit_at, group.last_commit_at):
            group.last_commit_at = contributor.last_commit_at

    return list(groups.values())


def _top_group(groups: List[_Group]) -> Optional[_Group]:
    if not groups:
        return None
    return min(
        groups,
        key=lambda group: (
            -group.churn,
            -group.commits,
            -group.additions,
            -group.deletions,
            group.key,
        ),
    )


def _directory_sources(
    paths: Iterable[str],
    direct_by_directory: Dict[str, List[OwnershipRecord]],
) -> Dict[str, Tuple[int, str, str]]:
    """Multi-source BFS over the directory tree.

    Return ``directory -> (directory distance, source file, field)`` and the
    directory adjacency map.  Every source is a directly assigned file.  Ties
    at the same distance are resolved by the lexicographically smaller source
    path, which keeps repeated scans deterministic.
    """

    nodes: Set[str] = {""}
    for path in paths:
        for directory in _ancestors(_directory(path)):
            nodes.add(directory)

    children: Dict[str, Set[str]] = {}
    for directory in nodes:
        parent = _parent(directory)
        if parent is not None:
            children.setdefault(parent, set()).add(directory)

    def neighbours(directory: str) -> Iterable[str]:
        parent = _parent(directory)
        if parent is not None:
            yield parent
        yield from children.get(directory, ())

    heap: List[Tuple[int, str, str, str]] = []
    for directory, records in direct_by_directory.items():
        representative = min(records, key=lambda record: record.path)
        assert representative.field_id is not None
        heapq.heappush(
            heap, (0, representative.path, representative.field_id, directory)
        )

    sources: Dict[str, Tuple[int, str, str]] = {}
    while heap:
        distance, source_path, field_id, directory = heapq.heappop(heap)
        if directory in sources:
            continue
        sources[directory] = (distance, source_path, field_id)
        for neighbour in neighbours(directory):
            if neighbour not in sources:
                heapq.heappush(
                    heap,
                    (distance + 1, source_path, field_id, neighbour),
                )
    return sources


def assign_ownership(
    files: Iterable[str],
    history: History,
    roster: Roster,
    resolver: EmployeeResolver,
) -> AssignmentResult:
    all_files = sorted(set(files))
    records: Dict[str, OwnershipRecord] = {}
    contributors: List[FileContributor] = []
    unresolved: List[OwnershipRecord] = []
    direct_by_directory: Dict[str, List[OwnershipRecord]] = {}
    unmapped_top_authors: Set[str] = set()

    for path in all_files:
        groups = _merge_contributors(
            path, history.contributors_for(path), resolver, roster
        )
        for group in groups:
            contributors.append(
                FileContributor(
                    path=path,
                    author_key=group.key,
                    employee_id=group.employee_id,
                    author_name=group.name,
                    author_email=group.email,
                    additions=group.additions,
                    deletions=group.deletions,
                    commit_count=group.commits,
                    last_commit_at=group.last_commit_at,
                )
            )

        top = _top_group(groups)
        field_id: Optional[str] = None
        top_employee_id: Optional[str] = None
        if top is not None:
            top_employee_id = top.employee_id
            if top_employee_id is not None:
                field_id = roster.employees[top_employee_id].field_id

        if field_id is not None:
            record = OwnershipRecord(
                path=path,
                field_id=field_id,
                top_employee_id=top_employee_id,
                top_author_key=top.key if top else None,
                top_author_name=top.name if top else None,
                top_author_email=top.email if top else None,
                top_additions=top.additions if top else 0,
                top_deletions=top.deletions if top else 0,
                top_commit_count=top.commits if top else 0,
                last_commit_at=history.file_last_commit.get(path),
                assignment="direct",
                fallback_source_path=None,
                fallback_distance=None,
                responsible_employee_id=top_employee_id,
            )
            records[path] = record
            direct_by_directory.setdefault(_directory(path), []).append(record)
        else:
            if top is not None and top.key:
                unmapped_top_authors.add(top.key)
            record = OwnershipRecord(
                path=path,
                field_id=None,
                top_employee_id=None,
                top_author_key=top.key if top else None,
                top_author_name=top.name if top else None,
                top_author_email=top.email if top else None,
                top_additions=top.additions if top else 0,
                top_deletions=top.deletions if top else 0,
                top_commit_count=top.commits if top else 0,
                last_commit_at=history.file_last_commit.get(path),
                assignment="pending",
                fallback_source_path=None,
                fallback_distance=None,
                responsible_employee_id=None,
            )
            records[path] = record
            unresolved.append(record)

    sources = _directory_sources(all_files, direct_by_directory)

    for record in unresolved:
        directory = _directory(record.path)
        local_candidates = direct_by_directory.get(directory, [])
        source_path: Optional[str] = None
        field_id: Optional[str] = None
        distance: Optional[int] = None

        if local_candidates:
            candidate = min(
                local_candidates,
                key=lambda item: (
                    -_filename_affinity(record.path, item.path),
                    item.path,
                ),
            )
            source_path = candidate.path
            field_id = candidate.field_id
            distance = 0
        elif directory in sources:
            distance, source_path, field_id = sources[directory]

        if field_id is None or source_path is None:
            record.assignment = (
                "unassigned_no_history"
                if record.top_author_key is None
                else "unassigned_unmapped_author"
            )
            continue

        record.field_id = field_id
        record.assignment = (
            "fallback_no_history"
            if record.top_author_key is None
            else "fallback_unmapped_author"
        )
        record.fallback_source_path = source_path
        record.fallback_distance = distance
        record.responsible_employee_id = roster.fields[field_id].owner_id

    ordered = [records[path] for path in all_files]
    direct_count = sum(1 for record in ordered if record.assignment == "direct")
    fallback_count = sum(
        1 for record in ordered if record.assignment.startswith("fallback_")
    )
    unassigned_count = sum(
        1 for record in ordered if record.assignment.startswith("unassigned_")
    )

    contributors.sort(key=lambda item: (item.path, item.author_key))
    return AssignmentResult(
        files=ordered,
        contributors=contributors,
        direct_count=direct_count,
        fallback_count=fallback_count,
        unassigned_count=unassigned_count,
        unmapped_top_authors=unmapped_top_authors,
    )
