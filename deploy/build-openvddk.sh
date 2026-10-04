#!/bin/bash
# Build OpenVDDK (a GPL-3.0 clean-room VDDK) and install it where nbdkit's
# vddk plugin can load it:  /opt/openvddk/lib64/libvixDiskLib.so.8
#
#   OPENVDDK_REPO   git URL to build from
#   OPENVDDK_REF    branch, tag or commit (default: the repository default)
#   PREFIX          install location (default /opt/openvddk)
set -euo pipefail

REPO=${OPENVDDK_REPO:-https://github.com/jimmyma-zhanwei/openvddk.git}
REF=${OPENVDDK_REF:-}
PREFIX=${PREFIX:-/opt/openvddk}
SRC=${SRC:-/usr/local/src/openvddk}

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

dnf -y install git gcc make cmake openssl-devel zlib-devel nbdkit nbdkit-vddk-plugin

if [ -d "$SRC/.git" ]; then
    git -C "$SRC" fetch --tags origin
else
    git clone "$REPO" "$SRC"
fi
git -C "$SRC" checkout -- .
if [ -n "$REF" ]; then
    git -C "$SRC" checkout "$REF"
else
    git -C "$SRC" pull --ff-only
fi
echo "Building OpenVDDK at $(git -C "$SRC" rev-parse --short HEAD)"

# Make OpenVDDK binary-compatible with VMware's VDDK as nbdkit's vddk plugin
# uses it (see the patch header). Skipped once upstream contains the fix.
PATCH="$(dirname "$(readlink -f "$0")")/openvddk-abi.patch"
git -C "$SRC" checkout -- .
if git -C "$SRC" apply --check "$PATCH" 2>/dev/null; then
    git -C "$SRC" apply "$PATCH"
    echo "Applied $(basename "$PATCH")"
elif git -C "$SRC" apply --reverse --check "$PATCH" 2>/dev/null; then
    echo "Upstream already contains the ABI fix"
else
    echo "openvddk-abi.patch does not apply to this revision; review it" >&2
    exit 1
fi

cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$SRC/build" -j"$(nproc)"
(cd "$SRC/build" && ctest --output-on-failure)

lib=$(find "$SRC/build" -name 'libvixDiskLib.so*' -type f | sort | head -1)
[ -n "$lib" ] || { echo "build produced no libvixDiskLib.so" >&2; exit 1; }
install -d "$PREFIX/lib64"
cp -a "$(dirname "$lib")"/libvixDiskLib.so* "$PREFIX/lib64/"
[ -e "$PREFIX/lib64/libvixDiskLib.so.8" ] || ln -sf "$(basename "$lib")" "$PREFIX/lib64/libvixDiskLib.so.8"
git -C "$SRC" rev-parse HEAD > "$PREFIX/REVISION"

# nbdkit loads the library lazily; --dump-plugin with libdir proves it resolves.
nbdkit vddk libdir="$PREFIX" --dump-plugin | grep -E '^vddk_(library_version|dll)' || {
    echo "nbdkit could not load OpenVDDK from $PREFIX" >&2; exit 1; }
echo "OpenVDDK installed in $PREFIX"
