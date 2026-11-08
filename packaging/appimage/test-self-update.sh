#!/usr/bin/env bash
# Update a real AppImage to another real AppImage in a plain Ubuntu container.
#
# Usage: packaging/appimage/test-self-update.sh <AppImage>
#
# Repacks the AppImage twice, one that asks a local server instead of GitHub and a newer one.
# A wrong checksum must change nothing, and a good release must replace the running file, start
# the new version and clear away the old file. Run test-ubuntu.sh first, for its image. Needs
# podman, and mksquashfs on the PATH.
set -euo pipefail

appimage=$(readlink -f "${1:?usage: test-self-update.sh <AppImage>}")
checkout=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
image=scuffle-appimage-test:24.04
port=18443
new=99.0.0
work=$(mktemp -d)
trap 'kill "${server:-}" 2>/dev/null || true; chmod -R u+w "$work"; rm -rf "$work"' EXIT
current=$(sed -nE 's/^version = "(.*)"/\1/p' "$checkout/pyproject.toml")
offset=$("$appimage" --appimage-offset)

repack() {     # version, output file
    local root=$work/root-$1
    mkdir "$root"
    (cd "$root" && "$appimage" --appimage-extract >/dev/null)
    local squash=$root/squashfs-root site
    chmod -R u+w "$squash"
    site=$(dirname "$(dirname "$(find "$squash" -path '*/scuffle/selfupdate.py')")")
    sed -i "s#https://api.github.com/repos/nyrda/scuffle/releases/latest#http://127.0.0.1:$port/latest#
            s#https://github.com/nyrda/scuffle/releases/download/#http://127.0.0.1:$port/download/#" "$site/scuffle/selfupdate.py"
    find "$site/scuffle/__pycache__" -name 'selfupdate.*' -delete     # compiled from the original
    if [ "$1" != "$current" ]; then
        mv "$site/scuffle-$current.dist-info" "$site/scuffle-$1.dist-info"
        sed -i "s/^Version: .*/Version: $1/" "$site/scuffle-$1.dist-info/METADATA"
    fi
    head -c "$offset" "$appimage" > "$2"
    mksquashfs "$squash" "$work/image-$1" -quiet -no-progress -noappend -all-root -no-xattrs -comp zstd
    cat "$work/image-$1" >> "$2"
    chmod 755 "$2"
}

mkdir "$work/publish" "$work/install"
chmod 777 "$work/install"
repack "$current" "$work/install/Scuffle-$current-x86_64.AppImage"
repack "$new" "$work/publish/Scuffle-$new-x86_64.AppImage"
released=$work/publish/Scuffle-$new-x86_64.AppImage
wanted=$(sha256sum "$released" | cut -d' ' -f1)

start_server() {
    [ -z "${server:-}" ] || { kill "$server"; wait "$server" 2>/dev/null || true; }
    python3 "$checkout/tools/self_update_server.py" "$port" "$new" "$released" "$@" 2>/dev/null &
    server=$!
    sleep 2
}

in_container() {
    podman run --rm -i --network=host --device /dev/fuse --cap-add SYS_ADMIN \
        -e CURRENT="$current" -e NEW="$new" -e WANTED="$wanted" \
        -v "$work/install:/install" "$image" bash -s
}

start_server --wrong-sum
in_container <<'INNER'
set -euo pipefail
export HOME=/tmp/home XDG_CONFIG_HOME=/tmp/xdg/config XDG_DATA_HOME=/tmp/xdg/data XDG_CACHE_HOME=/tmp/xdg/cache
mkdir -p $HOME
check() {   # description, then a command that must succeed
    if bash -c "$2"; then echo "ok: $1"; else echo "FAIL: $1"; exit 1; fi
}
export app=/install/Scuffle-$CURRENT-x86_64.AppImage
export before=$(sha256sum "$app")
check 'the old package reports its version' '[ "$("$app" --version)" = "scuffle $CURRENT" ]'
check 'a wrong checksum fails the update' '! "$app" self-update 2>/dev/null'
check 'a wrong checksum leaves the program as it was' '[ "$(sha256sum "$app")" = "$before" ]'
check 'a wrong checksum leaves no download behind' '[ ! -e /install/Scuffle-$CURRENT-x86_64.new.AppImage ]'
INNER

start_server
in_container <<'INNER'
set -euo pipefail
export HOME=/tmp/home XDG_CONFIG_HOME=/tmp/xdg/config XDG_DATA_HOME=/tmp/xdg/data XDG_CACHE_HOME=/tmp/xdg/cache
export XDG_RUNTIME_DIR=/tmp/runtime QT_QPA_PLATFORM=offscreen
mkdir -p $HOME $XDG_RUNTIME_DIR; chmod 700 $XDG_RUNTIME_DIR
check() {   # description, then a command that must succeed
    if bash -c "$2"; then echo "ok: $1"; else echo "FAIL: $1"; exit 1; fi
}
export app=/install/Scuffle-$CURRENT-x86_64.AppImage
check 'the update reports success' '"$app" self-update --start | grep -q "\"updated\": true"'
check 'the program file is the release' '[ "$(sha256sum "$app" | cut -d" " -f1)" = "$WANTED" ]'
export old=/install/Scuffle-$CURRENT-x86_64.old.AppImage
for _ in $(seq 60); do [ -e "$old" ] || break; sleep 1; done
check 'the restarted Scuffle cleared away the old file' '[ ! -e "$old" ]'
check 'the updated Scuffle is running' 'pgrep -f "python3.14 -I -B -m scuffl[e]" >/dev/null'
pkill -f 'python3.14 -I -B -m scuffl[e]' || true
sleep 2
check 'the updated program reports the new version' '[ "$("$app" --version)" = "scuffle $NEW" ]'
INNER
