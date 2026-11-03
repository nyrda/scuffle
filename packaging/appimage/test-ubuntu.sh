#!/usr/bin/env bash
# Start the AppImage in a plain Ubuntu container, as a distribution without Nix runs it.
#
# Usage: packaging/appimage/test-ubuntu.sh <AppImage> [ubuntu tag]
#
# The image gets the libraries every desktop has, but not Python or the xcb helper
# libraries that the AppImage brings. The checks run without network, with folders of
# their own: --help, the client list, the window on the offscreen, X11 and Wayland
# platforms, the bundled umu-run in a mount of its own, and the bridge and umu-run that
# Waystone resolves. Needs podman.
set -euo pipefail

appimage=$(readlink -f "${1:?usage: test-ubuntu.sh <AppImage> [ubuntu tag]}")
tag=${2:-24.04}
image=waystone-appimage-test:$tag
fixture=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)/tests/fixtures/catalog-us.json

podman build -q -t "$image" -f - <<EOF >/dev/null
FROM docker.io/library/ubuntu:$tag
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -qq -y --no-install-recommends \
      libgl1 libegl1 libfontconfig1 fonts-dejavu-core libdbus-1-3 libxkbcommon0 libxkbcommon-x11-0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-glx0 libxcb-randr0 libxcb-render0 libxcb-shape0 \
      libxcb-shm0 libxcb-sync1 libxcb-xfixes0 libxcb-xkb1 libwayland-client0 libwayland-cursor0 \
      libwayland-egl1 fuse3 xvfb weston >/dev/null \
 && (dpkg -l libglib2.0-0 libglib2.0-0t64 2>/dev/null | grep -q ^ii || apt-get install -qq -y libglib2.0-0 >/dev/null) \
 && for lib in libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-util1; do \
      if dpkg -s \$lib >/dev/null 2>&1; then dpkg --remove --force-depends \$lib >/dev/null; fi; done \
 && ! command -v python3
EOF

podman run --rm -i --network=none --device /dev/fuse --cap-add SYS_ADMIN \
    -v "$appimage:/test/Waystone.AppImage:ro" -v "$fixture:/test/catalog.json:ro" \
    "$image" bash -s <<'EOF'
set -euo pipefail
export HOME=/tmp/home XDG_CONFIG_HOME=/tmp/xdg/config XDG_DATA_HOME=/tmp/xdg/data XDG_CACHE_HOME=/tmp/xdg/cache
mkdir -p $HOME $XDG_CACHE_HOME/waystone/catalog
# A fresh catalog, so the window does not look for clients online.
for region in us eu; do
    sed -E "s/\"checked_at\": *[0-9]+/\"checked_at\": $(date +%s)/; s/\"region\": *\"[a-z]+\"/\"region\": \"$region\"/" \
        /test/catalog.json > $XDG_CACHE_HOME/waystone/catalog/$region.json
done
app=/test/Waystone.AppImage
check() {   # description, then a shell command that must succeed
    if bash -c "$2"; then echo "ok: $1"; else echo "FAIL: $1"; exit 1; fi
}

check '--help through FUSE' "$app --help | grep -q 'usage: waystone'"
check '--help extracted, without FUSE' "$app --appimage-extract-and-run --help | grep -q 'usage: waystone'"
check 'clients' "$app --root /tmp/root clients | grep -q '\"client\": \"wow\"'"

window() {   # platform, then variables. The window must still run after 8 seconds.
    local platform=$1; shift
    local status=0
    env "$@" QT_QPA_PLATFORM=$platform timeout 8 $app > /tmp/$platform.log 2>&1 || status=$?
    if [ $status != 124 ]; then echo "FAIL: the window on $platform exited with $status"; cat /tmp/$platform.log; exit 1; fi
    echo "ok: window on $platform"
}
window offscreen
Xvfb :5 -nolisten tcp 2>/dev/null & sleep 2
window xcb DISPLAY=:5
mkdir -p /tmp/run && chmod 700 /tmp/run
backend=headless
case $(weston --version) in 'weston '[1-9].*) backend=headless-backend.so;; esac      # Ubuntu 22.04
XDG_RUNTIME_DIR=/tmp/run weston --backend=$backend --socket=test --idle-time=0 2>/dev/null & sleep 2
window wayland XDG_RUNTIME_DIR=/tmp/run WAYLAND_DISPLAY=test

# Waystone's mount stays only while Waystone runs, so umu-run starts the AppImage again.
$app --appimage-mount > /tmp/mountpoint & mounted=$!
sleep 2
APPIMAGE=$app sh -x "$(cat /tmp/mountpoint)/usr/bin/umu-run" --help > /tmp/umu.log 2>&1
kill $mounted
check 'umu-run in a mount of its own' "grep -q 'exec $app --help' /tmp/umu.log && grep -q 'Unified Linux Wine Game Launcher' /tmp/umu.log"

# An extracted copy is writable, so it shows whether Waystone writes into the image.
cd /tmp && $app --appimage-extract >/dev/null
root=/tmp/squashfs-root
touch /tmp/marker && sleep 1
check 'window from an extracted copy' "QT_QPA_PLATFORM=offscreen timeout 8 $root/AppRun; [ \$? = 124 ]"
check 'umu-run from an extracted copy' "$root/usr/bin/umu-run --help | grep -q 'Unified Linux Wine Game Launcher'"
check 'nothing written into the image' "[ -z \"\$(find $root -newer /tmp/marker)\" ]"

# What Waystone resolves inside the AppImage, with AppRun's variables.
check 'bridge copied to the data folder, umu-run from the AppImage' "
APPDIR=$root WAYSTONE_BRIDGE=$root/usr/libexec/waystone/wow_auth_bridge.exe PATH=$root/usr/bin:\$PATH \
    $root/usr/python/bin/python3.14 -I -B -c '
import os, pathlib
from waystone.runtime.bridge import bridge_executable
from waystone.runtime.proton import find_umu
from waystone.settings import data_dir
bridge, umu = bridge_executable(), find_umu()
assert bridge.parent == data_dir() / \"bin\" and bridge.is_file(), bridge
assert umu == pathlib.Path(os.environ[\"APPDIR\"]) / \"usr/bin/umu-run\", umu
'"
rm $root/usr/lib/umu/umu-run
check 'a missing umu-run says so' "! $root/usr/bin/umu-run --help 2>/tmp/missing.log && grep -q 'umu-run is missing from this Waystone AppImage' /tmp/missing.log"
EOF
