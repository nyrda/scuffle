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
