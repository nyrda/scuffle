#!/usr/bin/env bash
# Build the Windows package on a Windows machine over SSH.
#
# Usage: packaging/windows/remote-build.sh <ssh host> [remote folder]
#
# The working tree, with uncommitted changes, goes to <remote folder> in the
# remote user's home, build.ps1 runs there, and the zip comes back to
# dist/Scuffle-<version>-windows-x64.zip. The remote machine needs Python from python.org with the
# py launcher. The remote folder keeps the build tools between runs.
set -euo pipefail

host=${1:?usage: remote-build.sh <ssh host> [remote folder]}
remote=${2:-scuffle-build}
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$root"

# Run a PowerShell script remotely, whichever shell OpenSSH starts there.
remote_powershell() {
    local encoded
    encoded=$(printf '%s' "\$ErrorActionPreference = 'Stop'; \$ProgressPreference = 'SilentlyContinue'; Set-Location \$HOME; $1" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
    ssh "$host" powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "$encoded"
}

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
git ls-files -z --cached --others --exclude-standard |
    while IFS= read -r -d '' file; do if [[ -e $file ]]; then printf '%s\0' "$file"; fi; done |
    tar --null -T - -cf "$staging/source.tar"

remote_powershell "
    New-Item -ItemType Directory -Force '$remote' | Out-Null
    if (Test-Path '$remote\\source') { Remove-Item -Recurse -Force '$remote\\source' }
    New-Item -ItemType Directory '$remote\\source' | Out-Null"
scp -q "$staging/source.tar" "$host:$remote/"
remote_powershell "
    tar -xf '$remote\\source.tar' -C '$remote\\source'
    if (\$LASTEXITCODE) { exit 1 }
    & '$remote\\source\\packaging\\windows\\build.ps1' -Venv (Join-Path \$HOME '$remote\\venv')"
version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
zip=Scuffle-$version-windows-x64.zip
mkdir -p dist
scp -q "$host:$remote/source/dist/$zip" "dist/$zip"
echo "Fetched $root/dist/$zip"
