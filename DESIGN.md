# Waystone design

Waystone installs, updates and launches the public World of Warcraft clients
without the Battle.net desktop client. It runs on Linux and is structured so
that a Windows build needs only a different runtime layer.

## Goals

- One installation folder holds every installed client and shares their content.
- A slim selector lists the public clients. Every client can be hidden, and each test or
  other public client can be shown on its own.
- One Battle.net sign-in through the user's own browser, renewed automatically.
- A download manager with a queue, pause and resume, a bandwidth limit, and
  cleanup of content no installed client needs.
- Updates are checked before launch and can run while Waystone is open.

Not in scope: account ownership filtering, several Battle.net identities,
starting a client before its download finished, and delta patches. The game
reports account problems itself after login.

### Client removal

The installed client's menu opens an in-app confirmation page. It quotes the
storage card's content size as an upper bound because other clients can share
those bytes. Keeping WTF, Interface and Screenshots is checked by default to
protect settings, addons and pictures. They move to
`Waystone-backup-<code>-<uuid>` inside the managed root.

`Library.uninstall` runs under the library lock on the DownloadManager worker.
It refuses running clients, unfinished updates, and existing jobs, including
paused and failed jobs. It validates ownership against every other client's
installed and pending folder with case-insensitive comparisons. Removal checks
containment and refuses symlinks, junctions and anything mounted inside, such as
a bind mount, in game files, state, backups, and the CASC store. Linux bind mounts
of the same file system share its device number, so the mount table decides.

Before moving any files, it atomically writes `.waystone/uninstall.json`.
Readers immediately treat that client as not installed. The game folder moves
to `.waystone-trash-<uuid>`, selected settings move to the backup, and the
client's state folder moves into the trash too. Removal then rewrites
`.build.info` and removes the journal. Only renames come before that point, so
the client is either installed or removed. Deleting the trash and the cleanup
come after it and are best effort. A file that cannot be deleted, such as one
held open, stays in the trash and goes with the next library operation. It
never blocks other clients. The cleanup uses a zero compaction threshold. Other
clients' plans keep shared content alive. It waits while another client is
running, or has any unfinished download or update. Compaction gives a moved
object its new location in one commit and publishes the index before deleting a
segment, so a crash keeps every object. Every lock acquisition recovers an
interrupted removal, and startup queues recovery on the worker. Recovery is
idempotent across each rename and deletion. Once started, removal cannot pause
or cancel. Unstarted uninstall jobs are ignored when restoring `downloads.json`,
so a new launch never deletes a client from an old, uncommitted confirmation.

## Clients

Clients come in three categories.

| Category | Shown as | NGDP product | Launch `-uid` | Listed |
| --- | --- | --- | --- | --- |
| Live | World of Warcraft | `wow` | `wow` | unless hidden |
| Live | Mists of Pandaria Classic | `wow_classic` | `wow_classic` | unless hidden |
| Live | Burning Crusade Anniversary | `wow_anniversary` | `wow_classic_anniversary` | unless hidden |
| Live | Classic Era, with Era, Hardcore and Season of Discovery modes | `wow_classic_era` | `wow_classic_era` | unless hidden |
| Live | Forever, until the release channel exists | `wowf` | `wowf` | unless hidden |
| Test | Retail PTR, Retail PTR 2, MoP Classic PTR, Classic Era PTR | `wowt`, `wowxptr`, `wow_classic_ptr`, `wow_classic_era_ptr` | install UID | when chosen or installed |
| Test | Retail Beta, Forever Beta | `wow_beta`, `wow_classic_beta` | install UID | when chosen or installed |
| Other | Dark Realm Test, Retail Event, Classic Submission, Forever China Beta | `wowlivetest`, `wowe1`, `wowz`, `wow_cn_beta`, found by discovery | install UID | when shown, and chosen or installed |
| Other | any other public product | found by discovery | the product code | when installed, or shown and chosen |

