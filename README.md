# S3 Git workspaces

Store Git repositories in S3 and mount a working tree without downloading all
file contents first. Commits and trees arrive up front; files hydrate as tools
read them. Keep ignored dependency and cache directories such as `node_modules`
on native local storage for fast I/O.

This fork combines [awslabs/git-remote-s3](https://github.com/awslabs/git-remote-s3)
with [Cloudflare ArtifactFS](https://github.com/cloudflare/artifact-fs). It adds
an object-backed lazy transport, prefix-scoped listing fixes, and a workspace
command that manages the filesystem daemon and native dependency mounts.

## Get started

For Freestyle's standard Ubuntu workflow, follow the
[S3 Git guide](https://www.freestyle.sh/docs/guides/use-s3-as-a-git-remote-in-a-sandbox).

To install on another Ubuntu VM:

```sh
git clone https://github.com/freestyle-sh/git-remote-s3.git
cd git-remote-s3
git checkout v0.5.0-preview.2
sudo ./install.sh
```

The installer provides Git LFS, FUSE 3, the Python helpers in
`/opt/freestyle/git-s3`, and a pinned ArtifactFS build. Commands are available
in `/usr/local/bin`. Installation requires network access and a Go toolchain
download to build ArtifactFS. It does not start a daemon or mount a repository.

## Seed and mount

Configure boto3 using an ordinary AWS profile, or a Freestyle S3 TLS transform
and placeholder credentials. The endpoint, path-style addressing, CA and
`request_checksum_calculation = when_required` work with both helpers.

```sh
# One-time seed from a complete local checkout.
git remote add lazy s3+lazy://my-bucket/repos/my-repo
git push lazy HEAD:refs/heads/main

# In a fresh Linux VM: starts a background daemon and waits for readiness.
git-s3-workspace mount s3+lazy://my-bucket/repos/my-repo /home/ubuntu/repo \
  --ref refs/heads/main
cd /home/ubuntu/repo
printf "\nEdited in the mounted workspace.\n" >> README.md
git add README.md
git commit -m 'Update README'
git push origin HEAD:refs/heads/main

git-s3-workspace status /home/ubuntu/repo
git-s3-workspace stop /home/ubuntu/repo
```

`lazy` is the local name chosen for the seed remote; use any name you prefer.
The `s3+lazy://` URL scheme selects the lazy S3 transport. Mounted workspaces
use `origin` as their remote name.

Commits and trees arrive before mount; blob contents arrive on reads. This
removes full blob download from workspace startup, not the metadata transfer
or filesystem enumeration. The VM itself can boot before mounting. Large
histories can still have substantial metadata. Directory reads can trigger
ArtifactFS's bounded speculative prefetch. Git commands such as status, grep,
checkout, or diff may read many files and hydrate more blobs. There is no
claim that all Git operations remain lazy.

`git clone --filter=blob:none --no-checkout s3+lazy://…` also works without FUSE.
A normal clone hydrates the working tree. SHA-1 repositories are supported.
Shallow history and arbitrary filter expressions are not currently supported.

## Native dependency I/O

`git-s3-workspace` bind-mounts native directories for ignored `node_modules`,
`.next`, `.cache`, `.turbo`, `.venv`, `dist`, and `build`. Tracked package.json
files are also discovered so monorepos get nested `node_modules` mounts.
Other hot ignored directories can be requested with repeated
`--local-dir packages/web/.next`. Add them to `.gitignore` first.

A tracked path is never hidden. Existing nonempty directories are preserved
and skipped (explicit requests fail). The mounted paths are real directories,
so npm's relative links and Node's resolution keep normal semantics. Operations
below them go directly through the kernel to the local filesystem, bypassing
ArtifactFS and its SQLite overlay. Other ignored files still use the FUSE
overlay; use a native directory for hot caches. Ignoring a path alone does not
make an arbitrary file native.

Native data and ArtifactFS state live under the current user's
`~/.local/share/git-s3-workspaces/` and survive stop/start. Keep that state with
the workspace; it is local VM data, never an S3 push. VM snapshots capture it.
Don't delete the state while mounted. After a reboot, rerun `mount` with the
same URL and path. Stop before deleting or moving a workspace, or removing or
renaming a native mountpoint itself rather than its contents.

Linux bind mounts require root or passwordless `sudo mount`/`sudo umount`.
Freestyle's Ubuntu user has passwordless sudo. The installer enables
`user_allow_other` in `/etc/fuse.conf`; the workspace daemon opts into
`allow_other` only for this workflow and retains kernel Unix permission checks.
Other users can read world-readable mounted files, as on a normal local tree.
Use private directory permissions when isolation between OS users matters.

## Storage and concurrency

The new `s3+lazy://` format is separate from upstream `s3://` bundles:

- `<prefix>/v2/objects/<first two OID characters>/<rest>` stores zlib Git blobs.
- `<prefix>/v2/metadata/<sha256>.pack` stores blobless commit/tree/tag history.
- `<prefix>/v2/refs.json` publishes the refs in one conditional S3 PUT.

All keys stay below `<prefix>/`. Objects and metadata are uploaded before
publishing refs. Publication uses ETag `If-Match` (or `If-None-Match` for a new
repository); a stale writer is rejected. No expiring lock or unconditional
ref deletion is used. The S3-compatible backend must implement conditional
PutObject correctly. The transport has been tested with an S3 emulator and
native MinIO on a real Freestyle VM, including stale-writer rejection and
pause/resume.
Blob OIDs and metadata digests are verified before Git installs downloads.
Uploads/downloads are parallel and stream object bodies. Failed publication can
leave unreachable immutable objects; garbage collection is not yet implemented.
Multi-ref helper batches publish with one CAS; Git's `--atomic` option is not
advertised. Existing ref protection markers in bundle repositories do not apply
to the separate lazy format.

To migrate, clone the bundle remote once, then push each desired ref to a
**new prefix** with `s3+lazy://`. Existing bundles are unchanged and never
silently converted. Old `git-remote-s3` clients cannot read the lazy format.
Use the new helper everywhere that reads or pushes lazy repositories.

Git LFS objects still use `<prefix>/lfs/` with `git-lfs-s3`. ArtifactFS returns
canonical Git blobs and does not apply LFS smudge filters: an unread LFS asset
appears as its pointer. Use a regular clone, `git-lfs-s3 install --remote origin`,
and `git lfs pull` when materialized LFS assets are required. Lazy LFS smudging,
submodule checkout, and noncanonical EOL transforms are not currently supported.

## Validate

```sh
python3 -m venv .venv
.venv/bin/pip install -e . pytest mock 'moto[s3,server]'
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q test
# Real Linux FUSE, S3, npm local dependencies, edit/commit/push, restart, and I/O benchmark:
AFS_RUN_E2E_TESTS=1 PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q -s test/fuse_test.py
```

## Upstream and license

Apache-2.0. Upstream history and notices are retained. See
[README.upstream.md](README.upstream.md) for the original bundle helper's
reference documentation. `packaging/upstream.env` records the pinned upstream
revisions; the ArtifactFS build applies the included opt-in native-mount patch.
