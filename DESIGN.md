# Waystone design

Waystone installs, updates and launches the public World of Warcraft clients
without the Battle.net desktop client. It runs on Linux and is structured so
that a Windows build needs only a different runtime layer.

## Goals

- One installation folder holds every installed client and shares their content.
- A slim selector lists the public clients. Every client can be hidden, and each test or
  other public client can be shown on its own.
- Battle.net sign-in through the user's own browser, renewed automatically, for one
  account or several with quick switching.
- A download manager with a queue, pause and resume, a bandwidth limit, and
  cleanup of content no installed client needs.
- Updates are checked before launch and can run while Waystone is open.

Not in scope: account ownership filtering, starting a client before its
download finished, and delta patches. The game
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

### Moving the installation folder

**Move…** beside the folder in Settings moves the whole root to an empty or new
folder. It runs in `move.py` on the DownloadManager worker as a `MoveTask`,
which is not a job. While it is queued or running, `enqueue` refuses new jobs,
the client page disables its actions, `Launcher.play` refuses to start, and the
download bar shows the move.

The move refuses running clients, existing jobs, paused and failed ones too,
and a pending uninstall. The destination must be empty or missing, with an
existing parent. It must not lie inside the root, nor the root inside it. The
comparisons use `normcase` and `samefile` on existing ancestors, so another
spelling on a case-insensitive drive or a bind mount of the root counts as
inside. The root is walked under the library lock as uninstall walks it. Links,
junctions, special files and anything mounted inside refuse the move, and the
mount table decides for Linux bind mounts.

The journal is `move.json` in Waystone's data folder, outside both roots,
because a rename carries the root's own `.waystone` along. It records the
token, both paths, the mode, the phase and whether the move created the
destination. Each folder taking part carries `.waystone/move.json` with the
token. `Library.locked()` refuses a folder with this marker and checks it under
the lock that the move holds while it writes the marker. After that the lock is
released, since Windows renames or deletes no folder while a file inside is
open. A lock file next to the journal keeps a second Waystone from moving or
recovering at the same time.

- **One file system.** The source and the destination must both be ordinary
  folders, not mount points. One `os.rename` moves the root. The marker moves
  with it, so it shows which side the root is on after a crash. The setting is
  saved, the marker cleared and the journal dropped. A failed save renames the
  folder back.
- **Across file systems.** The rename fails with `EXDEV`, or
  `ERROR_NOT_SAME_DEVICE` on Windows, and the journal switches to copying. The
  free space must cover the root plus 1 GB. The destination gets its marker
  first, then every folder and file. Each file is synced, and `identity.json`
  goes last, so a partial copy is never taken for an installation. Locks and
  SQLite's `-shm` files are left out. The check compares the walk of both trees
  and every size. It compares the store's records in `.waystone` and the
  published indexes whole, and both ends of every other file over 4 MB. The
  copied store must then open and read back the newest object of each segment.
  Then the journal's phase becomes `switched` and the setting is saved. The
  phase becomes `deleting` and the source is deleted, its marker last. The
  copy's marker is cleared only after that, so no other process starts a job in
  it meanwhile. Cancel works until the switch. It raises inside the copy, and
  the rollback deletes what carries the token.

Recovery reads the journal. A move that switched is finished: the setting is
saved, unless the user pointed it at a third folder since, and the source is
deleted. That needs the copy to still be an installation. If it is gone and
the phase is still `switched`, the source is whole and the move is undone.
Once deleting began, nothing more is deleted and the error is shown. A setting
that fails to save keeps its old value in memory too, so no later step takes
the new folder as saved and deletes the source. Any other move is undone: the partial copy is deleted, its own folder
too when the move created it, and the source's marker is cleared. Recovery
deletes only a folder that carries the journal's token, so a folder the user
put there after a crash stays. It also removes the debris of a marker write
that a crash cut short. A source with a running game, or with something that
fails to delete, stays with its marker and journal for the next start. The
window settles the setting and the markers before it opens the library, which
takes no time. The worker then deletes the other folder as a settling
`MoveTask`, so a large deletion never holds up the window. The command line
recovers in full before it opens the library. Within a running Waystone, a
failed or cancelled move runs the same recovery at once, from the journal on
disk rather than from its own copy, which a failed write can leave ahead. A test kills a real
process at every rename, write, copy and deletion of both modes and checks that
one complete root remains and that recovery ends at it.

