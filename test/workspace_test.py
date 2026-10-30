import subprocess
import pytest
from git_remote_s3.workspace import native_dirs


def test_native_directory_guards_normalize_and_preserve_tracked_files(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-C", str(target), *args], check=True, capture_output=True
        )

    git("init")
    git("config", "user.email", "test@example.test")
    git("config", "user.name", "Test")
    (target / "tracked").mkdir()
    (target / "tracked/file").write_text("must stay visible")
    (target / ".gitignore").write_text("tracked/\n")
    git("add", ".gitignore")
    git("add", "-f", "tracked/file")
    git("commit", "-m", "fixture")
    with pytest.raises(ValueError, match="tracked files"):
        native_dirs(target, tmp_path / "state", ["./tracked"], [])
    for path in ("../escape", ".git/objects", "/tmp/outside"):
        with pytest.raises(ValueError, match="relative paths"):
            native_dirs(target, tmp_path / "state", [path], [])
    with pytest.raises(ValueError, match="gitignore"):
        native_dirs(target, tmp_path / "state", ["unignored"], [])
    assert (target / "tracked/file").read_text() == "must stay visible"
