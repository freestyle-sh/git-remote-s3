"""Real Linux FUSE + S3 + npm tests. Run AFS_RUN_E2E_TESTS=1 pytest -q test/fuse_test.py."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from lazy_test import storage, source, run

pytestmark = pytest.mark.skipif(
    os.environ.get("AFS_RUN_E2E_TESTS") != "1",
    reason="requires Linux FUSE and mount privilege",
)


def workspace(*args, **kwargs):
    result = subprocess.run(
        ["git-s3-workspace", *map(str, args)], capture_output=True, text=True, **kwargs
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def test_s3_fuse_workflow_and_native_dependencies(storage, tmp_path):
    src = source(tmp_path)
    (src / "package.json").write_text(
        json.dumps({"name": "native-test", "private": True})
    )
    (src / "dep").mkdir()
    (src / "dep/package.json").write_text(
        json.dumps({"name": "local-dependency", "version": "1.0.0", "main": "index.js"})
    )
    (src / "dep/index.js").write_text("module.exports = 42;\n")
    run(src, "add", ".")
    run(src, "commit", "-m", "test package")
    run(src, "push", "origin", "HEAD:refs/heads/main")
    target = tmp_path / "mounted"
    state_root = tmp_path / "state"
    options = ["--state-root", state_root]
    status = workspace("mount", "s3+lazy://test-bucket/repos/p1", target, *options)
    assert "node_modules" in status["native_dirs"]
    try:
        # A real mount, not a symlink, and kernel operations use the local filesystem.
        kind = subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(target / "node_modules")],
            text=True,
        ).strip()
        assert "fuse" not in kind
        assert not (target / "node_modules").is_symlink()
        assert (target / "README.md").read_text() == "hello\n"
        assert (target / "large.bin").read_bytes() == (src / "large.bin").read_bytes()
        subprocess.run(
            ["npm", "install", "./dep", "--no-audit", "--no-fund"],
            cwd=target,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=target,
            check=True,
            capture_output=True,
        )
        assert (
            subprocess.check_output(
                ["node", "-e", "console.log(require('local-dependency'))"], cwd=target
            ).strip()
            == b"42"
        )
        # npm's local relative links work across the bind mount.
        run(target, "config", "user.email", "test@example.test")
        run(target, "config", "user.name", "Test")
        (target / "README.md").write_text("edited through FUSE\n")
        run(target, "add", "README.md", "package.json", "package-lock.json")
        run(target, "commit", "-m", "mounted edit")
        run(target, "push", "origin", "HEAD:refs/heads/main")
        assert (
            "node_modules" not in run(target, "status", "--porcelain").stdout.decode()
        )
        count = 3000

        def bench(path):
            path.mkdir(exist_ok=True)
            start = time.perf_counter()
            for i in range(count):
                (path / str(i)).write_bytes(b"x" * 128)
            for i in range(count):
                assert (path / str(i)).read_bytes() == b"x" * 128
            for i in range(count):
                (path / str(i)).unlink()
            return time.perf_counter() - start

        native = bench(target / "node_modules/benchmark")
        baseline = bench(tmp_path / "baseline")
        print(
            json.dumps(
                {
                    "files": count,
                    "native_bind_seconds": native,
                    "local_baseline_seconds": baseline,
                    "ratio": native / baseline,
                    "fstype": kind,
                }
            )
        )
        assert native < baseline * 3 + 0.2
    finally:
        workspace("stop", target, *options)
    # Native dependencies and tracked edits survive daemon restarts.
    workspace("mount", "s3+lazy://test-bucket/repos/p1", target, *options)
    try:
        assert (target / "README.md").read_text() == "edited through FUSE\n"
        assert (
            subprocess.check_output(
                ["node", "-e", "console.log(require('local-dependency'))"], cwd=target
            ).strip()
            == b"42"
        )
    finally:
        workspace("stop", target, *options)
