# SPDX-License-Identifier: Apache-2.0
"""ArtifactFS lifecycle and native ignored directories, using the VM's mount helper."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import signal
import socket
import subprocess
import sys
import time

DEFAULT_LOCAL = ("node_modules", ".next", ".cache", ".turbo", ".venv", "dist", "build")


def run_git(target, *args, **kwargs):
    return subprocess.run(
        ["git", "-C", str(target), *args], capture_output=True, **kwargs
    )


def mount_command(*args):
    command = list(args) if os.geteuid() == 0 else ["sudo", "-n", *args]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def native_dirs(target, state, requested, installed):
    # Every tracked package.json gets its own native node_modules when ignored.
    files = run_git(
        target, "ls-tree", "-r", "--name-only", "-z", "HEAD", check=True
    ).stdout
    names = files.decode("utf-8", "surrogateescape").split("\0")
    package_dirs = {
        str(PurePosixPath(n).parent / "node_modules")
        for n in names
        if PurePosixPath(n).name == "package.json"
    }
    requested = {str(PurePosixPath(name)) for name in requested}
    candidates = set(DEFAULT_LOCAL) | package_dirs | requested
    for name in sorted(candidates):
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or ".git" in path.parts
        ):
            raise ValueError("local directories must be relative paths outside .git")
        # A tracked descendant or a tracked file at this path must never be hidden.
        if any(n == name or n.startswith(name.rstrip("/") + "/") for n in names):
            if name in requested:
                raise ValueError(f"local directory contains tracked files: {name}")
            continue
        ignored = (
            run_git(target, "check-ignore", "-q", "--", name + "/").returncode == 0
        )
        if not ignored:
            if name in requested:
                raise ValueError(f"add {name}/ to .gitignore before making it native")
            continue
        destination = state / "local" / path
        destination.mkdir(parents=True, exist_ok=True)
        link = target / path
        # Refuse symlink parents: never follow a repository-controlled path out of the mount.
        for parent in path.parents:
            if str(parent) != "." and (target / parent).is_symlink():
                raise ValueError(f"local directory has a symlink parent: {name}")
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or (
            link.exists() and (not link.is_dir() or any(link.iterdir()))
        ):
            if name in requested:
                raise ValueError(
                    f"refusing to replace existing local directory: {name}"
                )
            continue
        link.mkdir(exist_ok=True)
        mount_command("mount", "--bind", str(destination), str(link))
        installed.append(name)
    return installed


def state_for(target, state_root=None):
    root = (
        Path(
            state_root
            or os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
        )
        .expanduser()
        .resolve()
    )
    return (
        root
        / "git-s3-workspaces"
        / hashlib.sha256(os.fsencode(target)).hexdigest()[:20]
    )


def socket_path(state):
    # Unix socket paths have a small fixed limit; state roots may be very long.
    runtime = Path("/tmp") / f"git-s3-workspaces-{os.getuid()}"
    runtime.mkdir(mode=0o700, exist_ok=True)
    info = runtime.lstat()
    if runtime.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(
            "workspace runtime directory must be private and owned by this user"
        )
    return runtime / (hashlib.sha256(os.fsencode(state)).hexdigest()[:32] + ".sock")


def control(state, message):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5)
        sock.connect(str(socket_path(state)))
        sock.sendall(message.encode())
        return json.loads(sock.recv(64 * 1024))


def serve(args, target, state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    with (state / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = dict(
            os.environ,
            ARTIFACT_FS_ROOT=str(state / "artifact-fs"),
            ARTIFACT_FS_LOCAL_MOUNTS="1",
        )
        if target.exists() and any(target.iterdir()) and not os.path.ismount(target):
            raise ValueError("mount target must be absent or empty")
        if os.path.ismount(target):
            raise ValueError("target is already mounted")
        target.parent.mkdir(parents=True, exist_ok=True)
        identity = {"remote": args.remote, "ref": args.ref}
        config = state / "workspace.json"
        if config.exists() and json.loads(config.read_text()) != identity:
            raise ValueError(
                "workspace state belongs to another remote or ref; use a new target"
            )
        config.write_text(json.dumps(identity))
        subprocess.run(
            [
                "artifact-fs",
                "add-repo",
                "--name",
                target.name,
                "--remote",
                args.remote,
                "--ref",
                args.ref,
                "--refresh",
                "never",
                "--mount-root",
                str(target.parent),
            ],
            check=True,
            env=env,
        )
        daemon = subprocess.Popen(
            ["artifact-fs", "daemon", "--root", str(target.parent)], env=env
        )
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        sockpath = socket_path(state)
        installed = []
        try:
            deadline = time.monotonic() + args.timeout
            while not os.path.ismount(target):
                if daemon.poll() is not None or stopping:
                    raise ValueError(
                        "ArtifactFS exited before mounting; see workspace.log"
                    )
                if time.monotonic() >= deadline:
                    raise ValueError("ArtifactFS mount timeout")
                time.sleep(0.1)
            native_dirs(target, state, args.local_dir, installed)
            sockpath.unlink(missing_ok=True)
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(sockpath))
                server.listen(4)
                while not stopping and daemon.poll() is None:
                    if not select.select([server], [], [], 0.5)[0]:
                        continue
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(2)
                        command = conn.recv(32).decode()
                        response = {
                            "ready": True,
                            "target": str(target),
                            "native_dirs": list(installed),
                        }
                        if command == "stop":
                            try:
                                while installed:
                                    mount_command("umount", str(target / installed[-1]))
                                    installed.pop()
                                mount_command("umount", str(target))
                                stopping = True
                            except subprocess.CalledProcessError:
                                response["error"] = (
                                    "workspace is busy; close processes using native directories and retry stop"
                                )
                        conn.sendall(json.dumps(response).encode())
        finally:
            sockpath.unlink(missing_ok=True)
            # Unmount nested native paths before terminating their FUSE parent.
            for name in reversed(installed):
                mount_command("umount", str(target / name))
            if os.path.ismount(target):
                mount_command("umount", str(target))
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
            # A killed daemon can leave a disconnected FUSE mount.
            if os.path.ismount(target):
                mount_command("umount", str(target))


def main():
    parser = argparse.ArgumentParser(
        description="Mount S3 Git lazily with ArtifactFS; keep ignored dependency directories on native disk"
    )
    parser.add_argument("command", choices=("mount", "serve", "status", "stop"))
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--ref", default="refs/heads/main")
    parser.add_argument("--local-dir", action="append", default=[])
    parser.add_argument("--state-root")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if len(args.paths) != (2 if args.command in ("mount", "serve") else 1):
        parser.error("mount requires REMOTE TARGET; status/stop require TARGET")
    args.remote = args.paths[0] if len(args.paths) == 2 else None
    # resolve the parent only: the target itself may be a disconnected FUSE mount.
    raw = Path(args.paths[-1]).absolute()
    target = raw.parent.resolve() / raw.name
    if target.name in ("", ".", "..") or target == Path("/"):
        parser.error("invalid target")
    state = state_for(target, args.state_root)
    try:
        if args.command in ("status", "stop"):
            result = control(state, args.command)
            if result.get("error"):
                raise ValueError(result["error"])
            print(json.dumps(result))
            if args.command == "stop":
                deadline = time.monotonic() + args.timeout
                with (state / "lock").open("a") as lock:
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise ValueError("workspace stop timed out")
                            time.sleep(0.1)
                if os.path.ismount(target):
                    raise ValueError(
                        "workspace is still busy; close processes and retry stop"
                    )
        elif args.command == "serve":
            serve(args, target, state)
        else:
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                status = control(state, "status")
            except (OSError, ValueError):
                status = None
            if status:
                raise ValueError("workspace is already running")
            with (state / "workspace.log").open("a") as log:
                child_args = sys.argv[1:]
                child_args[0] = "serve"
                child = subprocess.Popen(
                    [sys.executable, "-m", "git_remote_s3.workspace", *child_args],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + args.timeout
            while child.poll() is None and time.monotonic() < deadline:
                try:
                    print(json.dumps(control(state, "status")))
                    return
                except (OSError, ValueError):
                    time.sleep(0.1)
            child.terminate()
            raise ValueError(f"workspace startup failed; see {state / 'workspace.log'}")
    except Exception as e:
        print(f"git-s3-workspace: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