Nothing inside the root names its own location. Client states, plans,
`.build.info` and the store's catalogue use paths relative to the root.
`downloads.json` names the root, but a move needs an empty queue. The new
manager writes the new root with its next save. The shared Wine prefix stays in
the data folder and maps `Z:` to `/`, so the game reaches any root. The game
writes `InstallPath` under `HKLM\Software\Wow6432Node\Blizzard Entertainment\World of Warcraft`
and Windows Error Reporting entries itself, with the folder it starts from. It
rewrites them on its next start, and Waystone reads neither, so the move leaves
the prefix and the Windows registry alone.

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

### Importing an installation

An import is an install job with a seed source. `downloads.json` keeps the
source's path and the choice to copy settings, so a paused or restored import
seeds again from the same folder. Trying a failed import again keeps both.
Before the job takes the library lock it checks that the source still has
`.build.info`, `Data` and CASC indexes. A source on a disconnected drive fails
the job with that reason before anything is read or planned. A source that
disappears while it is read makes the seed skip the missing objects, and the
CDN supplies them. Every stored object is verified either way.

The source is only ever read. The seed opens it read-only and keeps its index
cache in the library's state folder. Enqueueing refuses a source that contains
or lies inside the installation folder by name, since it runs on the window's
thread. Before the job creates the installation folder, it compares the
folders themselves, through links and by device and inode, because a
case-insensitive mount names one folder `WoW` and `wow`. Uninstalling, cleanup
and every other library operation stay inside the installation folder, so they
never reach it. The window touches the source only through workers, so a
stalled drive never freezes it.

`imports.Search` runs on a worker thread and yields installations as it finds
them. It checks each place to a fixed depth, never follows a symbolic link or
junction, visits each folder once by device and inode, reads at most a few
thousand entries per folder and stops after 12 seconds or 20,000 folders. On
Linux it skips places, folders and recorded paths on network file systems
listed in `/proc/self/mounts`. On Windows only local fixed drives are searched,
recorded paths included. A folder that hangs in the kernel cannot be
interrupted, so the page stops waiting a few seconds after the search's
deadline and the worker is left to finish on its own. The places are `~/.wine`,
`~/Games`, Bottles and Flatpak Bottles, every Steam `compatdata/*/pfx` of every
Steam library, and `/mnt`, `/media` and `/run/media/<user>`. They are listed on
the worker when the search starts, inside its time budget. A folder holding
`drive_c` is searched as a Wine prefix. On Windows they are Program Files and
every local fixed drive. Battle.net's `ProgramData/Battle.net/Agent/product.db`
is a protobuf file whose strings include each installation's path. Every string
that looks like a Windows path is checked, which needs no knowledge of its
field numbers. Inside a prefix, `C:` maps to `drive_c` and other drives to the
prefix's `dosdevices` links.

A folder is an installation when it has `.build.info` and a `Data` folder with
CASC indexes and is not a Waystone folder. Its clients are its subfolders whose
`.flavor.info` names a product, or whose name Battle.net uses, such as
`_classic_era_`. A subfolder counts only when `.build.info` lists its product
or it holds a game executable, since Battle.net leaves a removed client's
folder behind with its settings.

Copying settings runs after a successful install under the library lock. It
walks the source client's `WTF` and `Interface` folders without following
links, copies regular files only and reuses a target folder of the same name in
any case, such as the `INTERFACE` an install creates. An existing file is kept
unless it is still byte for byte the `Config.wtf` the install wrote. Each file
is written through a temporary file and renamed, so a crash leaves no partial
file that a later run would keep. A target folder that is a link is skipped. A
failed copy is reported on the client page and leaves the install complete.

Adopting a wowupdater installation stays on the command line. It renames the
source into the installation folder, which conflicts with leaving an imported
installation untouched.

## Components

| Module | Responsibility |
| --- | --- |
| `ngdp` | version and CDN discovery, manifests, BLTE, CASC store |
| `library` | the installation folder, its clients, plans, activation, verification and cleanup |
| `downloads` | the queue, pause and resume, bandwidth limit and progress events |
| `imports` | finding installations to import, their clients, copying their settings |
| `auth` | browser sign-in, renewable session, game ticket issue |
| `runtime` | Wine or native Windows, the bridge, Proton discovery, game processes |
| `ui` | the PySide6 interface |
| `cli` | the same operations for scripts and tests |
| `diagnostics` | the plain-text report for bug reports, without private data |
| `instance` | one window per user, and handing later starts to it |
| `shortcuts`, `steam` | app launcher entries, Start menu links and Steam's non-Steam games |

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

### Several Battle.net accounts

Waystone keeps one renewable session per Battle.net account. One of them is
active, and every Play uses the active one. The sessions live in the data
directory:

