import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import boto3
import pytest
from moto.server import ThreadedMotoServer
from git_remote_s3.lazy import LazyRemote


@pytest.fixture
def storage(tmp_path, monkeypatch):
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    monkeypatch.setenv("AWS_ENDPOINT_URL", f"http://{host}:{port}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    import urllib.request

    urllib.request.urlopen(
        urllib.request.Request(f"http://{host}:{port}/moto-api/reset", method="POST")
    ).close()
    client = boto3.client("s3")
    client.create_bucket(Bucket="test-bucket")
    yield client
    server.stop()


def run(repo, *args, check=True, **kwargs):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, **kwargs
    )
    if check and result.returncode:
        raise AssertionError(f"git {args}: {result.stderr.decode()}")
    return result


def source(tmp_path):
    path = tmp_path / "source"
    path.mkdir()
    run(path, "init", "-b", "main")
    run(path, "config", "user.email", "test@example.test")
    run(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    (path / "large.bin").write_bytes(bytes(range(256)) * 8192)
    (path / ".gitignore").write_text("node_modules/\n.cache/\n")
    run(path, "add", ".")
    run(path, "commit", "-m", "initial")
    run(path, "remote", "add", "origin", "s3+lazy://test-bucket/repos/p1")
    run(path, "push", "-u", "origin", "HEAD:refs/heads/main")
    return path


def clone(tmp_path, path="clone", filtered=True):
    target = tmp_path / path
    args = ["clone", "--no-checkout"]
    if filtered:
        args += ["--filter=blob:none"]
    subprocess.run(
        ["git", *args, "s3+lazy://test-bucket/repos/p1", str(target)],
        capture_output=True,
        check=True,
    )
    return target


def test_blobless_clone_hydrates_exact_binary(storage, tmp_path):
    src = source(tmp_path)
    dst = clone(tmp_path)
    oid = run(src, "rev-parse", "HEAD:large.bin").stdout.strip().decode()
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
    assert run(dst, "cat-file", "-e", oid, env=env, check=False).returncode != 0
    # Only the explicitly requested blob becomes present in Git's object store.
    assert run(dst, "show", "HEAD:README.md").stdout == b"hello\n"
    assert run(dst, "cat-file", "-e", oid, env=env, check=False).returncode != 0
    assert run(dst, "show", "HEAD:large.bin").stdout == (src / "large.bin").read_bytes()
    run(dst, "fsck", "--full")
    keys = [o["Key"] for o in storage.list_objects_v2(Bucket="test-bucket")["Contents"]]
    assert all(k.startswith("repos/p1/") for k in keys)


def test_regular_clone_checkout_and_incremental_push(storage, tmp_path):
    src = source(tmp_path)
    dst = clone(tmp_path, filtered=False)
    run(dst, "reset", "--hard", "HEAD")
    assert (dst / "large.bin").read_bytes() == (src / "large.bin").read_bytes()
    run(dst, "config", "user.email", "test@example.test")
    run(dst, "config", "user.name", "Test")
    (dst / "README.md").write_text("updated\n")
    run(dst, "add", "README.md")
    run(dst, "commit", "-m", "change")
    run(dst, "push", "origin", "HEAD:refs/heads/main")
    run(src, "pull", "--ff-only")
    assert (src / "README.md").read_text() == "updated\n"


def test_partial_clone_push_does_not_hydrate_old_blobs(storage, tmp_path, monkeypatch):
    src = source(tmp_path)
    dst = clone(tmp_path)
    run(dst, "config", "user.email", "test@example.test")
    run(dst, "config", "user.name", "Test")
    # Use plumbing to change one path without checking out the rest.
    run(dst, "read-tree", "HEAD")
    (dst / "README.md").write_text("changed\n")
    blob = (
        run(dst, "hash-object", "-w", "--stdin", input=b"changed\n")
        .stdout.strip()
        .decode()
    )
    run(dst, "update-index", "--cacheinfo", "100644," + blob + ",README.md")
    tree = run(dst, "write-tree", "--missing-ok").stdout.strip().decode()
    parent = run(dst, "rev-parse", "HEAD").stdout.strip().decode()
    commit = (
        run(dst, "commit-tree", tree, "-p", parent, input=b"only one file\n")
        .stdout.strip()
        .decode()
    )
    run(dst, "update-ref", "refs/heads/main", commit)
    monkeypatch.chdir(dst)
    LazyRemote("s3+lazy://test-bucket/repos/p1", client=storage).push(
        ["push HEAD:refs/heads/main"]
    )
    oid = run(src, "rev-parse", "HEAD:large.bin").stdout.strip().decode()
    assert (
        run(
            dst,
            "cat-file",
            "-e",
            oid,
            env=dict(os.environ, GIT_NO_LAZY_FETCH="1"),
            check=False,
        ).returncode
        != 0
    )
    fresh = clone(tmp_path, "fresh", filtered=False)
    assert run(fresh, "show", "HEAD:README.md").stdout == b"changed\n"
    assert (
        run(fresh, "show", "HEAD:large.bin").stdout == (src / "large.bin").read_bytes()
    )


def test_concurrent_writers_compare_and_swap(storage, tmp_path, monkeypatch):
    src = source(tmp_path)
    monkeypatch.chdir(src)
    winner = LazyRemote("s3+lazy://test-bucket/repos/p1", client=storage)
    loser = LazyRemote("s3+lazy://test-bucket/repos/p1", client=storage)
    winner.push(["push HEAD:refs/heads/winner"])
    with pytest.raises(ValueError, match="stale remote"):
        loser.push(["push HEAD:refs/heads/loser"])
    manifest, _ = winner.read_manifest()
    assert "refs/heads/winner" in manifest["refs"]
    assert "refs/heads/loser" not in manifest["refs"]


def test_fetch_corrupt_blob_fails_closed(storage, tmp_path):
    src = source(tmp_path)
    oid = run(src, "rev-parse", "HEAD:large.bin").stdout.strip().decode()
    storage.put_object(
        Bucket="test-bucket",
        Key=f"repos/p1/v2/objects/{oid[:2]}/{oid[2:]}",
        Body=b"corrupt",
    )
    dst = clone(tmp_path)
    assert run(dst, "show", "HEAD:large.bin", check=False).returncode != 0
    assert not (dst / ".git/objects" / oid[:2] / oid[2:]).exists()


def test_non_fast_forward_and_ref_deletion(storage, tmp_path):
    src = source(tmp_path)
    run(src, "push", "origin", "HEAD:refs/heads/other")
    run(src, "push", "origin", ":refs/heads/other")
    result = run(src, "ls-remote", "origin").stdout
    assert b"refs/heads/other" not in result
    run(src, "checkout", "--orphan", "unrelated")
    run(src, "commit", "-m", "unrelated")
    assert (
        run(src, "push", "origin", "HEAD:refs/heads/main", check=False).returncode != 0
    )
    run(src, "push", "--force", "origin", "HEAD:refs/heads/main")
