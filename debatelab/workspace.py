"""Disposable git-worktree workspaces for repo-grounded debates.

The pin ({source, commit}) is recorded at `debate new`; the checkout is
materialized at `debate run` and removed when a human decision lands.
See specs/2026-07-15-workspace-grounded-debates-design.md §1-2.
"""
import shutil
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


def materialize(workspace: dict, debate_dir: Path) -> tuple[Path, bool]:
    """Ensure debates/<id>/workspace/ is a checkout of the pinned commit.

    Returns (path, created_now). An existing directory is reused as-is:
    agent scratch (caches, build output) is harmless in a disposable tree,
    and recreating mid-debate would discard nothing of record (spec §2)."""
    target = Path(debate_dir) / "workspace"
    if target.exists():
        return target, False
    source = workspace["source"]
    if not Path(source).is_dir():
        raise WorkspaceError(f"workspace source repo is gone: {source}")
    _git([
        "-C", source, "worktree", "add", "--detach",
        str(target), workspace["commit"],
    ])
    return target, True


def remove(workspace: dict, debate_dir: Path) -> str | None:
    """Tear the worktree down; best-effort, never an error (spec §2).

    Returns a warning string when only the local directory could be
    removed and the source repo keeps a stale registration."""
    target = Path(debate_dir) / "workspace"
    if not target.exists():
        return None
    try:
        _git([
            "-C", workspace["source"], "worktree", "remove", "--force",
            str(target),
        ])
        return None
    except WorkspaceError as e:
        shutil.rmtree(target, ignore_errors=True)
        return (
            f"workspace deleted, but source cleanup failed ({e}); "
            f"run `git worktree prune` in {workspace['source']}"
        )