```text
session/
  session.lock          one lock for renewals, sign-ins, switching, sign-outs and the migration
  history.jsonl         credential events, without credentials or account ids
  active                the key of the active login
  game-token.dpapi      the game ticket, only between a renewal and the game's start
  logins/<key>/
    session.dpapi       the renewable ticket, protected with DPAPI
    login.json          its number and its BattleTag, when Battle.net sent one
    accounts.json       its WoW accounts, as described above
```

- **Key.** A login is keyed by the Battle.net account id from the logon's
  `LogonRecord`, the same id `game_accounts` in the settings uses. It is never
  keyed by the email, and the email is never stored. Only a session from an
  older Waystone whose account id was never read uses the key `unknown`. Its
  first renewal reads the id and renames the folder. A folder without
  `session.dpapi` is not a login. Folders are `0700` and files `0600` on
  Linux, and every ticket is DPAPI-protected as before.
- **Label.** The sidebar names a login by its BattleTag, taken from the
  `LogonRecord` field that has the form `Name#1234`. Without one it is
  "Account 1", "Account 2" and so on. The number is chosen when the login is
  added and kept, so a sign-out never renames the others. The BattleTag stays
  in `login.json` and is never written to the settings.
- **Active.** `active` holds the active key and is written atomically. When it
  is missing or names a login that is gone, the login with the lowest number is
  active. With one login that is always the one.
- **Launch.** A launch takes the session lock, reads the active login once, and
  uses that login for the renewal, the game ticket and the WoW account choice.
  Switching only rewrites `active` under the same lock, so it can never swap the
  session of a launch in progress. The sidebar also offers no switching while a
  launch is between Play and Running. A running game already holds its ticket.
- **Renewal.** Each renewal checks that the logon's account id is the login's
  key before it saves anything. A different id counts as a rejected session. A
  rejected session deletes that login's folder only, and the browser sign-in
  starts for it. When the browser then signs in to another account, that
  account is saved as its own login and becomes active, and the launch stops
  with a message naming both. Play therefore never starts an account other than
  the one the sidebar shows.
- **Adding.** **Add account…** runs the browser sign-in. The redirect ticket goes
  to one `VerifyAuthToken` and is then dropped. The new session is saved under
  the id that logon returns and becomes active. Signing in again to an account
  that is already listed replaces its session and keeps its number and choices.
- **Signing out.** **Sign out of this account** deletes the active login's
  folder and nothing else. The next login by number becomes active. The WoW
  account choice in the settings stays, as it does today.
- **Per client.** Clients do not remember the account they were last played
  with. The sidebar shows one active account, and Play uses it. A per-client
  account would either switch the sidebar whenever another client is selected or
  start a game as an account the sidebar does not show. Both make it harder to
  see which account a Play uses.

The sidebar's sign-in row is the switcher. Signed out it reads "Not signed in"
with **Sign in**, as before. Signed in it reads "Signed in" with the active
login's label as a menu. The menu lists every login with the active one
checked, then **Add account…** and **Sign out of this account**. The WoW account
row below shows the active login's WoW accounts.

#### Migration

A session from an older Waystone is `session.dpapi` and `accounts.json`
directly in `session/`. Waystone moves it while it holds the session lock, the
same lock an older Waystone renews under, so the two never interleave. Its key is
the owner in `accounts.json`, or `unknown` without one.

1. The new login's folder is created, and `accounts.json` and `login.json` are
   written into it. A folder without a session is not a login yet.
2. `session.dpapi` is renamed into the folder. This rename is the commit.
3. The old `accounts.json` is deleted.

A crash before the rename leaves the old session in place, and the next start
repeats the steps. A crash after it leaves a complete login and an old
`accounts.json`, which the next start deletes. No ticket is used or renewed by
the migration, so the user stays signed in. Every read of the logins migrates
when it can take the lock. While another process holds it, or while the move
fails, the old session is listed and renewed in place under the same key. A session that an older Waystone writes
later, after a new sign-in, is newer and replaces the login with its key.
Going back to an older Waystone afterwards needs a new sign-in.

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

## Shortcuts

### One Waystone per user

The process that shows the window holds `window.lock` in the data folder for
its whole life and answers on a local socket. That is a Unix socket in
`$XDG_RUNTIME_DIR`, or a named pipe on Windows, named after a hash of the data
folder. Every start first tries the lock. A start that finds it taken sends its
request to the socket and ends. A plain start brings the window to the front.
A shortcut's start asks the window to play. Both sides prove they know
`window.key`, a random secret in the data folder with owner-only permissions,
so another user cannot drive the window. The lock goes with the process, so a
crashed window never blocks the next start. Its leftover socket is removed by
the next owner of the lock. A window that holds the lock but does not listen
yet gets 30 seconds. So the library, the download queue and the session are
only ever used by one process, and a shortcut never fights a running window
for the library lock. The other commands, such as `update` or a plain `play`,
hold the same lock while they run and refuse to start while a window holds it.
`clients` only reads and needs no lock.

