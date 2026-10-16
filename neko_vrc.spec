# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)
from pathlib import Path
from xml.etree import ElementTree


# Keep the packaged backend's Windows version in sync with the default frontend.
version = ElementTree.parse(
    Path(SPECPATH) / 'desktop/NekoVRC.WinUI/NekoVRC.WinUI.csproj',
).findtext('./PropertyGroup/Version')
version_parts = tuple(int(part) for part in version.split('.'))
file_version = (version_parts + (0, 0, 0, 0))[:4]
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=file_version, prodvers=file_version),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('FileDescription', 'Neko-VRC Backend'),
            StringStruct('FileVersion', version),
            StringStruct('InternalName', 'Neko-VRC.Backend'),
            StringStruct('OriginalFilename', 'Neko-VRC.Backend.exe'),
            StringStruct('ProductName', 'Neko-VRC'),
            StringStruct('ProductVersion', version),
        ])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ],
)


openvr_data = collect_data_files('openvr', includes=['*.dll'])


a = Analysis(
    ['neko_backend.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('assets/shocking_vrchat.ico', 'assets'),
        *openvr_data,
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Neko-VRC.Backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/shocking_vrchat.ico'],
    version=version_info,
)
