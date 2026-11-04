# Developing Scuffle

Scuffle is a Python 3 and PySide6 app. The Nix dev shell has everything it
needs, including `umu-launcher` and the Windows bridge. [DESIGN.md](DESIGN.md)
explains how it works.

## Running and testing

```sh
nix develop -c env PYTHONPATH=src python3 -m scuffle   # the window, from the checkout
nix develop -c pytest -q                                # the tests, a few minutes
nix build                                               # the Nix package, runs the tests
```

`tools/check_uninstall_mutations.py` and `tools/check_move_mutations.py` break
the uninstall and move code on purpose and check that the tests catch each
change. Run them with `nix develop -c python3 tools/<script>`. Their reports go
under `.pytest_cache/`.

## Screenshots

```sh
nix develop -c env QT_QPA_PLATFORM=offscreen PYTHONPATH=src \
  python3 tools/screens.py /tmp/scuffle-screens
nix develop -c python3 tools/readme_screenshot.py
```

`tools/screens.py` renders every page and state of the window at three window
sizes, for review after a change to the UI. `tools/readme_screenshot.py` draws
`docs/screenshot.png`, the picture in the README. Both make up their clients,
downloads and sign-in in memory, with a fixed client catalog from
`tests/fixtures/catalog-us.json`, so they need no installation and no network.
The README picture only changes when the window does, so run the script and
commit the new picture after changing the window.

## Client snapshot

```sh
nix develop -c env PYTHONPATH=src python3 tools/catalog_snapshot.py
```

Scuffle reads Battle.net's public catalog at run time.
`src/scuffle/catalog_snapshot.py` holds a copy of what it read, for a first
start without a network. The script reads the catalog from Blizzard's CDN and
writes that file again, one product to a line. Run it before a release, or when
Blizzard adds or renames a client, and commit the result. The tests read the
captured catalog in `tests/fixtures/battlenet` instead, see
`tests/fixtures/README.md`.

## AppImage

```sh
nix build .#appimage
packaging/appimage/test-ubuntu.sh result/Scuffle-0.1.0-x86_64.AppImage
```

The first command writes `result/Scuffle-0.1.0-x86_64.AppImage`. The second
runs it in a plain Ubuntu container with podman, without network, and checks
the command line, the window on X11, Wayland and offscreen, and the bundled
`umu-run`. On NixOS, try the AppImage with `appimage-run` with `zstd` added to
its libraries.

## Windows package

PyInstaller cannot cross-compile, so `Scuffle.exe` is built on Windows:

```sh
packaging/windows/remote-build.sh <ssh host>
```

The script copies the working tree, with its uncommitted changes, to
`scuffle-build` in the remote home folder, runs `packaging/windows/build.ps1`
there and fetches the result. The Windows machine needs Python 3.14 from
python.org with the `py` launcher. On a Windows checkout,
`packaging/windows/build.ps1` builds the same file directly.

The build tools are pinned with hashes in `packaging/windows/requirements.txt`.
After changing `requirements.in`, regenerate it with the command at its top.

## What the packages contain

Both packages bundle other programs, unchanged and each under its own license:

| Program | License | In |
| --- | --- | --- |
| CPython, from python-build-standalone on Linux | PSF License | both |
| Qt for Python (PySide6, Shiboken6) and the Qt libraries it ships | LGPL 3 | both |
| PyInstaller's bootloader | GPL 2 with an exception for bundled programs | `Scuffle.exe` |
| umu-launcher | GPL 3 | AppImage |
| jeepney | MIT | AppImage |
| The xcb utility libraries from Debian 12 | MIT and X11 | AppImage |
| The AppImage type 2 runtime, with the libraries it links | MIT, and theirs | AppImage |

The exact versions and download addresses are in `packaging/appimage/default.nix`
and `packaging/windows/requirements.txt`. Their sources are at those projects.

## Releases

A tag such as `v0.1.0` that matches the version in `pyproject.toml` makes CI
build both packages, test them and publish a GitHub release with
`Scuffle.exe`, `Scuffle-<version>-x86_64.AppImage` and their `SHA256SUMS`.
Before tagging, refresh the client snapshot and the README picture, and raise
the version in `pyproject.toml`.

```sh
git tag v0.1.0
git push origin v0.1.0
```