### What a shortcut runs

A shortcut runs `waystone play <client> --shortcut`. Classic Era's desktop
actions add `--mode`, and Steam's entry adds `--wait`. Without a window to hand
over to, the process becomes the window's process but keeps the window hidden,
and presses Play itself. The window's own Play then checks for an update,
renews the session and starts the game, so a shortcut behaves exactly like the
button. The window shows itself, on the client's page, as soon as it has
something to say: a sign-in page, an update it queued, an unfinished download
or update, a client that is not installed, or a start that failed. Once shown,
it stays open as a normal window. A hidden process quits once the game runs,
or with `--wait` once it has exited, which keeps Steam showing the game as
running. A window that took over a shortcut with `--wait` keeps the
connection open and answers when the game has exited. Its first answer names
the game's executable, so a shortcut whose window closes before the game keeps
waiting on the game's process itself. A shortcut for a game
that already runs starts nothing and waits for that game.

The command has to survive upgrades.

- Under an AppImage it is `$APPIMAGE`, the image's own file. The program
  itself runs from a temporary mount.
- The packaged Windows build uses its own `Waystone.exe`.
- A Nix store path changes with every upgrade and goes with garbage
  collection. The Nix wrapper also puts its store folder first on `PATH`. So
  Waystone walks `PATH` for a `waystone` outside the store (`NIX_STORE_DIR`, by
  default `/nix/store`) that resolves to the program running, such as
  `~/.nix-profile/bin/waystone` or `/run/current-system/sw/bin/waystone`.
  Without one, as with `nix run`, it uses the program's own path.
- A development run uses `python -m waystone`.

### Entries and markers

On Linux the entry is `waystone-<client>.desktop` in
`$XDG_DATA_HOME/applications`, with its icon in the hicolor theme under the same
data folder at 48, 64, 128 and 256 pixels. The icon is the client's tile drawn
in code. Its `TryExec` hides the entry once the program is gone. On Windows a
link in the Start menu's `Waystone` folder points at `Waystone.exe` with an
icon file in the data folder. Windows' own shell object writes it through
PowerShell, to a temporary name that is then renamed into place.

Waystone removes only files that carry its marker for that client: the
`X-Waystone-Client` key in a desktop entry, a `tEXt` chunk named `Waystone`
in every PNG it writes, including Steam artwork, and the `--shortcut` argument
in a Windows link or a Steam entry. A file with the same name but no marker is
neither replaced nor removed. Uninstalling a client removes its launcher entry.
Its Steam entry stays, since Steam may be running. It then opens the window on
the uninstalled client.

### Steam

Steam keeps non-Steam games in `userdata/<account>/config/shortcuts.vdf`, a
binary VDF file. Waystone finds Steam at `~/.steam/steam`, `~/.steam/root` or
`Steam` in the data folder, or through the registry on Windows, and changes the
file of every account that signed in there. Flatpak's Steam cannot start host
programs and is not looked for.

- The parser keeps every key and value as raw bytes, so writing a parsed file
  gives back the same bytes. A field of a type whose length it does not know
  stops the change before anything is written.
- Steam holds the list in memory and writes it back while it runs, which would
  drop the change. Waystone looks for this installation's `steam` process by
  its path inside the Steam folder and asks the user to quit Steam first. A
  Steam it misses can only undo the change, since Steam writes its own complete
  list.
- Every account's file is read and checked before the first one is written.
  The new file must keep every other entry byte for byte. The old file is
  copied to `shortcuts.vdf.waystone-backup` and the new one is written to a
  temporary file and renamed into place, with the old file's permissions.
- Waystone's entry is appended with the next free index and the fields Steam
  writes for a non-Steam game. Its app ID is Steam's own derivation, the CRC32
  of the quoted program and the name with the high bit set, which also names
  the portrait, wide and icon artwork in `config/grid`. Artwork the user chose
  in Steam has no marker and is kept.
- Removal takes out only Waystone's entries for that client and leaves the
  other entries' indexes as they are.

## Settings and state

Settings live in the platform's configuration directory. Credentials live in
the platform's data directory with owner-only permissions and DPAPI protection.
The settings file is written under a lock, since the window and a move on the
download worker can save at once. The settings page changes the folder only
through Choose…, never from its field, so an older field cannot undo a move.
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

