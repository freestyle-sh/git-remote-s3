#!/bin/sh
# Install from a checked-out release. Run as root (sudo ./install.sh).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
prefix=${PREFIX:-/usr/local}
venv=${VENV:-/opt/freestyle/git-s3}
[ "$(id -u)" = 0 ] || { echo 'run with sudo' >&2; exit 1; }
if [ "${SKIP_APT:-0}" != 1 ]; then
    apt-get update -qq
    apt-get install -y --no-install-recommends git git-lfs fuse3 python3-venv ca-certificates
fi
# Non-root workspace daemons retain Unix permission checks with allow_other.
# This lets the privileged mount helper attach native dependency directories.
touch /etc/fuse.conf
if ! grep -qx user_allow_other /etc/fuse.conf; then
    printf "\nuser_allow_other\n" >> /etc/fuse.conf
fi
python3 -m venv "$venv"
"$venv/bin/pip" install --no-cache-dir --requirement "$here/packaging/requirements.txt"
"$venv/bin/pip" install --no-cache-dir --no-deps "$here"
mkdir -p "$prefix/bin" "$prefix/share/doc/git-s3"
for tool in git-remote-s3 git-remote-s3+lazy git-lfs-s3 git-s3 git-s3-workspace; do
    ln -sf "$venv/bin/$tool" "$prefix/bin/$tool"
done
if [ -n "${ARTIFACT_FS_BINARY:-}" ]; then
    install -m 0755 "$ARTIFACT_FS_BINARY" "$prefix/bin/artifact-fs"
    install -m 0644 "$ARTIFACT_FS_BINARY.LICENSE" "$prefix/share/doc/git-s3/ArtifactFS-LICENSE"
else
    if ! command -v go >/dev/null; then
        apt-get install -y --no-install-recommends golang-go
    fi
    "$here/packaging/build-artifact-fs.sh" "$prefix/bin/artifact-fs"
    mv "$prefix/bin/artifact-fs.LICENSE" "$prefix/share/doc/git-s3/ArtifactFS-LICENSE"
fi
install -m 0644 "$here/LICENSE" "$here/NOTICE" "$prefix/share/doc/git-s3/"
if [ -f "$here/SOURCE_REVISION" ]; then
    install -m 0644 "$here/SOURCE_REVISION" "$prefix/share/doc/git-s3/"
fi
"$prefix/bin/git-s3-workspace" --help >/dev/null
"$prefix/bin/artifact-fs" --help >/dev/null
printf 'Installed git-remote-s3, git-remote-s3+lazy, git-lfs-s3, git-s3-workspace and ArtifactFS\n'