The live and test clients are built in. Their labels, install UIDs and modes come
from Battle.net's public catalog (the `catalogs` product on the CDN). Its
`world_of_warcraft` fragment has an `installs` table that names each install
UID's product, such as `wow_ptr` for `wowt`, `wow_classic_anniversary` for
`wow_anniversary` and `wow_dark` for `wowlivetest`. The other clients Waystone
has a name for take their UIDs from the same table. Each client's folder name comes from its public
product configuration (`shared_container_default_subfolder`). Its `.flavor.info`
holds the NGDP product code. Every client launches its own executable, such as
`Wow.exe`, `WowT.exe` or `WowB.exe`, with `-launcherlogin -uid <install UID>`.
Classic Era modes add `-initialgamemode=cera`, `chc` or `csod`.

### Look

Waystone ships no Blizzard artwork, logos or icons. Each client's tile is drawn
in code: a gradient in the client's color with a plain glyph. Retail has a
star, Mists of Pandaria Classic a crescent, Burning Crusade Anniversary a flame,
Classic Era an hourglass and Forever a loop. Test and other public clients take
their live client's glyph, with stripes for a test client and dots for an other
one. The client page tints its header band with the client's color and draws
the glyph large and faint at its top right. The main button and the progress
bar take the client's color, darkened until white text on it reaches 4.5:1.
Pause and the other secondary states keep the neutral button.

### Discovery

`discovery` reads the version service's summary at
`us.version.battle.net/v2/summary` and the versions table of every World of
Warcraft product for the chosen region. The selector shows each client's
published version and marks a test or other build whose patch is behind its
live client as older than live, or as an old build.

A product without a built-in entry is offered only when it is public. Waystone
reads it as an install would: the product configuration, the build
configuration and the install manifest must download and decode without a
decryption key, and the manifest must hold a game executable. The vendor,
development and NEV products (`wowv*`, `wowdev*`, `wownev*`) and
`wowlivetest2` fail this check. `wow_cn_beta` passes it but supports only
Simplified Chinese, so it is offered only when the language is Chinese.
`wow_classic_titan` publishes only a Chinese build and is not listed. Unknown
public products get a label from their version line, version and code, such as
`Classic 3.4.1 (wowq)`. The catalog has no UID for them, so they launch with
their code as the install UID.

A failed check is kept as a verdict only when it would fail again, such as a
configuration that needs a key. A timeout, a connection error or a server error
gives no verdict. The product is checked again on the next refresh, and one that
was public before stays offered until then. Each client must install into a
folder of its own, so an install whose published folder another client already
uses is refused.

The result is cached per region in the cache folder for six hours. The window
starts with the cached list, or with the built-in clients when there is none,
and refreshes it on a worker thread. A product whose build did not change is
not checked again.

## Installation folder

Waystone owns its folder exclusively. Battle.net must not manage it.

```text
<root>/
  .build.info           one row per installed client
  Data/config/          build, CDN and keyring configurations
  Data/data/            shared CASC segments and indexes
  Data/indices/         CDN archive indexes
  _retail_/ _forever_/  per-client executables, settings, logs and add-ons
  .waystone/            inventory, per-client plans and state, staging
```

Content is addressed by encoding key. A key needed by several clients is
stored once. Cleanup removes keys no installed client references and compacts
the affected segments.

### Storing objects

A client has about a million objects of about 80 KB, so per-object costs set
the speed of downloads and seeding, especially on Windows, where opening a file
and committing to SQLite cost more.

- Each object is verified, appended to the newest segment and recorded under a
  SQLite savepoint, so a failed object rolls back alone and its bytes are cut.
- The segment taking appends stays open between objects.
- Records commit every 256 objects or every second. A checkpoint every few
  hundred objects syncs the segments and marks them durable.
- After a crash, recovery checks the segment tails past the last checkpoint and
  records every complete span an unfinished batch left there. A batch appended
  after bytes the game wrote into a segment cannot be found that way. It is lost
  and fetched again.
- A seed object is read once and checked in memory, from a segment file that
  stays open, since seeding reads in physical order.

### Checking files

A check compares the installed build with its plan: every object, every
installed file, the build configurations and the published indexes.

- An update plans the new build into `plan.sqlite` before it downloads. While it
  is unfinished, the check plans the installed build into
  `installed-plan.sqlite` from local content and uses that. Activation deletes
  it.
