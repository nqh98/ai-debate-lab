"""Disposable git-worktree workspaces for repo-grounded debates.

The pin ({source, commit}) is recorded at `debate new`; the checkout is
materialized at `debate run` and removed when a human decision lands.
See specs/2026-07-15-workspace-grounded-debates-design.md §1-2.
"""
import subprocess
from pathlib import Path

GIT_TIMEOUT = 60  # metadata/worktree commands, not agent work


class WorkspaceError(Exception):
    """The workspace could not be pinned or materialized."""


def _git(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True,
            timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise WorkspaceError("git is not on PATH")
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {' '.join(args)}: timed out")
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)}: {proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip()


def pin(source: str) -> dict:
    """Resolve a repo path to the {source, commit} record `debate new`
    stores. The commit is HEAD now: citations must survive later pushes."""
    src = Path(source).expanduser().resolve()
    if not src.is_dir():
        raise WorkspaceError(f"not a directory: {src}")
    commit = _git(["-C", str(src), "rev-parse", "HEAD"])
    return {"source": str(src), "commit": commit}
