"""Workspace pinning: --repo resolves to {source, commit} (spec §1)."""
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
