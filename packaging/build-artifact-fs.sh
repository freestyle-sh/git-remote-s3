#!/bin/sh
# Requires git and Go (GOTOOLCHAIN=auto fetches the version in the pinned go.mod).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$here/packaging/upstream.env"
out=${1:?usage: build-artifact-fs.sh OUTPUT}
mkdir -p "$(dirname "$out")"
out=$(cd "$(dirname "$out")" && pwd)/$(basename "$out")
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT INT TERM
git -C "$build_dir" init --quiet
git -C "$build_dir" remote add origin https://github.com/cloudflare/artifact-fs.git
git -C "$build_dir" fetch --quiet --depth=1 origin "$ARTIFACT_FS_REVISION"
git -C "$build_dir" checkout --quiet --detach FETCH_HEAD
test "$(git -C "$build_dir" rev-parse HEAD)" = "$ARTIFACT_FS_REVISION"
cd "$build_dir"
git apply "$here/packaging/artifact-fs-local-mounts.patch"
if [ "${ARTIFACT_FS_CHECK:-0}" = 1 ]; then
    GOTOOLCHAIN=auto go vet ./...
    GOTOOLCHAIN=auto go test ./...
fi
GOTOOLCHAIN=auto CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o "$out" ./cmd/artifact-fs
# The binary is distributed with its own and its dependencies' notices.
cp LICENSE "${out}.LICENSE"
if [ -f NOTICE ]; then cp NOTICE "${out}.NOTICE"; fi
