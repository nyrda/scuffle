# Device fingerprint and first-run identity

The launcher computes the SDK's 23-field Device-Id from Windows APIs under Wine. It can initialize a new prefix's persistent identity without installing or starting Battle.net. This removes the fingerprint's dependency on a previously prepared Battle.net prefix; account discovery and selected account/region launch settings are separate work.

The implementation is [device_id.c](../bridge/device_id.c). It runs through the captured `--pipe-device` helper and supplies the result to BGS `LogonOptions.device_id`. The browser sign-in and credential renewal paths both use it.

## Persistent seed

Battle.net build 2.53.6.17952, DLL SHA-256 `ac5f6666f2d0e076881c459551c533bc7ccdbd2544de008e36ec8619d5fe931f`, implements the seed reader/creator at preferred address `0x110d6130`:

1. Read `HKCU\Software\Blizzard Entertainment\Battle.net\Identity`, value `Identity`.
2. Reuse an existing `REG_BINARY` value of exactly 64 bytes.
3. When it needs to create one, first try `advapi32!SystemFunction036` for 64 random bytes, then `CryptAcquireContextW` plus `CryptGenRandom`.
4. Persist the bytes as `REG_BINARY`, then calculate `RGKY` by hashing those raw bytes.

The original also contains a final time-seeded generator fallback. The launcher requires one of the two strong random sources. It also rejects malformed or inaccessible existing values instead of silently replacing them. Valid existing seeds are preserved. A Linux file lock in the prefix serializes the fingerprint helper across separate Wine runtimes. A named mutex also serializes initialization among helpers in the same Wine server, and a registry flush completes a new seed's persistence before it is used.

The seed belongs to the prefix's Windows user/device context. Do not regenerate it on login, account changes, region changes, or client updates. Account profiles can share a device context. Different new prefixes initialize independent seeds. Do not substitute a random new fingerprint on every launch or copy another person's identity.

## Hash and field encoding

Each field uses 32-bit FNV-1a, with offset basis `0x811c9dc5`, prime `0x01000193`, and arithmetic modulo 2^32. For each input byte, XOR it into the accumulator, then multiply by the prime. The empty-input result is the offset basis. Fields serialize as unsigned decimal JSON integers.

| Fields | Input before hashing |
| --- | --- |
| `RGKY` | Raw 64-byte persistent identity |
| `CPGE` | `GetACP()`, uppercase hexadecimal, minimum four digits |
| `ULNG`, `SLNG` | User/system language IDs, uppercase hexadecimal, minimum four digits |
| `CNME`, `UNME` | Computer and user name buffers, with the original byte-count behavior described below |
| `UTCO` | Negated `TIME_ZONE_INFORMATION.Bias`, signed decimal minutes |
| `CARC`, `CREV`, `CLVL` | Native processor architecture, revision, and level, decimal |
| `PMEM`, `PSZE` | Total physical memory and total page-file capacity shifted right 20 bits, unsigned decimal |
| `OVER` | Version string from `RtlGetVersion`, matching the original formatter |
| `CVRA` | CPUID leaf 1 EAX, uppercase hexadecimal |
| `CFTC`, `CFTD` | CPUID leaf 1 ECX and EDX, uppercase hexadecimal |
| `CEFC`, `CEFD` | CPUID extended leaf ECX and EDX, with the original vendor masks |
| `CBRD` | CPUID brand string, preserving its bytes and spacing |
| `CVEN` | CPUID vendor string |
| `ANME`, `ADSC`, `MAC` | First `GetAdaptersInfo` record's name, description, and uppercase separator-free hexadecimal MAC |

Compatibility details matter. The original name handling hashes a character count's worth of bytes from UTF-16 buffers, not the full UTF-16 byte length. The username count excludes its terminator. The version formatter uses narrow `%s` on the service-pack buffer. Extended CPUID masks clear ECX bit 0 for Intel/AMD and EDX bit 11 for Intel. Missing CPU leaves and unavailable adapter data use the empty-input hash for those fields. The reconstruction preserves these behaviors rather than replacing them with a Linux-native approximation.

A stable seed does not freeze every other field. CPU exposure, memory, adapter enumeration, locale, timezone, and Wine's Windows version can affect the runtime-derived values. Retain the same intended Wine runtime context when comparing against the game's request.

The fingerprint format does not select a WoW account or region and does not prove account entitlement. New-device authentication remains server-controlled.

## Source and validation

The recovered functions are `0x110d6130` for identity initialization, `0x110d6370` for language/name/timezone inputs, `0x110d6530` for CPU inputs, `0x110d6910` for adapter inputs, and `0x110d6af0` for system inputs and serialization. The REA checkout retains both C and assembly under `analysis/battlenet/wow-launch/decompiled/`. Assembly resolves the CPUID register and masking details that are ambiguous in decompiled C.

The earlier game-request comparison matched all 23 reconstructed fields in the existing prefix. First-run generation has its own reproducible check:

```sh
nix build .#bridge --out-link result-bridge
python3 tools/check-device-id.py \
  --wine /absolute/path/to/DWProton/files/bin/wine \
  --bridge result-bridge/libexec/wow_auth_bridge.exe \
  --report docs/device-id-verification.json
```

The check creates disposable prefixes under the user's cache directory, starts only Wine and the helper/registry utility, and checks seed generation, the 23-field schema, `RGKY` against the persisted seed, reuse, restart persistence, independent prefix seeds, concurrent initialization, and rejection of malformed values. It shuts down only those disposable Wine servers and removes the prefixes afterward. The report contains results and executable provenance, with no seed or fingerprint values.

This check does not authenticate an account or launch the game. A successful first web login and game login from a fully independent prefix remain a separate integration check.
