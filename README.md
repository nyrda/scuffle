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

To put the installation somewhere else, use **Move…** next to the folder in
**Settings › General** and pick an empty or new folder. Everything moves along:
every client, the shared content and Waystone's own records. **Choose…** only
points Waystone at another folder and moves nothing.

- On the same drive the folder moves at once.
- To another drive, Waystone copies everything, checks the copy and switches to
  it, and only then deletes the old folder. The copy needs as much free space as
  the installation takes, plus 1 GB. The progress shows at the bottom of the
  window. Until the copy is checked, **Cancel** removes the copy and leaves the
  old folder as it was.
- Close every game and finish or remove every download first, including paused
  and failed ones. Games and downloads cannot start until the move is done.
- Waystone refuses a folder inside the installation, a folder that is not
  empty, and an installation that holds links or another mounted drive.
- If Waystone or the computer stops halfway, the next start finishes the move
  once the copy was checked, and undoes it otherwise. One complete installation
  always remains.

Don't move the folder by hand while Waystone uses it. On Linux the Wine prefix
stays in `~/.local/share/waystone/prefix`. The game writes its own folder into
the prefix again each time it starts, so nothing there needs changing.

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

If your Battle.net account has several WoW accounts, the game asks which one to
use. The sidebar then shows a **WoW account** choice above **Signed in**. Pick
an account there and the game starts with it. **Ask in the game** brings the
game's question back. With one WoW account the game never asks. Each Battle.net
account keeps its own choice.

### Several Battle.net accounts

Once you are signed in, the sidebar shows **Signed in** with your account's
BattleTag, or "Account 1" when Battle.net does not send one. Click it to open
the account menu:

- **Add account…** signs in to another Battle.net account through the browser,
  the same way as the first time. The new account becomes the active one.
- Pick an account in the list to switch to it. **Play** always uses the active
  account, and each account keeps its own session.
- **Sign out of this account** signs out of the active account only. The
  others stay signed in.

You cannot switch or sign out while a game is starting. A game that is already
running keeps the account it started with. Clients do not remember an account
of their own, so the sidebar always shows the account the next **Play** uses.
When Battle.net ends an account's session, **Play** asks you to sign in to that
account again. If you sign in to a different account instead, Waystone adds it,
makes it active and stops, so a game never starts as an account you did not
pick.

When you update from a version with a single sign-in, you stay signed in.
Waystone moves that session into its new place without signing in again.

## Shortcuts

An installed client's **⋯** menu offers **Add to app launcher**, which is
**Add to Start menu** on Windows. The entry starts that client directly, and
Classic Era's entry offers its Era, Hardcore and Season of Discovery modes as
actions. Without a mode it starts the mode last chosen in Waystone.

**Add to Steam** appears when Steam is installed and adds the client as a
non-Steam game for every Steam account on the computer, with its tile as icon
and library artwork. Steam rewrites its list of games while it runs, so quit
Steam first. Waystone asks you to when it finds Steam running. The client shows
up in the library when Steam starts again. Waystone keeps a copy of the old
list as `shortcuts.vdf.waystone-backup` next to it. Steam installed through
Flatpak cannot start programs outside its sandbox and is not offered.

A shortcut works like **Play**. With a valid sign-in and an up-to-date client,
the game starts and no Waystone window appears. When the client needs a sign-in
or an update, is still downloading, or does not start, the Waystone window
opens on it instead. If Waystone is already open, the shortcut hands over to
that window. Only one Waystone runs at a time, and starting it again brings its
window to the front.

The same menu removes the entries again. Uninstalling a client removes its app
launcher entry. Its Steam entry stays until you remove it, and then opens
Waystone on the client. Waystone only removes files it made itself.

The entries run the `waystone` your profile installs, an AppImage's own file,
or `Waystone.exe`, so they keep working after an upgrade.

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

## Bug reports

**Settings › Diagnostics** helps with a bug report. **Copy diagnostics** puts
a short plain-text report on the clipboard, ready to paste into an issue. It
lists Waystone's version and how it was installed, the system, Python and Qt,
the Proton build, `umu-run`, GameMode and MangoHud, the region, language and
download settings, the installed clients with their update state, the
downloads with the error of a failed one, and the last lines of the Proton log.

