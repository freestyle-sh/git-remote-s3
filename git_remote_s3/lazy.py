# SPDX-License-Identifier: Apache-2.0
"""Object-backed S3 remote. Bundle repositories keep using git-remote-s3.

v2/refs.json is the only mutable object. S3 conditional writes publish refs
only after all immutable blobs and the blobless metadata pack are durable.
"""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
import zlib

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .common import parse_git_url
from .enums import UriScheme

OID = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def git(*args, input=None, **kwargs):
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
    return subprocess.run(
        ["git", *args],
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env=env,
        **kwargs,
    ).stdout


def valid_ref(ref):
    return (
        ref.startswith(("refs/heads/", "refs/tags/"))
        and subprocess.run(
            ["git", "check-ref-format", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


class LazyRemote:
    def __init__(self, url, name="origin", client=None):
        scheme, profile, self.bucket, prefix = parse_git_url(url)
        if scheme != UriScheme.S3_LAZY or not self.bucket or not prefix:
            raise ValueError("expected s3+lazy://bucket/repository-prefix")
        self.prefix = prefix.rstrip("/") + "/v2/"
        self.name = name
        self.client = client or boto3.Session(profile_name=profile).client(
            "s3",
            config=Config(
                max_pool_connections=16, request_checksum_calculation="when_required"
            ),
        )
        self.filtered = False
        self.manifest, self.etag = self.read_manifest()

    def read_manifest(self):
        try:
            obj = self.client.get_object(
                Bucket=self.bucket, Key=self.prefix + "refs.json"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return {"version": 2, "refs": {}, "head": None}, None
            raise
        with obj["Body"] as body:
            data = json.load(body)
        if data.get("version") != 2 or not isinstance(data.get("refs"), dict):
            raise ValueError("unsupported S3 lazy manifest")
        for ref, entry in data["refs"].items():
            if (
                not valid_ref(ref)
                or not OID.fullmatch(entry["oid"])
                or not DIGEST.fullmatch(entry["pack"])
            ):
                raise ValueError("invalid S3 lazy ref")
        if data.get("head") is not None and data["head"] not in data["refs"]:
            raise ValueError("invalid S3 lazy HEAD")
        return data, obj["ETag"]

    def download(self, key, path):
        # download_file can HEAD before GET; streaming GET also works with a read-only rule.
        obj = self.client.get_object(Bucket=self.bucket, Key=self.prefix + key)
        with obj["Body"] as body, open(path, "wb") as out:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                out.write(chunk)

    def fetch_metadata(self, entry):
        with tempfile.TemporaryDirectory(prefix="s3-metadata-") as tmp:
            path = Path(tmp) / "metadata.pack"
            self.download("metadata/" + entry["pack"] + ".pack", path)
            with path.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if digest != entry["pack"]:
                raise ValueError("S3 metadata pack checksum mismatch")
            with path.open("rb") as f:
                git("index-pack", "--stdin", "--promisor", stdin=f)

    def fetch_blob(self, oid):
        if not OID.fullmatch(oid):
            raise ValueError("invalid Git object ID")
        objdir = Path(os.fsdecode(git("rev-parse", "--git-path", "objects")).strip())
        target = objdir / oid[:2] / oid[2:]
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".s3-", dir=target.parent) as tmp:
            path = Path(tmp) / "object"
            self.download("objects/" + oid[:2] + "/" + oid[2:], path)
            # Validate the entire loose object before installing it in Git's object database.
            decoder = zlib.decompressobj()
            digest = hashlib.sha1()
            header = b""
            size = 0
            expected = None
            with path.open("rb") as f:
                for compressed in iter(lambda: f.read(64 * 1024), b""):
                    # Bound decompression memory even for highly compressible blobs.
                    while compressed:
                        raw = decoder.decompress(compressed, 1024 * 1024)
                        compressed = decoder.unconsumed_tail
                        digest.update(raw)
                        if expected is None:
                            header += raw
                            if b"\0" not in header:
                                if len(header) > 128:
                                    raise ValueError("invalid Git object header")
                                continue
                            hdr, raw = header.split(b"\0", 1)
                            kind, length = hdr.split(b" ", 1)
                            if kind != b"blob" or not length.isdigit():
                                raise ValueError("invalid Git blob header")
                            expected = int(length)
                            header = b""
                        size += len(raw)
            if (
                not decoder.eof
                or decoder.unused_data
                or size != expected
                or digest.hexdigest() != oid
            ):
                raise ValueError("S3 Git blob checksum mismatch")
            os.replace(path, target)

    def fetch(self, commands):
        blobs = set()
        entries = {entry["oid"]: entry for entry in self.manifest["refs"].values()}
        for command in commands:
            _, oid, ref = command.split(" ", 2)
            if not OID.fullmatch(oid):
                raise ValueError("invalid fetch object ID")
            if oid in entries:
                self.fetch_metadata(entries[oid])
                if not self.filtered:
                    missing = (
                        git("rev-list", "--objects", "--missing=print", oid)
                        .decode()
                        .splitlines()
                    )
                    blobs.update(line[1:] for line in missing if line.startswith("?"))
            else:
                # Git's promisor fetch asks for blob OIDs directly (without a named ref).
                blobs.add(oid)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(self.fetch_blob, sorted(blobs)))

    def upload_blobs(self, oid, old_oids):
        # Excluding advertised remote commits keeps subsequent pushes incremental and
        # avoids downloading promised blobs that are already durable on S3.
        revisions = oid + "\n" + "".join("^" + old + "\n" for old in old_oids)
        objects = (
            git(
                "rev-list",
                "--objects",
                "--no-object-names",
                "--missing=print",
                "--stdin",
                input=revisions.encode(),
            )
            .decode()
            .splitlines()
        )
        if any(obj.startswith("?") for obj in objects):
            raise ValueError(
                "new history contains missing objects; hydrate it before pushing"
            )
        with tempfile.TemporaryDirectory(prefix="s3-push-") as tmp:
            # cat-file is binary-safe and one long-lived process serves all new objects.
            with tempfile.TemporaryFile() as requested:
                requested.write(("\n".join(objects) + "\n").encode())
                requested.seek(0)
                proc = subprocess.Popen(
                    ["git", "cat-file", "--batch"],
                    stdin=requested,
                    stdout=subprocess.PIPE,
                    env=dict(os.environ, GIT_NO_LAZY_FETCH="1"),
                )
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                        pending = []
                        for object_id in objects:
                            line = proc.stdout.readline().decode().split()
                            if len(line) != 3 or line[0] != object_id:
                                raise ValueError("missing local Git object")
                            kind, remaining = line[1], int(line[2])
                            path = Path(tmp) / object_id
                            with path.open("wb") as out:
                                compressor = zlib.compressobj()
                                out.write(
                                    compressor.compress(
                                        f"{kind} {remaining}\0".encode()
                                    )
                                )
                                while remaining:
                                    raw = proc.stdout.read(min(1024 * 1024, remaining))
                                    if not raw:
                                        raise ValueError("truncated Git object")
                                    remaining -= len(raw)
                                    out.write(compressor.compress(raw))
                                out.write(compressor.flush())
                            if proc.stdout.read(1) != b"\n":
                                raise ValueError("invalid cat-file framing")
                            if kind == "blob":
                                pending.append(
                                    pool.submit(self.upload_blob, object_id, path)
                                )
                            else:
                                path.unlink()
                            # Bound both disk usage and the number of queued uploads.
                            if len(pending) >= 16:
                                for future in pending:
                                    future.result()
                                pending.clear()
                        for future in pending:
                            future.result()
                    if proc.wait() != 0:
                        raise ValueError("git cat-file failed")
                finally:
                    proc.stdout.close()
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()

    def upload_blob(self, oid, path):
        self.client.upload_file(
            str(path), self.bucket, self.prefix + "objects/" + oid[:2] + "/" + oid[2:]
        )
        path.unlink()

    def push(self, commands):
        # A single CAS publishes the whole batch; unsupported --atomic is rejected by Git.
        updated = json.loads(json.dumps(self.manifest))
        old_oids = [entry["oid"] for entry in self.manifest["refs"].values()]
        for entry in self.manifest["refs"].values():
            self.fetch_metadata(entry)
        for command in commands:
            source, dest = command.removeprefix("push ").split(":", 1)
            if not valid_ref(dest):
                raise ValueError("only refs/heads/* and refs/tags/* are supported")
            force = source.startswith("+")
            source = source.removeprefix("+")
            if not source:
                updated["refs"].pop(dest, None)
                continue
            oid = git("rev-parse", "--verify", source).decode().strip()
            if not OID.fullmatch(oid):
                raise ValueError("only SHA-1 repositories are supported")
            old = self.manifest["refs"].get(dest)
            if old and old["oid"] != oid and not force:
                if dest.startswith("refs/tags/"):
                    raise ValueError("tag exists; use --force to replace it")
                git("merge-base", "--is-ancestor", old["oid"], oid)
            if dest.startswith("refs/heads/"):
                git("cat-file", "-e", oid + "^{commit}")
            self.upload_blobs(oid, old_oids)
            with tempfile.TemporaryFile() as pack:
                subprocess.run(
                    ["git", "pack-objects", "--stdout", "--revs", "--filter=blob:none"],
                    input=(oid + "\n").encode(),
                    stdout=pack,
                    check=True,
                    env=dict(os.environ, GIT_NO_LAZY_FETCH="1"),
                )
                pack.seek(0)
                digest = hashlib.file_digest(pack, "sha256").hexdigest()
                pack.seek(0)
                self.client.upload_fileobj(
                    pack, self.bucket, self.prefix + "metadata/" + digest + ".pack"
                )
            updated["refs"][dest] = {"oid": oid, "pack": digest}
        if updated.get("head") not in updated["refs"]:
            updated["head"] = next(
                (r for r in updated["refs"] if r.startswith("refs/heads/")), None
            )
        updated["transaction"] = uuid.uuid4().hex
        condition = {"IfMatch": self.etag} if self.etag else {"IfNoneMatch": "*"}
        try:
            result = self.client.put_object(
                Bucket=self.bucket,
                Key=self.prefix + "refs.json",
                Body=json.dumps(updated).encode(),
                **condition,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in (
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
                "409",
            ):
                raise ValueError("stale remote; fetch, merge, and retry") from e
            raise
        self.manifest, self.etag = updated, result["ETag"]

    def run(self, stdin, stdout):
        pending = []
        for line in stdin:
            command = line.rstrip("\n")
            if command == "capabilities":
                stdout.write("fetch\npush\noption\n\n")
            elif command.startswith("option "):
                _, option, value = command.split(" ", 2)
                if option == "filter" and value == "blob:none":
                    self.filtered = True
                    stdout.write("ok\n")
                elif option in (
                    "verbosity",
                    "progress",
                    "cloning",
                    "followtags",
                    "check-connectivity",
                ):
                    stdout.write("ok\n")
                else:
                    stdout.write("unsupported\n")
            elif command in ("list", "list for-push"):
                if command == "list" and self.manifest.get("head"):
                    stdout.write("@" + self.manifest["head"] + " HEAD\n")
                for ref, entry in self.manifest["refs"].items():
                    stdout.write(entry["oid"] + " " + ref + "\n")
                stdout.write("\n")
            elif command.startswith(("fetch ", "push ")):
                pending.append(command)
            elif not command and pending:
                if pending[0].startswith("fetch "):
                    self.fetch(pending)
                else:
                    try:
                        self.push(pending)
                        for cmd in pending:
                            stdout.write("ok " + cmd.split(":", 1)[1] + "\n")
                    except Exception as e:
                        # No SDK exception text: it can contain endpoints and credential metadata.
                        message = (
                            str(e) if isinstance(e, ValueError) else type(e).__name__
                        )
                        for cmd in pending:
                            stdout.write(
                                "error "
                                + cmd.split(":", 1)[1]
                                + " "
                                + message.replace("\n", " ")
                                + "\n"
                            )
                pending.clear()
                stdout.write("\n")
            elif not command:
                break
            else:
                raise ValueError("unsupported remote-helper command")
            stdout.flush()


def main():
    try:
        LazyRemote(sys.argv[2], sys.argv[1]).run(sys.stdin, sys.stdout)
    except Exception as e:
        message = str(e) if isinstance(e, ValueError) else type(e).__name__
        print("fatal: S3 lazy remote: " + message, file=sys.stderr)
        sys.exit(1)
