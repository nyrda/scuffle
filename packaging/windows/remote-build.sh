#!/usr/bin/env bash
# Build dist/Waystone.exe on a Windows machine over SSH.
#
# Usage: packaging/windows/remote-build.sh <ssh host> [remote folder]
#
# Nix cross-builds the bridge here. The working tree, with uncommitted changes,
# goes to <remote folder> in the remote user's home, build.ps1 runs there, and
# the result comes back to dist/Waystone.exe. The remote machine needs Python
# from python.org with the py launcher. The remote folder keeps the build tools
# between runs.
set -euo pipefail

host=${1:?usage: remote-build.sh <ssh host> [remote folder]}
remote=${2:-waystone-build}
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$root"

# Run a PowerShell script remotely, whichever shell OpenSSH starts there.
remote_powershell() {
    local encoded
    encoded=$(printf '%s' "\$ErrorActionPreference = 'Stop'; \$ProgressPreference = 'SilentlyContinue'; Set-Location \$HOME; $1" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
    ssh "$host" powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "$encoded"
}

bridge=$(nix build .#bridge --no-link --print-out-paths)/libexec/wow_auth_bridge.exe
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
git ls-files -z --cached --others --exclude-standard |
    while IFS= read -r -d '' file; do if [[ -e $file ]]; then printf '%s\0' "$file"; fi; done |
    tar --null -T - -cf "$staging/source.tar"

remote_powershell "
    New-Item -ItemType Directory -Force '$remote' | Out-Null
    if (Test-Path '$remote\\source') { Remove-Item -Recurse -Force '$remote\\source' }
    New-Item -ItemType Directory '$remote\\source' | Out-Null"
scp -q "$staging/source.tar" "$bridge" "$host:$remote/"
remote_powershell "
    tar -xf '$remote\\source.tar' -C '$remote\\source'
    if (\$LASTEXITCODE) { exit 1 }
    & '$remote\\source\\packaging\\windows\\build.ps1' -Bridge '$remote\\wow_auth_bridge.exe' -Venv (Join-Path \$HOME '$remote\\venv')"
mkdir -p dist
scp -q "$host:$remote/source/dist/Waystone.exe" dist/Waystone.exe
echo "Fetched $root/dist/Waystone.exe"