The report leaves out your Battle.net sign-in, WoW account names, account ids,
your user name and your home folder, which it writes as `~`. It never reads
the saved session. Errors and log lines are cleaned of addresses, tickets and
account names before they go in. Read the report before you share it all the
same.

On Linux, **Open Proton log** opens `steam-battlenet.log` once a game has
written one, and **Open logs folder** opens `~/.local/share/waystone/logs`.
Turn on **Proton log** under **Settings › Launch** first.

`waystone diagnostics` prints the same report in a terminal.

## Notifications and tray

Waystone shows a desktop notification when a download or update finishes or
fails, when a client has a new version, when a start fails, and when a game
quits unexpectedly. A new version is announced once, also across restarts.
Nothing is shown for the client the window shows right now. Click a
notification to open Waystone on that client. Notifications go through the
desktop's own service, so its do-not-disturb mode applies. On Linux that is the
freedesktop notification service, which works without a tray. Turn them off
with **Show notifications** in **Settings › General**.

A game counts as quitting unexpectedly when it writes a crash report into the
`Errors` folder of its game folder, or, on Windows, when it exits with an error
code. Force quit is not a crash.

Where the desktop has a system tray, Waystone puts its icon there. Its menu
shows Waystone, the current download with **Pause all** and **Resume all**,
up to five installed clients to start, the most recently played first, and
**Quit Waystone**. Starting a client there works as its main button does. It
updates the client first when needed, and opens the sign-in page when there is
no session.

Turn on **Keep running in the tray when the window is closed** to let
downloads carry on after the window closes. **Quit Waystone** in the tray then
pauses them and keeps the queue, as closing the window does without the tray.
Without a system tray, as on Wayland compositors without a tray bar, the
setting is greyed out and closing the window quits.

## Command line

```sh
waystone --root /games/Waystone clients
waystone --root /games/Waystone install wow --region eu --locale enUS
waystone --root /games/Waystone install wow --seed-from "/games/World of Warcraft"
waystone --root /games/Waystone update wow
waystone --root /games/Waystone verify wow
waystone --root /games/Waystone play wow_classic_era --mode hardcore
waystone diagnostics
waystone play wow_classic_era --mode hardcore --shortcut --wait
```

The commands refuse to run while the Waystone window is open, since the window
may be downloading or starting a game in the same folder. `clients` and
`diagnostics` only read, so they also run beside the window. `play --shortcut` is
what the shortcuts run. It uses the window's installation
folder and opens the window when it needs a sign-in or an update, where a plain
`play` asks for the sign-in in the terminal. `--wait` keeps the command
running until the game exits, as Steam needs.

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
`tools/check_move_mutations.py` does the same for moving the installation folder,
with its report under `.pytest_cache/move-mutations`.

## Linux AppImage

`Waystone-<version>-x86_64.AppImage` runs on distributions without Nix, such as
Ubuntu 22.04 and newer, Fedora, Arch and SteamOS. Python, Qt, umu-launcher and
the bridge are inside. Build it on a machine with Nix:

```sh
nix build .#appimage
ls result/Waystone-0.1.0-x86_64.AppImage
```

Copy the file anywhere, make it executable and start it:

```sh
chmod +x Waystone-0.1.0-x86_64.AppImage
./Waystone-0.1.0-x86_64.AppImage            # the window
./Waystone-0.1.0-x86_64.AppImage --help     # the command line
```

It needs FUSE, which desktops install as `fuse3`. Without it,
`--appimage-extract-and-run` unpacks the file to a temporary folder first. That
folder is deleted when Waystone exits, so close the game before Waystone.

The AppImage keeps its settings, session and prefix in the same folders as the
Nix package, so you can switch between them. Proton still comes from Steam,
ProtonUp or another launcher. Closing a game needs `nsenter`, which every
distribution ships with util-linux.

On NixOS, use the Nix package. To try the AppImage there anyway, run it with
`appimage-run` and add `zstd` to its libraries. `packaging/appimage/test-ubuntu.sh
result/Waystone-0.1.0-x86_64.AppImage` runs it in a plain Ubuntu container with
podman, without network, and checks the command line, the window on X11,
Wayland and offscreen, and the bundled umu-run.

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
