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
| Other | any other public product | found by discovery | the product code | when shown, and chosen or installed |

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

The device fingerprint is derived from a 64-byte seed under
`HKCU\...\Battle.net\Identity`. Waystone creates it if the prefix has none and
keeps it afterwards. See [docs/device-id.md](docs/device-id.md).

## Runtime

| | Linux | Windows |
| --- | --- | --- |
| Game | Proton through `umu-run`, found at the standard paths | native |
| Bridge | the Windows bridge inside the Wine prefix | the same bridge, run directly |
| Prefix | one Waystone prefix, shared by all clients | none |
| Registry | the prefix's `HKCU` | the user's `HKCU` |

The bridge does DPAPI protection, the device fingerprint, the registry write
and the game start. The game runs in its own runtime and never joins another
client's container.

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
`-launcherlogin -uid <uid>`, the product configuration's arguments, the user's
arguments and then the game mode.

On Linux, global options shape the game process:

| Option | Effect | Notes |
| --- | --- | --- |
| GameMode | `gamemoderun` before `umu-run` | gamemode stays active while umu-run waits for the game. Left out when `gamemoderun` is missing |
| MangoHud | `MANGOHUD=1` | pressure-vessel imports the host's MangoHud Vulkan layer. The `mangohud` wrapper's `LD_PRELOAD` would only reach umu-run |
| Wayland | `PROTON_ENABLE_WAYLAND=1` | offered when the chosen Proton build reads it |
| HDR | `PROTON_ENABLE_HDR=1` | offered when the chosen Proton build reads it |
| NVIDIA Reflex | `PROTON_DISABLE_NVAPI=1` when off | shown only with an NVIDIA GPU. Proton 9 and later give NVIDIA GPUs NVAPI by default, and current builds no longer read `PROTON_ENABLE_NVAPI`. The game uses NVAPI for Reflex. It has no DLSS |
| ntsync | `PROTON_NO_NTSYNC=1` when off | on by default in builds that offer the switch, and needs `/dev/ntsync` |
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
out and shown off in the settings, while its saved choice is kept. Wayland, HDR
and ntsync variables are harmless to a build that does not read them, and the
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
`show_other_clients` whether other public clients appear at all. A client with a
download stays listed until the download is gone. Client codes the catalog does
not know are kept, since discovery can find them again. An installed client the
catalog does not offer now still gets a row in Settings, so it can be shown
again after it was hidden. Entries that cannot be a product code are dropped
when loading.

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
