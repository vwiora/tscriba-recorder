# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['pystray._darwin']
binaries += collect_dynamic_libs('ctranslate2')
binaries += collect_dynamic_libs('tokenizers')
hiddenimports += collect_submodules('ctranslate2')
hiddenimports += collect_submodules('tokenizers')
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('PIL')
hiddenimports += collect_submodules('objc')
hiddenimports += collect_submodules('Foundation')
hiddenimports += collect_submodules('AppKit')
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['tscriba_recorder_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Tscriba Recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Tscriba Recorder',
)
app = BUNDLE(
    coll,
    name='Tscriba Recorder.app',
    icon=None,
    bundle_identifier='com.local.tscriba_recorder',
)
