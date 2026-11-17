"""Zip the Windows package folder for a release: Scuffle/Scuffle.exe and Scuffle/scuffle-<version>/.

Usage: zip_package.py <folder> <zip>

The entries are sorted and carry one fixed time, so the same folder makes the same zip.
"""
from pathlib import Path
import sys
import zipfile

folder, target = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(p for p in folder.rglob('*') if p.is_file()):
        entry = zipfile.ZipInfo(f'Scuffle/{path.relative_to(folder).as_posix()}', (1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        with path.open('rb') as source, archive.open(entry, 'w') as output:
            while block := source.read(1 << 20):
                output.write(block)
