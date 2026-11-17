# PyInstaller spec for the Windows package: dist/Scuffle/Scuffle.exe and the folder of its version
# beside it. build.ps1 runs it with SCUFFLE_ICON set. Windows needs no bridge: Scuffle does its work
# in runtime/windows.py.
#
# Not one file: that unpacks itself on every start, which the browser engine makes take seconds.
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
            StringStruct('ProductName', 'Scuffle'),
            StringStruct('FileDescription', 'Scuffle'),
            StringStruct('ProductVersion', project['version']),
            StringStruct('FileVersion', project['version']),
            StringStruct('OriginalFilename', 'Scuffle.exe'),
            StringStruct('InternalName', 'Scuffle'),
        ])]),
        VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
    ],
)

analysis = Analysis(
    [str(ROOT / 'packaging' / 'entry.py')],
    pathex=[str(ROOT / 'src')],
    datas=[(str(ROOT / 'src' / 'scuffle' / 'ui' / name), 'scuffle/ui') for name in ('icon.svg', 'icon-small.svg')],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest'],
)
# Software OpenGL is a fallback for OpenGL. Qt draws with Direct3D on Windows, the sign-in window too.
analysis.binaries = [entry for entry in analysis.binaries if Path(entry[0]).name.lower() != 'opengl32sw.dll']
# The sign-in window's browser runs its pages in a program of its own, with resources beside it.
# Without them the window would only fail once a user signs in, so the build fails instead.
bundled = {Path(entry[0]).name for entry in analysis.binaries + analysis.datas}
missing = {'QtWebEngineProcess.exe', 'qtwebengine_resources.pak', 'icudtl.dat'} - bundled
if missing:
    raise SystemExit(f'The sign-in window\'s browser engine is incomplete, missing {", ".join(sorted(missing))}')

executable = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    # Read and write text as UTF-8, as on Linux, instead of the Windows code page.
    [('X utf8=1', None, 'OPTION')],
    exclude_binaries=True,
    name='Scuffle',
    icon=os.environ['SCUFFLE_ICON'],
    version=version,
    console=False,
    upx=False,          # packed executables look like malware to virus scanners
    # Each version has a folder of its own, so an update unpacks beside the running one.
    # selfupdate.version_folder() knows this name.
    contents_directory=f"scuffle-{project['version']}",
)

COLLECT(executable, analysis.binaries, analysis.datas, name='Scuffle', upx=False)
