"""Deterministic acceptance fixture: a real git repository with history."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


@dataclass
class DemoRepo:
    base: Path
    repo: Path
    roster: Path
    author_map: Path
    database: Path
    now: datetime
    commit_dates: List[datetime]

    @property
    def env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_ROOT) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        env.pop("GITFIELD_DB", None)
        return env


def _git(repo: Path, args: List[str], env: Optional[dict] = None) -> subprocess.CompletedProcess:
    command = [
        "git",
        "-C",
        str(repo),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        *args,
    ]
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _append(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def _commit(
    repo: Path,
    author: str,
    email: str,
    when: datetime,
    message: str,
) -> None:
    timestamp = when.astimezone(timezone.utc).isoformat(timespec="seconds")
    env = {
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _git(repo, ["add", "-A"], env=env)
    _git(repo, ["commit", "-q", "-m", message], env=env)


def create_demo_repo(
    base: Path,
    *,
    now: Optional[datetime] = None,
) -> DemoRepo:
    """Create a repository exercising direct, departed and no-history cases.

    Files:
      services/payment/charge.py                    Alice -> PAY (direct)
      services/payment/refund.py                    Bob -> PAY (mapped)
      services/payment/adapters/v2/gateway.py       departed -> PAY (2 hops)
      services/search/index.py                      Carol -> SEARCH (direct)
      services/search/ranker.py                     Carol -> SEARCH (renamed)
      services/search/legacy_ranker.py              departed -> SEARCH (sibling)
      services/search/notes.md                      no recent history -> SEARCH
      services/shared_util.py                       Alice -> PAY (direct)
    """

    base = base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    repo = base / "demo-repo"
    repo.mkdir()
    current = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    old = current - timedelta(days=4 * 365)
    dates = [current - timedelta(days=400 - index * 10) for index in range(7)]

    _git(repo, ["init", "-q", "-b", "main"])

    _write(
        repo,
        "services/payment/charge.py",
        "\n".join(f"old-charge-{index}" for index in range(200)) + "\n",
    )
    _write(repo, "services/search/notes.md", "# Search notes\n")
    _write(repo, "services/search/legacy_ranker.py", "legacy base\n")
    _write(repo, "services/search/tmp_ranker.py", "temporary ranker\n")
    _write(repo, "services/search/index.py", "index base\n")
    _commit(
        repo,
        "Legacy Contributor",
        "7777@corp.example",
        old,
        "old history outside the window",
    )

    _append(repo, "services/payment/charge.py", "alice-1\nalice-2\nalice-3\nalice-4\n")
    _commit(repo, "Alice", "1001@corp.example", dates[0], "Alice updates charge")

    _write(repo, "services/payment/refund.py", "refund-1\nrefund-2\nrefund-3\n")
    _commit(
        repo,
        "Bob Wang",
        "bob.wang@corp.example",
        dates[1],
        "Bob creates refund",
    )

    _append(
        repo,
        "services/search/index.py",
        "\n".join(f"carol-index-{index}" for index in range(10)) + "\n",
    )
    _commit(repo, "Carol", "2001@corp.example", dates[2], "Carol updates index")

    _append(
        repo,
        "services/search/legacy_ranker.py",
        "\n".join(f"dave-legacy-{index}" for index in range(30)) + "\n",
    )
    _commit(
        repo,
        "Dave Former",
        "9999@corp.example",
        dates[3],
        "departed author updates legacy ranker",
    )

    _write(
        repo,
        "services/payment/adapters/v2/gateway.py",
        "\n".join(f"gateway-{index}" for index in range(10)) + "\n",
    )
    _commit(
        repo,
        "Former Contractor",
        "8888@corp.example",
        dates[4],
        "departed author creates nested gateway",
    )

    _write(
        repo,
        "services/shared_util.py",
        "\n".join(f"util-{index}" for index in range(5)) + "\n",
    )
    _commit(repo, "Alice", "1001@corp.example", dates[5], "Alice creates shared util")

    _git(repo, ["mv", "services/search/tmp_ranker.py", "services/search/ranker.py"])
    _append(repo, "services/search/ranker.py", "carol-ranker\n")
    _commit(
        repo,
        "Carol",
        "2001@corp.example",
        dates[6],
        "rename ranker and update it",
    )

    roster = base / "responsibility.csv"
    roster.write_text(
        "field_id,field_name,owner_id,member_id\n"
        "PAY,支付平台,1001,1001\n"
        "PAY,支付平台,1001,1002\n"
        "SEARCH,搜索推荐,2001,2001\n"
        "DATA,数据分析,3001,3001\n",
        encoding="utf-8",
    )
    author_map = base / "author-map.csv"
    author_map.write_text(
        "employee_id,git_email\n"
        "1002,bob.wang@corp.example\n",
        encoding="utf-8",
    )

    return DemoRepo(
        base=base,
        repo=repo,
        roster=roster,
        author_map=author_map,
        database=base / "gitfield.db",
        now=current,
        commit_dates=dates,
    )


def add_incremental_commit(fixture: DemoRepo) -> None:
    _append(
        fixture.repo,
        "services/shared_util.py",
        "\n".join(f"erin-{index}" for index in range(100)) + "\n",
    )
    _write(
        fixture.repo,
        "analytics/events.sql",
        "\n".join(f"select {index};" for index in range(15)) + "\n",
    )
    _commit(
        fixture.repo,
        "Erin",
        "3001@corp.example",
        fixture.now - timedelta(hours=2),
        "Erin takes over shared util and adds analytics",
    )


def run_cli(fixture: DemoRepo, args: List[str]) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "gitfield", *args]
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=fixture.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
