# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Clawdmeter — produces a single windowed exe.
# Run from the repo root:  pyinstaller --clean --noconfirm Clawdmeter.spec
# Output: dist/Clawdmeter.exe

a = Analysis(
    ['daemon/tray_windows.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('firmware/src/logo.h', 'firmware/src'),
    ],
    hiddenimports=[
        'bleak.backends.winrt',
        'bleak.backends.winrt.scanner',
        'bleak.backends.winrt.client',
        'pystray._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Clawdmeter',
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
)