- Publishing records the end of the newest segment. Objects appended after it,
  such as an unfinished update's, are not in the published indexes yet, or are
  still published at an older location. The index check accepts that for them
  only.

### CDN requests

Every region row names its CDN hosts. For EU every WoW product currently names
only `level3.blizzard.com`, so failing over to another host is often not
possible. A CDN edge that has not cached a range first fetches it from its
origin, which can take several seconds. Content less popular than Retail's,
such as Forever's, often takes this path.

- A request waits up to 30 seconds for its first byte.
- Once the body flows, 5 seconds of silence, or less than 1 KB/s, count as a
  stall. The request then fails over to the next host or retries.
- Every object is verified against its key, so any advertised host is safe.

### Archive indexes

A download looks up which CDN archive holds each object. Blizzard often does
not publish a build's archive-group index, and Forever's builds list about 1400
archives. Waystone then fetches every archive's own index, about 7 million
locations in all.

- Indexes download in parallel and are verified before they are cached.
- One thread appends each index's rows to a staging file, committed with its
  tag, so an interrupted import resumes.
- Once all are staged, the rows are sorted into the lookup table in one
  transaction and the staging file is deleted. Inserting them index by index
  rewrote most of the table every time. That took minutes and grew the SQLite
  log to 20 GB.
- The client shows how many indexes are done.

## Components

| Module | Responsibility |
| --- | --- |
| `ngdp` | version and CDN discovery, manifests, BLTE, CASC store |
| `library` | the installation folder, its clients, plans, activation, verification and cleanup |
| `downloads` | the queue, pause and resume, bandwidth limit and progress events |
| `auth` | browser sign-in, renewable session, game ticket issue |
| `runtime` | Wine or native Windows, the bridge, Proton discovery, game processes |
| `ui` | the PySide6 interface |
| `cli` | the same operations for scripts and tests |

The interface only calls the library, downloads, auth and runtime APIs. All
long operations report progress as events. Downloads and sign-in can be
cancelled. The update check and ticket exchange before a launch take seconds
and run to completion.

## Sign-in

1. A logon without a ticket returns Battle.net's sign-in URL.
2. Waystone opens it in the default browser.
3. After signing in, the browser fails to open `http://localhost:0/?ST=...`.
   The user copies that address. Waystone reads it from the clipboard when its
   window regains focus, or the user pastes it.
4. `VerifyAuthToken` completes the logon, and `GenerateAuthToken` reserves the
   first renewable ticket.

Each launch consumes the saved ticket, reserves its successor and issues a
separate game ticket. A rejected session starts the sign-in again. The game
reads its ticket only from `HKCU\Software\Blizzard Entertainment\Battle.net\Launch Options\WoW`,
so Waystone writes `WEB_TOKEN` and `ACCOUNT_TS` there right before launch.
Waystone never reads or uses Battle.net's own saved login.

### WoW account

One Battle.net login can hold several WoW accounts. The game asks which one to
use unless `GAME_ACCOUNT` names it, as the Battle.net app writes it with
`REGION`, for example `GAME_ACCOUNT=WANDERER` and `REGION=EU`.

- The logon's `LogonRecord` lists the login's game account handles. For each
  WoW handle, `GetGameAccountInfo` (method 201 of
  `bnet.protocol.account.v2.client.AccountService`, hash `0x22dc2464`) returns
  the name the game knows the account by. This is the same call the game's own
  account choice makes.
- The lookup runs after the session and game tickets are saved, so a failure
  never costs a sign-in or a launch. A failed lookup keeps the last list of the
  same login. It is never treated as an empty list.
- The list is kept in `accounts.json` beside the session and removed on sign
  out. The choice is kept in the settings by the login's account id and names
  the WoW account by its id, never by its place in the list.
- A sole account is passed without asking. With several, the sidebar shows the
  choice beside "Signed in". Its default is "Ask in the game", which leaves the
  game's own question. A chosen account that is gone, or another login, falls
  back to that question and never to another account.
- The bridge gets `--game-account <name> <region>` before the game's path and
  writes both values next to the ticket, as Waystone does itself on Windows.
  Without a choice `GAME_ACCOUNT` is removed, so one from an earlier start
  cannot linger.

