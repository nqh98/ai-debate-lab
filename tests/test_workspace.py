"""Workspace pinning: --repo resolves to {source, commit} (spec §1)."""
import shutil
import subprocess

import pytest

from debatelab import workspace


def make_repo(tmp_path, name="src"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return repo


def test_pin_resolves_source_and_head(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    assert ws["source"] == str(repo.resolve())
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ws["commit"] == head


def test_pin_rejects_non_directory(tmp_path):
    with pytest.raises(workspace.WorkspaceError):
        workspace.pin(str(tmp_path / "missing"))


def test_pin_rejects_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(workspace.WorkspaceError):
        workspace.pin(str(plain))


def test_materialize_creates_worktree_at_pinned_commit(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    # advance HEAD past the pin: the worktree must ignore the new commit
    (repo / "later.txt").write_text("later\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "later"],
        check=True,
    )
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    path, created = workspace.materialize(ws, debate_dir)
    assert created is True
    assert path == debate_dir / "workspace"
    assert (path / "hello.txt").exists()
    assert not (path / "later.txt").exists()


def test_materialize_reuses_existing_worktree(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    workspace.materialize(ws, debate_dir)
    (debate_dir / "workspace" / "scratch.txt").write_text("agent litter\n")
    path, created = workspace.materialize(ws, debate_dir)
    assert created is False
    assert (path / "scratch.txt").exists()


def test_materialize_missing_source_raises(tmp_path):
    ws = {"source": str(tmp_path / "gone"), "commit": "a" * 40}
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    with pytest.raises(workspace.WorkspaceError):
        workspace.materialize(ws, debate_dir)


def test_remove_deletes_worktree_and_registration(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    workspace.materialize(ws, debate_dir)
    assert workspace.remove(ws, debate_dir) is None
    assert not (debate_dir / "workspace").exists()
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "debate/workspace" not in listed


def test_remove_missing_workspace_is_noop(tmp_path):
    ws = {"source": str(tmp_path), "commit": "a" * 40}
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    assert workspace.remove(ws, debate_dir) is None


def test_remove_falls_back_when_source_is_gone(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    workspace.materialize(ws, debate_dir)
    shutil.rmtree(repo)
    warning = workspace.remove(ws, debate_dir)
    assert warning is not None and "prune" in warning
    assert not (debate_dir / "workspace").exists()
