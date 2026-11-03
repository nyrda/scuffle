# Waystone

Waystone installs, updates and launches the public World of Warcraft clients
without the Battle.net desktop client. One folder holds every installed client,
and content the clients share is stored once.

This project is not affiliated with or endorsed by Blizzard Entertainment.
Downloads come from Blizzard's public CDN. Signing in uses Battle.net's own
sign-in page in your browser.

Status: Linux works. On Windows the packaged build installs, updates,
verifies and uninstalls clients, signs in, and starts and closes games. See
[DESIGN.md](DESIGN.md) for the architecture.

## Clients

The list shows the live clients: World of Warcraft, Mists of Pandaria Classic,
Burning Crusade Anniversary, Classic Era with its Era, Hardcore and Season of
Discovery modes, and Forever. **Settings › Clients** chooses what else appears.

- **Test clients**: the PTRs and betas, each with its own checkbox.
- **Other public clients**: older builds Blizzard still publishes openly, such
  as the Dark Realm Test, a retail event build and a Classic submission build.
  Turn on **Show other public clients** to choose from them. Turned off, only
  installed ones stay listed. Many are old and may not connect.

Each row shows the version Blizzard publishes for your region, and whether the
client is installed, downloading or older than its live game. Any client,
installed or not, can be hidden from the list there or with **Hide from list**
in its **⋯** menu. Waystone looks up the current clients on Blizzard's version
service in the background and keeps the last answer for when it is offline.

To free space, choose **Uninstall…** in an installed client's **⋯** menu.
The confirmation shows its measured content size; shared content stays for the
other clients. By default, Waystone moves WTF, Interface and Screenshots, with
your settings, addons and pictures, to `Waystone-backup-<client>-<id>` inside the
installation folder.
Uncheck that option to delete them too. All other files in that client's game
folder are deleted. **Hide from list** leaves the installation on disk.

Close the client and finish any unfinished update or remove its download job
before uninstalling. Removal runs in the background. If Waystone closes halfway
through, it finishes the removal on the next start.

## Importing an existing installation

If a client is already installed elsewhere, for example by Battle.net, choose
**Import…** in its **⋯** menu instead of **Install**. Waystone looks for
installations in Wine prefixes such as `~/.wine`, Lutris prefixes and
`~/Games`, Steam's Proton prefixes and mounted drives under `/mnt`, `/media`
and `/run/media`. On Windows it looks in Program Files, on local drives and in
the folders Battle.net records. The search stops after 12 seconds and skips
network drives. If yours is not listed, **Choose folder…** picks it, either
the `World of Warcraft` folder itself or a folder above it.

The page lists the clients the installation holds. Check the ones to import.
Each becomes an ordinary download in the queue, which you can pause and resume.
Content that matches the current build is copied from the installation, and
only the rest is downloaded. If the installation is on a drive that is not
connected when a paused import resumes, the import fails with a message.
Connect the drive and try again, or remove the download and install normally.

**Copy settings and addons** is on by default. Once a client's install has
succeeded, it copies the `WTF` and `Interface` folders, with your settings,
keybinds, macros and addons, into the new client folder. Files already there
are kept, except the default `Config.wtf` Waystone writes, which your own
replaces. Links inside those folders are not copied.

Waystone only reads the installation you import from. It never changes, moves
or deletes anything in it, so Battle.net can keep using it.

## Run

```sh
nix run .           # the window
nix run . -- --help  # the command line
```

On first start, choose an installation folder in the settings. Waystone owns
that folder. Do not let Battle.net manage it at the same time.

While a game runs, its button reads **Close game**, which closes it as its
window's close button does. Hold Shift and the button reads **Force quit**,
which ends the game at once, for a game that hangs.

On Linux, Waystone uses a Proton build and `umu-run` from the usual places,
such as `~/.steam/root/compatibilitytools.d`. Pick another Proton build in the
settings. All clients share one Wine prefix in `~/.local/share/waystone/prefix`.

## Launch options

Each client's ⋯ menu has **Launch options…** for arguments that Wow.exe
understands: the graphics API, windowed or fullscreen, the console, no sound,
compatibility mode and no driver extensions. Other arguments, such as
`-config Config2.wtf`, go into the extra field. Waystone sets `-launcherlogin`,
`-uid` and `-initialgamemode` itself. On Linux a client can also have its own
environment variables.