The device fingerprint is derived from a 64-byte seed under
`HKCU\...\Battle.net\Identity`. Waystone creates it if the prefix has none and
keeps it afterwards. See [docs/device-id.md](docs/device-id.md).

## Runtime

| | Linux | Windows |
| --- | --- | --- |
| Game | Proton through `umu-run`, found at the standard paths | native |
| Bridge | the Windows bridge inside the Wine prefix | none, Waystone does its work in `runtime/windows.py` |
| Prefix | one Waystone prefix, shared by all clients | none |
| Registry | the prefix's `HKCU` | the user's `HKCU` |

The bridge does DPAPI protection, the device fingerprint, the registry write
and the game start. The game runs in its own runtime and never joins another
client's container.

Smart App Control and other Windows code integrity policies block unsigned
programs, and the bridge is one. On Windows Waystone therefore does the same
work itself through `ctypes` and `winreg`, following `bridge/*.c` field for
field. CPUID runs from a few bytes of machine code, since Python has no way to
issue the instruction. Under Wine the C bridge and the Python port produced the
same fingerprint bytes, and each decrypted what the other protected.

A packaged Waystone that ships the bridge runs it from a copy in `bin` inside
its data folder, since a packaged build's own folder can go away when Waystone
exits while the bridge still waits for the game. The copy's name carries its
content hash, so an update never replaces a bridge that a running game still
uses.

### Closing a game

While a game runs, its main button reads **Close game**. Holding Shift turns it
into **Force quit**. A game that Waystone did not start, for example one left
from an earlier window, counts too. A worker lists the processes at most every
2 seconds, so the window never waits for it.

- Close asks the game's visible windows to close, as their close button does.
  The bridge's `--close-game <path>` finds the game by its file, comparing the
  volume and file index, because Wine shows one file under different drive
  letters and several clients name their executable `Wow.exe`. It exits with
  0 when it asked, 3 when the game runs without a window, and 4 when none runs.
  On Windows Waystone makes the same comparison itself.
- On Linux the game's `wineserver` lives in its pressure-vessel container. The
  bridge joins it through `nsenter` into the game's user and mount namespaces,
  with the game's own Wine and its Wine and display variables. umu's own
  re-entry needs `UMU_CONTAINER_NSENTER` at the start, which would make a
  second client join the first one's container.
- Force quit sends `SIGKILL` to the game process. It is found by its first
  argument and its folder, so the bridge and `umu-run`, which name the game in
  their arguments, are left alone. They exit when the game is gone. On Windows
  Waystone ends the process through the handle it checked the file with, so
  its ID cannot pass to another process in between.
- A game without a window, as when it hangs while quitting, or one that has
  not quit after 20 seconds, gets a banner that points at Force quit.

## Linux packaging

Linux has two packages. The Nix package wraps the app with `WAYSTONE_BRIDGE`,
`umu-launcher` and `util-linux` from nixpkgs. The AppImage is for distributions
without Nix and is built by the same flake, `nix build .#appimage`.

The AppImage holds nothing from the Nix store. Its parts are prebuilt for
portability, and each is fetched by hash in `packaging/appimage/default.nix`:

| Part | Source |
| --- | --- |
| Python 3.14 | python-build-standalone, built against glibc 2.17 |
| Qt and PySide6 6.12 | the `manylinux_2_34` wheels, the version the Nix and Windows packages use |
| xcb helper libraries | Debian 12 packages, for Qt's X11 plugin |
| umu-launcher 1.4.4 | its release zipapp, the version nixpkgs wraps |
| the bridge | the Nix mingw build, a Windows program |
| the AppImage runtime | the static type2 runtime, which needs FUSE but not libfuse2 |

The build runs in the Nix sandbox, so the same commit yields the same file, and
`nix build .#appimage --rebuild` confirms it. The wheels set the glibc baseline
at 2.34: Ubuntu 22.04, Fedora 35, Debian 12, current Arch and SteamOS 3.
python-appimage and linuxdeploy were the alternatives. They collect libraries
from the build machine, which on NixOS are Nix's own and need its newer glibc.
An AppImage of a Nix closure carries Nix's glibc and graphics stack, so Qt
would not find the host's OpenGL drivers, and it is several times larger.

