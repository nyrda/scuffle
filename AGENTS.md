# AGENTS.md

Scuffle installs, updates and launches the public World of Warcraft clients
without the Battle.net app. Linux runs games through Proton and `umu-run`, and
Windows runs them natively. It is a Python 3 and PySide6 app.

Read `DESIGN.md` before changing behaviour. `README.md` is the user's guide, and
`DEVELOPMENT.md` covers building, testing and packaging.

## Layout

- `src/scuffle/`: `library.py` (installations, uninstall), `downloads.py` (the
  queue and its worker), `move.py`, `imports.py`, `launch.py`, `cli.py`,
  `settings.py`, `diagnostics.py`, `shortcuts.py` and `steam.py`, and
  `instance.py` (one window per user)
- `src/scuffle/ngdp/`: Blizzard's CDN formats and the shared object store
- `src/scuffle/auth/`: the Battle.net sign-in and the per-account session vault
- `src/scuffle/runtime/`: Proton, the Wine bridge and the Windows runtime
- `src/scuffle/ui/`: the window. `style.py` holds the colours, the stylesheet
  and the code-drawn tiles
- `bridge/`: the C helper that runs inside the Wine prefix
- `packaging/`: the AppImage (`appimage/`) and the Windows package (`windows/`)
- `tools/`: `screens.py` renders every window state. The `check_*_mutations.py`
  scripts check that the uninstall and move tests catch deliberate bugs

## Commands

```sh
nix develop -c pytest -q                       # the whole suite, a few minutes
nix develop -c env QT_QPA_PLATFORM=offscreen PYTHONPATH=src \
  python3 tools/screens.py /tmp/scuffle-screens  # every UI state at three sizes
nix develop -c python3 tools/readme_screenshot.py  # docs/screenshot.png, after window changes
nix build                                      # the Nix package, runs the tests
nix build .#appimage                           # the Linux AppImage
packaging/windows/remote-build.sh <ssh host>   # Scuffle.exe, built on Windows
```

## Rules for the product

- No Blizzard artwork, logos or icons, bundled or fetched. Icons and tiles are
  drawn in code or are original SVGs.
- Sign-in uses Battle.net's own page in the browser. Never read or write the
  Battle.net app's saved login. Never log, show or store a ticket, token or
  email in plain text.
- Every change of state on disk is crash safe: write atomically, journal
  multi-step changes, and recover on the next start. Uninstall and move are
  the models.

## Code and tests

- Small functions, plain names, comments that say why.
- UI controls are reachable by keyboard and join the page's focus chain. Render
  new states with `tools/screens.py` and look at all three sizes.
- Tests check behaviour at the real boundary, with no test-only seams in
  production code. A regression test must fail without its fix.
- `tests/test_ui.py::test_the_folder_picker_reveals_the_current_folder_once_it_has_loaded`
  is timing-sensitive and fails when `/tmp` is crowded. Rerun it alone.

## Docs and commits

- Prose in docs has no semicolons. README is for users, DESIGN for the design,
  DEVELOPMENT for working on Scuffle.
- A commit message is one plain sentence about the change, as in
  `git log --oneline`.
