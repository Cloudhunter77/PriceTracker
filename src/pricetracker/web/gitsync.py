"""Keeping the local files and GitHub in step.

The daily workflow commits price history from CI, so a repo the UI is editing
drifts behind. Rather than pushing on every keystroke, the UI pulls before it
writes and offers an explicit Push once you're done making changes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

TRACKED = ("watchlist.yaml", "events.yaml", "data")
TIMEOUT = 60


@dataclass(slots=True)
class GitStatus:
    is_repo: bool
    dirty_files: list[str]
    ahead: int
    error: str | None = None

    @property
    def needs_push(self) -> bool:
        return bool(self.dirty_files) or self.ahead > 0


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def status(root: Path = Path(".")) -> GitStatus:
    """What is uncommitted or unpushed right now."""
    try:
        inside = _run(["rev-parse", "--is-inside-work-tree"], root)
        if inside.returncode != 0:
            return GitStatus(is_repo=False, dirty_files=[], ahead=0)

        changed = _run(["status", "--porcelain", "--", *TRACKED], root)
        files = [line[3:].strip() for line in changed.stdout.splitlines() if line.strip()]

        ahead = 0
        counts = _run(["rev-list", "--count", "@{upstream}..HEAD"], root)
        if counts.returncode == 0 and counts.stdout.strip().isdigit():
            ahead = int(counts.stdout.strip())

        return GitStatus(is_repo=True, dirty_files=files, ahead=ahead)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitStatus(is_repo=False, dirty_files=[], ahead=0, error=str(exc))


def pull(root: Path = Path(".")) -> str | None:
    """Catch up with the daily workflow's commits. Returns an error, or None.

    Failure is not fatal — being offline shouldn't stop you editing your
    watchlist — so the caller reports it and carries on.
    """
    result = _run(["pull", "--rebase", "--autostash"], root)
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or "git pull failed"
    return None


def commit_and_push(message: str, root: Path = Path(".")) -> tuple[bool, str]:
    """Commit the tracked config and data, then push. Returns (ok, message)."""
    add = _run(["add", "--", *TRACKED], root)
    if add.returncode != 0:
        return False, (add.stderr or "git add failed").strip()

    staged = _run(["diff", "--staged", "--quiet"], root)
    if staged.returncode == 0:
        # Nothing new staged, but there may still be local commits to push.
        push = _run(["push"], root)
        if push.returncode != 0:
            return False, (push.stderr or "git push failed").strip()
        return True, "Nothing to commit; pushed existing commits."

    commit = _run(["commit", "-m", message], root)
    if commit.returncode != 0:
        return False, (commit.stderr or commit.stdout or "git commit failed").strip()

    push = _run(["push"], root)
    if push.returncode != 0:
        # A rebase-and-retry covers the common case: CI pushed while you edited.
        rebased = _run(["pull", "--rebase", "--autostash"], root)
        if rebased.returncode != 0:
            return False, (rebased.stderr or "git pull --rebase failed").strip()
        push = _run(["push"], root)
        if push.returncode != 0:
            return False, (push.stderr or "git push failed").strip()

    return True, "Pushed to GitHub."
