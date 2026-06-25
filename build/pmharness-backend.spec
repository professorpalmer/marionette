# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# SPECPATH is defined by PyInstaller and points to the folder containing this spec file
ROOT_DIR = os.path.abspath(os.path.join(SPECPATH, '..'))

# Dynamically resolve puppetmaster's directory if it is installed
pathex = [ROOT_DIR]
try:
    import puppetmaster
    puppetmaster_dir = os.path.dirname(os.path.dirname(puppetmaster.__file__))
    if puppetmaster_dir not in pathex:
        pathex.append(puppetmaster_dir)
except ImportError:
    pass

# We collect submodules automatically
hiddenimports = collect_submodules('harness') + collect_submodules('pmharness')
try:
    hiddenimports += collect_submodules('puppetmaster')
except Exception:
    pass

# Ensure we collect the web assets and catalog.json using absolute paths
datas = [
    (os.path.join(ROOT_DIR, 'harness', 'web'), 'harness/web'),
    (os.path.join(ROOT_DIR, 'pmharness', 'catalog.json'), 'pmharness'),
]
try:
    datas += collect_data_files('puppetmaster')
except Exception:
    pass

a = Analysis(
    [os.path.join(ROOT_DIR, 'harness', '_backend_main.py')],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='pmharness-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