Only the Qt parts Waystone uses are kept: QtCore, QtGui, QtWidgets, the X11,
Wayland and offscreen platforms, desktop themes, input methods, and SVG icons
and images. The build fails if a kept library needs a Qt or xcb helper library
that is not bundled. The xcb helpers sit next to Qt's libraries, where the
plugins' `RPATH` finds them, so the AppImage sets no `LD_LIBRARY_PATH` that the
browser or the game would inherit. Everything else comes from the host and is
part of every desktop: glibc, libstdc++, libxcb, libxkbcommon, the Wayland
client libraries, fontconfig, OpenGL, GLib, D-Bus, zstd and the CA
certificates. libxkbcommon-x11 is not bundled, since it must come from the same
release as the host's libxkbcommon. A mismatched pair crashes the X11 plugin.

`AppRun` stops with a message that names the file when its Python or the
bridge is missing. It starts the bundled Python with `-I -B`, so the user's Python variables
and packages stay out and no bytecode is written. The bytecode is compiled at
build time with unchecked hashes. `AppRun` sets `APPDIR` to its own folder and
`WAYSTONE_BRIDGE` to the bridge inside it, and puts its `usr/bin` first on
`PATH`. It leaves `APPIMAGE` as the runtime set it, so Waystone can name its own
file. The configuration, data and cache folders are the XDG ones, as with the
Nix package, so a user can switch between the two.

The AppImage's mount lasts as long as Waystone's process, since the runtime
unmounts it when the last holder of its keepalive pipe exits and Waystone's
children do not inherit that pipe. The bridge and umu-run wait for the game,
which can outlive Waystone, so neither runs from the mount:

- A `WAYSTONE_BRIDGE` inside `APPDIR` runs from the copy in `bin` inside the
  data folder, as the Windows package's bridge does. The Nix store stays, so
  the Nix package's bridge runs in place.
- umu-run is bundled, preferred over a system one as in the Nix package, and
  found by `find_umu` on `PATH`. `usr/bin/umu-run` starts the AppImage file
  again with `WAYSTONE_APPRUN=umu-run`, which gives umu-run a mount of its own
  for as long as it runs, and `AppRun` then runs the zipapp with the bundled
  Python. An extracted copy, such as appimage-run's, is not a mount point and
  runs in place. Without the zipapp it exits with a message that names it,
  which Waystone shows. umu-run downloads the Steam Linux Runtime on first use,
  as it does elsewhere.

`nsenter` comes from the host's util-linux, which every target distribution
installs. Closing a game joins the same pressure-vessel container as with Nix,
where umu-run runs inside one more FHS container. Without `nsenter` the close
fails with a message that points at Force quit.

`--appimage-extract-and-run`, for systems without FUSE, deletes the extracted
copy when Waystone exits, so a game started from it should end before
Waystone does. On NixOS the Nix package is the one to use. appimage-run's
environment lacks libzstd and needs it added to run the AppImage.

## Launch options

Each client has its own game arguments, stored by client code. They apply on
Linux and Windows. The toggles cover arguments that the current clients list in
their own usage text (`Wow.exe -help`): `-d3d12` or `-d3d11`, `-windowed` or
`-fullscreen`, `-console`, `-nosound`, `-compat` and `-nodriverext`. Extra
arguments are split like a shell command line, with backslashes kept for
Windows paths. Waystone sets `-launcherlogin`, `-uid` and `-initialgamemode`
itself, so the extra arguments may not contain them. Extra arguments that
repeat or contradict a toggle are passed as typed, since a word may be another
argument's value, and the page points them out. The game gets
`-launcherlogin -uid <uid>`, the user's arguments and then the game mode. The
product configuration's `launch_arguments` are left out. Several clients,
among them Classic, Classic Era and their PTRs, publish `-launch`, and current
games stop on it with "CommandLine Parse Error" before their window opens.

On Linux, global options shape the game process:

