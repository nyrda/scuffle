# PyInstaller spec for the one-file Windows build. build.ps1 runs it with WAYSTONE_ICON set.
# Windows needs no bridge: Waystone does its work in runtime/windows.py.
import os
from pathlib import Path
import tomllib

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).parents[1]
project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
numbers = tuple(int(part) for part in project['version'].split('.')) + (0,)

version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('ProductName', 'Waystone'),
            StringStruct('FileDescription', 'Waystone'),
            StringStruct('ProductVersion', project['version']),
            StringStruct('FileVersion', project['version']),
            StringStruct('OriginalFilename', 'Waystone.exe'),
            StringStruct('InternalName', 'Waystone'),
        ])]),
        VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
    ],
)

analysis = Analysis(
    [str(ROOT / 'packaging' / 'entry.py')],
    pathex=[str(ROOT / 'src')],
    datas=[(str(ROOT / 'src' / 'waystone' / 'ui' / 'icon.svg'), 'waystone/ui')],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest'],
)
# Software OpenGL is a fallback for Qt Quick and OpenGL widgets. Waystone uses neither.
analysis.binaries = [entry for entry in analysis.binaries if Path(entry[0]).name.lower() != 'opengl32sw.dll']

executable = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    # Read and write text as UTF-8, as on Linux, instead of the Windows code page.
    [('X utf8=1', None, 'OPTION')],
    analysis.binaries,
    analysis.datas,
    name='Waystone',
    icon=os.environ['WAYSTONE_ICON'],
    version=version,
    console=False,
    upx=False,          # packed executables look like malware to virus scanners
)
