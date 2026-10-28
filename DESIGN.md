# Waystone design

Waystone installs, updates and launches the public World of Warcraft clients
without the Battle.net desktop client. It runs on Linux and is structured so
that a Windows build needs only a different runtime layer.

## Goals

- One installation folder holds every installed client and shares their content.
- A slim selector lists the public clients. Each test client can be shown on its own.
- One Battle.net sign-in through the user's own browser, renewed automatically.
- A download manager with a queue, pause and resume, a bandwidth limit, and
  cleanup of content no installed client needs.
- Updates are checked before launch and can run while Waystone is open.

Not in scope: account ownership filtering, several Battle.net identities,
starting a client before its download finished, and delta patches. The game
reports account problems itself after login.

## Clients

| Shown as | NGDP product | Launch `-uid` | Visible |
| --- | --- | --- | --- |
| World of Warcraft | `wow` | `wow` | yes |
| Mists of Pandaria Classic | `wow_classic` | `wow_classic` | yes |
| Burning Crusade Anniversary | `wow_anniversary` | `wow_classic_anniversary` | yes |
| Classic Era, with Era, Hardcore and Season of Discovery modes | `wow_classic_era` | `wow_classic_era` | yes |
| Forever | `wowf`, until the release channel exists | `wowf` | yes |
| Retail PTR, second PTR, Classic PTR, Era PTR | `wowt`, `wowxptr`, `wow_classic_ptr`, `wow_classic_era_ptr` | install UID | when chosen in the settings |
| Midnight beta, Forever Beta | `wow_beta`, `wow_classic_beta` | install UID | when chosen in the settings |

Titan Reforged is China only and is not listed. Each client's folder name comes
from its public product configuration (`shared_container_default_subfolder`).
Its `.flavor.info` holds the NGDP product code. Every client launches with
`-launcherlogin -uid <install UID>`. Classic Era modes add
`-initialgamemode=cera`, `chc` or `csod`.

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
itself, so the extra arguments may not contain them. The game gets
`-launcherlogin -uid <uid>`, the product configuration's arguments, the user's
arguments and then the game mode.

On Linux, global options shape the game process:

| Option | Effect | Notes |
| --- | --- | --- |
| GameMode | `gamemoderun` before `umu-run` | gamemode stays active while umu-run waits for the game |
| MangoHud | `MANGOHUD=1` | pressure-vessel imports the host's MangoHud Vulkan layer. The `mangohud` wrapper's `LD_PRELOAD` would only reach umu-run |
| Wayland | `PROTON_ENABLE_WAYLAND=1` | offered when the chosen Proton build reads it |
| HDR | `PROTON_ENABLE_HDR=1` | offered when the chosen Proton build reads it |
| NVIDIA Reflex | `PROTON_ENABLE_NVAPI=1`, or `PROTON_DISABLE_NVAPI=1` when off | shown only with an NVIDIA GPU. The game uses NVAPI for Reflex. It has no DLSS |
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

Invalid launch options in the settings file are dropped and reported in the
window instead of stopping the start.

## Settings and state

Settings live in the platform's configuration directory. Credentials live in
the platform's data directory with owner-only permissions and DPAPI protection.
Settings include the installation folder, region, locale, bandwidth limit,
the test clients shown, each client's launch options and, on Linux, the Proton
build with its launch options.