| Option | Effect | Notes |
| --- | --- | --- |
| GameMode | `gamemoderun` before `umu-run` | gamemode stays active while umu-run waits for the game. Left out when `gamemoderun` is missing |
| MangoHud | `MANGOHUD=1` | pressure-vessel imports the host's MangoHud Vulkan layer. The `mangohud` wrapper's `LD_PRELOAD` would only reach umu-run |
| Wayland | `PROTON_ENABLE_WAYLAND=1` | offered when the chosen Proton build reads it |
| HDR | `PROTON_ENABLE_HDR=1` | offered when the chosen Proton build reads it |
| Proton log | `PROTON_LOG=1`, `PROTON_LOG_DIR=<data>/logs` | Proton writes `steam-battlenet.log`. Waystone's quiet `WINEDEBUG` is left out so Proton picks its channels |

The game command is the user's wrapper command, `gamemoderun`, `umu-run`, the
bridge and `Wow.exe`, in that order. Its environment is the inherited one, then
Waystone's defaults, the toggles, the global variables and the client's own
variables. `WINEPREFIX`, `PROTONPATH`, `GAMEID` and `WOW_BROKER_PIPE` are applied
last and cannot be overridden. Prefix creation and bridge calls get neither the
wrappers nor the user's variables. The options are checked before the session
is renewed, so a missing wrapper never spends a sign-in. On Windows only the
arguments apply.

An option that cannot work with the chosen build or on this machine is greyed
out and shown off in the settings, while its saved choice is kept. Wayland and
HDR variables are harmless to a build that does not read them, and the
game starts without GameMode when `gamemoderun` is missing, so a setting the
user cannot change never blocks a launch. When the game does not start and any
launch option is set, the message says the options may be the cause, since the
wrappers' output is not kept.

Invalid launch options in the settings file are dropped and reported in the
window instead of stopping the start. The report comes before the notice to
choose an installation folder.

## Settings and state

Settings live in the platform's configuration directory. Credentials live in
the platform's data directory with owner-only permissions and DPAPI protection.
Settings include the installation folder, region, locale, bandwidth limit, the
clients listed, each client's launch options and, on Linux, the Proton build
with its launch options. `extra_clients` holds the test and other clients chosen
for the list, `hidden_clients` the clients left out of it, live ones too, and
`show_other_clients` whether other public clients that are not installed appear.
A client with a download stays listed until the download is gone. Client codes
the catalog does not know are kept, since discovery can find them again. An
installed client the catalog does not offer now still gets a row in Settings, so
it can be shown again after it was hidden. Entries that cannot be a product code
are dropped when loading.

The settings page has four sections: General for the folder, region and
language, Clients for the three categories, Downloads for transfer controls, and
Launch for the Proton build and the Linux launch options.
`SettingsPage.add_launch_widget` and `add_download_row` add further settings to
those sections.

The download manager writes `downloads.json` atomically in the data directory.
It records the installation root and each unfinished job's client, kind, status
and error in queue order. Completed and cancelled jobs are omitted. A different
installation root does not inherit the queue. On startup, unfinished jobs return
paused unless `resume_downloads` is enabled. Failures remain visible. Progress
is read from the pending plan and stored-object inventory, never from saved UI
counters. Missing or unreadable plans leave progress unknown until resume.

Waiting jobs can move up or down. Pause all and Resume all use the same queue
lock as individual controls and worker transitions. A resume requested while a
running transfer is stopping takes effect after it stops. `pause_while_playing`
holds new downloads too, and remembers which jobs it paused. User pauses and
cancellations remove that ownership. The last game's exit resumes only the
remaining owned jobs. Closing the window stops the queue before waiting for
its active transfer.

Downloads settings accept a whole MB/s limit, with 0 for unlimited, and 1 to 64
connections, applied when the next CDN client is created. Invalid numeric types
fall back to defaults and out-of-range integers are clamped. Both automatic
resume and pausing during games default to off. Running progress keeps the
percentage, time, speed and size fields through warm-up and stalls, using
"Estimating…" for unavailable time and speed. Rates of at least 10 MB/s use
whole MB/s. The queue total uses all known remaining sizes and the current rate,
with an explicit qualification when paused jobs need to resume.