The settings page has five sections: General for the folder, region,
language, notifications and the tray, Clients for the three categories,
Downloads for transfer controls, Launch for the Proton build and the Linux
launch options, and Diagnostics for bug reports.
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

## Diagnostics

`diagnostics.report` builds a plain-text report from the settings, the library,
a copy of the queue and the window's last update check. `waystone diagnostics`
builds it from the saved settings and `downloads.json` instead. The window
builds it on a worker thread, since it asks `umu-run --version` with a five
second limit and reads the library, and copies it to the clipboard. The
settings page's note confirms the copy.

Each part is collected on its own. A part that raises becomes one line naming
the error type, never its message, so a missing Proton, umu-run, log or
library leaves the rest intact. Windows leaves out the runtime and the Proton
log, since the game starts directly and Proton writes the only log.

Privacy holds in three layers.

- What is read: the session, the game ticket and the credential history are
  never opened. Only the WoW account list is read, to learn the names and ids
  to remove. Launch option variables and wrapper arguments appear by name
  only. Desktop entries and Steam files are never read.
- What is kept from the Proton log: its last 10 lines from the final 64 KB,
  without lines that mention a token, a ticket or a game account.
- What the finished text loses: query strings of addresses, values after
  names such as `ST`, `WEB_TOKEN` and `GAME_ACCOUNT`, ticket shapes, emails,
  BattleTags, long base64 or hex runs with a digit, the home folder in either
  separator style as `~`, the user name, account names and account ids. The
  user name and account names are replaced as whole words. The ids are
  replaced as whole numbers, outside version strings. Lines are shortened to
  300 characters only after this, so a cut never hides part of a secret from
  the patterns.

## Notifications and tray

`ui/notifications.py` shows desktop notifications. `ui/tray.py` holds the tray
icon and its menu, which the window fills from its own state on every refresh.

### Notifications

- On Linux a notification goes to `org.freedesktop.Notifications` on the
  session bus, so it shows without a tray and the desktop's do-not-disturb
  applies. PySide6's QtDBus cannot marshal the `uint32` that `Notify` takes, so
  Waystone sends it with `jeepney`, a pure-Python D-Bus library.
- The connection stays open for the whole run. KDE and dunst send a click only
  to the connection that showed the notification. The `default` action of a
  known notification id opens the window on its client.
- All D-Bus work runs on a thread of its own with a 5 second reply timeout, so
  a missing bus, a missing service or a hung one never holds up the window. A
  failure falls back to the tray icon's message, or to nothing, and the next
  notification connects again. A body is escaped when the service reads markup.
- On Windows the tray icon shows the notification, which the system turns into
  a toast that follows Focus assist.
- Nothing is shown for the client the active window shows, or for downloads
  while the Downloads page is open. **Show notifications** turns them all off.

What counts:

| Event | Detected by |
| --- | --- |
| A download finished or failed | the job's status going from running to done or failed, seen by a download manager listener on the thread that changed it. A failure removed later, or one restored on start, is not a new one |
| A new version | the update check. `announced_updates` in the settings keeps the last version announced per client, so each version is announced once, also across restarts |
| A start that failed, or a needed update | the same results the window's banner shows. They matter for a start from the tray |
| A crash | a file in the game folder's `Errors` folder newer than the start, or, on Windows, a non-zero exit code. On Linux the bridge returns 0 after any game exit, so only the crash report counts there. A Force quit is never a crash |

### Tray

The icon is created on every desktop, and it counts as reachable only while
`QSystemTrayIcon.isSystemTrayAvailable()` is true, which a tray bar that starts
later can change. Its menu has Show Waystone, the current download with Pause all and Resume all, up
to five installed and listed clients by last play, and Quit. A client's entry
reads as the main button would, Play, Sign in and play or Update, and runs the
same `MainWindow.start`. It is greyed out while the client has a job, is
starting or running, and during a sign-in, when the window locks the other
clients too. A sign-in started from the tray shows the window.

With `keep_in_tray`, closing the window hides it while the tray is available.
Downloads and games carry on. A hidden window turns off quitting on the last
closed window, so a closing menu cannot end the process. A timer checks the
tray every 5 seconds while the window is hidden and shows the window when the
tray went away, so there is never a process the user cannot reach. Quit in the
tray, Ctrl+C and a terminate request all close the window through the same
close handling as before: the queue stops, the running transfer pauses, and the
settings are saved. Without a tray the setting is greyed out with the reason,
and its saved choice is kept.