On Linux the **Launch** card in the settings sets what runs around every client:

- GameMode runs the game through `gamemoderun`.
- MangoHud sets `MANGOHUD=1`, which works through umu's container.
- An option that cannot work with the chosen Proton build or this machine,
  such as Wayland or HDR, is greyed out and shown off. Its choice is
  kept for a build or tool that has it.
- The Proton log goes to `~/.local/share/waystone/logs/steam-battlenet.log`.
- Environment variables are `NAME=value` lines. A client's own variables win.
- A wrapper command goes before `umu-run`, as in Steam. For example
  `gamescope -w 2560 -h 1440 -f --`.

## Signing in

The first **Play** opens Battle.net's sign-in page in your browser. After you
sign in, the browser cannot open a page at `http://localhost:0/?ST=...`. Copy
that address from the address bar. Waystone reads it from the clipboard when
you switch back, or you can paste it. The address works only once.

Waystone turns it into its own renewable session. Each launch renews it, so you
sign in again only when Battle.net ends the session. The session is stored with
DPAPI under `~/.local/share/waystone/session` with owner-only permissions.
Waystone never reads the Battle.net app's own saved login, so both stay signed
in.

If your Battle.net login has several WoW accounts, the game asks which one to
use. The sidebar then shows a choice next to **Signed in**. Pick an account
there and the game starts with it. **Ask in the game** brings the game's
question back. With one account the game never asks.

## Downloads

Downloads run one at a time. The Downloads page lets you reorder waiting jobs,
pause or resume the whole queue, and see its estimated total time. Unknown sizes
and paused jobs are noted beside the estimate.

The queue survives closing Waystone. Unfinished jobs return paused, in order,
and Resume continues using stored content. Progress comes from the pending plan
and stored objects. If it cannot be read, Waystone checks it on resume. Failed
jobs keep their error until retried or removed.

**Settings › Downloads** accepts a custom speed limit in MB/s, with 0 for no
limit, and 1 to 64 connections. The connection count applies from the next
download. You can also pause downloads while a game is running. After the last
game exits, only downloads paused by that setting resume. Automatic resume on
Waystone startup is a separate setting, off by default.

After each install or update, Waystone removes content that no installed client
needs anymore.

Waystone checks for updates on start, every 30 minutes and before each launch.
If a client is behind, **Play** starts its update instead.

## Command line

```sh
waystone --root /games/Waystone clients
waystone --root /games/Waystone install wow --region eu --locale enUS
waystone --root /games/Waystone install wow --seed-from "/games/World of Warcraft"
waystone --root /games/Waystone update wow
waystone --root /games/Waystone verify wow
waystone --root /games/Waystone play wow_classic_era --mode hardcore
```

`--seed-from` copies verified content from an existing installation, such as a
Battle.net one, and opens it read-only. It is what **Import…** does in the
window, without copying settings and addons. Progress is printed to stderr as
JSON lines.

On Windows, `Waystone.exe` is a windowed program, so a terminal does not wait
for it. Start a command with `start /wait "" Waystone.exe ...` in cmd, or with
`Start-Process -Wait -NoNewWindow Waystone.exe -ArgumentList ...` in PowerShell.

## Development

```sh
nix develop
pytest
PYTHONPATH=src python -m waystone
```

To check the uninstall tests against deliberate failures, run
`nix develop -c python3 tools/check_uninstall_mutations.py`. The script restores
each source file and writes its report under `.pytest_cache/uninstall-mutations`.

## Windows package

`dist/Waystone.exe` is a single file with Python and Qt inside. Windows needs
no bridge, since Waystone does its work there itself. PyInstaller cannot
cross-compile, so the file is built on a Windows machine over SSH:

```sh
packaging/windows/remote-build.sh <ssh host>
```

The script copies the working tree with its uncommitted changes to
`waystone-build` in the remote home folder, runs `packaging/windows/build.ps1`
there and fetches the result. The Windows machine needs Python 3.14 from
python.org with the `py` launcher. On a Windows checkout,
`packaging/windows/build.ps1` builds the same file directly.

The file is not signed yet. Smart App Control, which Windows 11 turns on for
many new installations, can block an unsigned program, sometimes only on its
first start. Public releases need a code signing certificate.

Build tools come from `packaging/windows/requirements.txt`, pinned with hashes.
After changing `requirements.in`, regenerate it with the command at its top.
