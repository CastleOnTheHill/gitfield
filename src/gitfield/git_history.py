"""Read repository metadata and per-author file churn from git."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

from .errors import GitCommandError, GitfieldError
from .timewindow import git_date

_SCP_REMOTE_RE = re.compile(r"^(?P<user>[^/@\s]+)@(?P<host>[^:/\s]+):(?P<path>.+)$")
_HEADER_PREFIX = b"\x1e"
_FIELD_SEPARATOR = b"\x1f"


@dataclass(frozen=True)
class RepoInfo:
    key: str
    root: Path
    display_name: str
    source: str
    head: str
    is_bare: bool


@dataclass
class Contributor:
    name: str
    email: str
    additions: int = 0
    deletions: int = 0
    commits: int = 0
    last_commit_at: Optional[str] = None

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


@dataclass
class History:
    contributors: Dict[str, Dict[str, Contributor]] = field(default_factory=dict)
    file_last_commit: Dict[str, str] = field(default_factory=dict)
    commit_count: int = 0
    rename_count: int = 0

    def contributors_for(self, path: str) -> Dict[str, Contributor]:
        return self.contributors.get(path, {})


def _git_executable() -> str:
    executable = os.environ.get("GIT", "git")
    return executable


def _git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_PAGER", "cat")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git_command(repo: Path, args: Iterable[str]) -> list:
    return [_git_executable(), "--no-pager", "-C", str(repo), *args]


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def run_git(repo: Path, args: Iterable[str], *, check: bool = True) -> subprocess.CompletedProcess:
    command = _git_command(repo, args)
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
    )
    if check and completed.returncode != 0:
        detail = _decode(completed.stderr).strip()
        raise GitCommandError(
            f"git command failed ({' '.join(command)}): {detail or 'unknown error'}"
        )
    return completed


def _git_text(repo: Path, args: Iterable[str]) -> str:
    return _decode(run_git(repo, args).stdout).strip()


def _normalise_remote(url: str) -> Optional[str]:
    text = url.strip()
    if not text:
        return None
    scp = _SCP_REMOTE_RE.match(text)
    if scp:
        path = scp.group("path").strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"{scp.group('host').casefold()}/{path}"

    parsed = urlparse(text)
    if parsed.scheme and parsed.hostname:
        path = parsed.path.lstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"{parsed.hostname.casefold()}/{path}".rstrip("/")
    return None


def _sanitise_remote(url: str) -> str:
    text = url.strip()
    scp = _SCP_REMOTE_RE.match(text)
    if scp:
        return f"{scp.group('host')}:{scp.group('path')}"
    parsed = urlparse(text)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    return text


def inspect_repo(path: Path) -> RepoInfo:
    repo_path = path.expanduser().resolve()
    if not repo_path.exists():
        raise GitfieldError(f"repository path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise GitfieldError(f"repository path is not a directory: {repo_path}")

    try:
        bare_text = _git_text(repo_path, ["rev-parse", "--is-bare-repository"])
    except GitCommandError as exc:
        raise GitCommandError(
            f"not a git repository: {repo_path}\n{exc}"
        ) from exc

    is_bare = bare_text.casefold() == "true"
    if is_bare:
        root = repo_path
    else:
        root = Path(_git_text(repo_path, ["rev-parse", "--show-toplevel"])).resolve()

    head_completed = run_git(repo_path, ["rev-parse", "--verify", "HEAD"], check=False)
    if head_completed.returncode != 0:
        detail = _decode(head_completed.stderr).strip()
        raise GitfieldError(f"repository has no commits: {repo_path} ({detail})")
    head = _decode(head_completed.stdout).strip()

    remote_completed = run_git(
        repo_path, ["remote", "get-url", "origin"], check=False
    )
    remote = ""
    if remote_completed.returncode == 0:
        remote = _decode(remote_completed.stdout).strip()

    normalised_remote = _normalise_remote(remote) if remote else None
    if normalised_remote:
        key = f"remote:{normalised_remote}"
        source = _sanitise_remote(remote)
        display_name = normalised_remote.rsplit("/", 1)[-1]
    else:
        key = f"local:{root}"
        source = str(root)
        display_name = root.name or str(root)

    return RepoInfo(
        key=key,
        root=root,
        display_name=display_name,
        source=source,
        head=head,
        is_bare=is_bare,
    )


def list_current_files(repo: Path, rev: str) -> Set[str]:
    completed = run_git(repo, ["ls-tree", "-r", "-z", "--full-tree", rev])
    files: Set[str] = set()
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            continue
        parts = metadata.split(b" ")
        if len(parts) < 2 or parts[1] != b"blob":
            continue
        files.add(_decode(raw_path))
    return files


def _iter_nul_tokens(stream, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    buffer = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        parts = buffer.split(b"\0")
        buffer = parts.pop()
        for part in parts:
            yield part
    if buffer:
        yield buffer


def _parse_int(value: bytes) -> int:
    if value == b"-":
        return 0
    try:
        return int(value)
    except ValueError as exc:
        raise GitCommandError(f"unexpected git numstat value {value!r}") from exc


def _normalise_timestamp(value: str) -> str:
    """Normalise git's ISO timestamp to UTC so stored values sort correctly."""

    text = value.strip()
    normalised = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _later(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    if candidate is None:
        return current
    if current is None:
        return candidate
    try:
        if datetime.fromisoformat(candidate) >= datetime.fromisoformat(current):
            return candidate
    except ValueError:
        return candidate if candidate > current else current
    return current


def read_history(
    repo: Path,
    rev: str,
    since: datetime,
    until: datetime,
    current_files: Optional[Set[str]] = None,
) -> History:
    """Stream ``git log --numstat`` into per-file author churn.

    The scan window is evaluated with committer dates, matching git's own
    ``--since``/``--until`` filtering.  Merge commits are intentionally
    excluded.  For renames, the change is attributed to the new path; changes
    made before a rename in the same window are not followed backward.
    """

    command = _git_command(
        repo,
        [
            "log",
            rev,
            "--no-merges",
            "-z",
            "--numstat",
            f"--since={git_date(since)}",
            f"--until={git_date(until)}",
            "--format=%x1e%H%x1f%an%x1f%ae%x1f%cI",
        ],
    )
    stderr_file = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env=_git_env(),
        )
    except OSError as exc:
        stderr_file.close()
        raise GitCommandError(f"cannot execute git: {exc}") from exc

    history = History()
    current_commit: Optional[Tuple[str, str, str, str]] = None
    seen_paths_in_commit: Set[str] = set()
    parse_error: Optional[BaseException] = None

    try:
        assert process.stdout is not None
        tokens = _iter_nul_tokens(process.stdout)
        for token in tokens:
            if not token:
                continue

            if token.startswith(_HEADER_PREFIX):
                fields = token[1:].split(_FIELD_SEPARATOR, 3)
                if len(fields) != 4:
                    raise GitCommandError("unexpected git log header")
                sha = _decode(fields[0])
                author_name = _decode(fields[1])
                author_email = _decode(fields[2])
                committed_at = _normalise_timestamp(_decode(fields[3]))
                current_commit = (sha, author_name, author_email, committed_at)
                history.commit_count += 1
                seen_paths_in_commit = set()
                continue

            if current_commit is None:
                raise GitCommandError("git log produced numstat before a commit header")

            body = token[1:] if token.startswith(b"\n") else token
            parts = body.split(b"\t", 2)
            if len(parts) != 3:
                raise GitCommandError(f"unexpected git numstat line: {body!r}")
            additions = _parse_int(parts[0])
            deletions = _parse_int(parts[1])
            raw_path = parts[2]

            if raw_path == b"":
                old_path = next(tokens, None)
                new_path = next(tokens, None)
                if old_path is None or new_path is None:
                    raise GitCommandError("truncated rename record in git log")
                raw_path = new_path
                history.rename_count += 1

            path = _decode(raw_path)
            sha, author_name, author_email, committed_at = current_commit

            first_change_for_path = path not in seen_paths_in_commit
            seen_paths_in_commit.add(path)
            if current_files is not None and path not in current_files:
                continue

            author_key = (
                "email:" + author_email.strip().lower()
                if author_email.strip()
                else "name:" + author_name.strip().casefold()
            )
            per_file = history.contributors.setdefault(path, {})
            contributor = per_file.get(author_key)
            if contributor is None:
                contributor = Contributor(name=author_name, email=author_email)
                per_file[author_key] = contributor
            elif committed_at >= (contributor.last_commit_at or ""):
                contributor.name = author_name
                contributor.email = author_email
            contributor.additions += additions
            contributor.deletions += deletions
            if first_change_for_path:
                contributor.commits += 1
            contributor.last_commit_at = _later(
                contributor.last_commit_at, committed_at
            )
            history.file_last_commit[path] = _later(
                history.file_last_commit.get(path), committed_at
            ) or committed_at
    except BaseException as exc:
        parse_error = exc
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if parse_error is not None and process.poll() is None:
            process.kill()
        return_code = process.wait()
        stderr_file.seek(0)
        stderr = _decode(stderr_file.read()).strip()
        stderr_file.close()

    if isinstance(parse_error, (KeyboardInterrupt, SystemExit)):
        raise parse_error
    if return_code != 0:
        raise GitCommandError(
            f"git log failed ({' '.join(command)}): {stderr or 'unknown error'}"
        )
    if parse_error is not None:
        if isinstance(parse_error, GitfieldError):
            raise parse_error
        raise GitfieldError(f"cannot parse git history: {parse_error}") from parse_error

    return history
