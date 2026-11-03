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
