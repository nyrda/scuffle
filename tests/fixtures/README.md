# Protocol fixtures

These small public CDN captures come from the Forever build `1.60.1.70658`,
collected anonymously on 28 October 2026. They contain no account data.

* `install.blte` is the complete 23,151-byte public install manifest. Its EKey
  is `69ab17d415ecc051c5eafd9c91f97ae1`; decoded CKey is
  `acd3d5ba96cb39bf5d3ce10e0e206fff`.
* `range-object.blte` is a complete 338-byte archive-range object, EKey
  `5b349060a972337992fa86cf8a09a94f`, decoded CKey
  `cbc5acdb227705b51b6241f0e847dbe4`.
* `en-root-ckey-page.bin` and `en-root-ekey-page.bin` are complete, unchanged
  4 KiB EN pages. The proof JSON retains their original index descriptors,
  hashes, offsets, and root lookup result. Tests put these pages into a compact
  synthetic EN envelope. The full 141 MB encoding table is not retained.
* `dl-first8-records.bin` contains the first eight unchanged 22-byte DL records.
  The proof JSON retains independently recorded EKeys, sizes, and priorities.
  Tests put these records into a header declaring eight rows and no tags.

Format references were cross-checked against CascLib and TACT.Net's primary
source. These fixtures validate real record layouts and selected authentic
pages. They do not establish hashes for omitted parts of the large manifests.

# Version service fixtures

Captured anonymously from Blizzard's public version service on 28 October 2026.

* `version-summary.psv` is `https://us.version.battle.net/v2/summary`, cut to
  its header, four products of other games and every World of Warcraft row.
* `versions/<product>.psv` are unchanged `us.version.battle.net` versions
  tables. `wow_classic_titan` publishes only a Chinese row.
* `catalog-us.json` is what discovery saved for the US region that day. The
  failure reasons are cut to the error type.

# Battle.net catalog fixtures

Captured anonymously from Battle.net's public catalog, the `catalogs` product on
Blizzard's CDN, on 3 November 2026.

* `battlenet/versions.psv` and `battlenet/cdns.psv` are the unchanged
  `us.version.battle.net` tables of the `catalogs` product. Its versions table
  has one row per catalog format, PUB-23 to PUB-30, instead of one per region.
* `battlenet/build-config` is PUB-30's unchanged build configuration. Its `root`
  names the catalog's root file.
* `battlenet/root.json` is that root file cut to what Scuffle reads: the fragment
  list, the type and category definitions and the `default` strings. The other
  languages' strings are left out. Tests serve it under its own MD5.
* `battlenet/world_of_warcraft.json` is the unchanged `world_of_warcraft`
  fragment, MD5 `17c845cc0a048338caa9faa21b8ffbdf`. Its `installs` table maps
  each install UID to its product, and the `WoW` product's `types` name each UID
  and give Classic Era's game modes.
