# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os

datas = []
binaries = []
hiddenimports = []

# Collect qfluentwidgets (essential for UI)
tmp_ret = collect_all('qfluentwidgets')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Collect faster_whisper and dependencies
tmp_ret = collect_all('faster_whisper')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Collect ctranslate2 (engine for faster_whisper)
tmp_ret = collect_all('ctranslate2')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Explicitly add bin files
# Ensure we use absolute paths or relative to spec file
binaries += [('bin/ffmpeg.exe', 'bin'), ('bin/7za.exe', 'bin')]

# Add configuration and other necessary files
datas += [('logo.ico', '.'), ('glossary_example.txt', '.')]

# Analysis...
excludes = ['tkinter', 'matplotlib', 'ipython', 'notebook', 'scipy.notebook', 'unittest', 'tiktoken', 'tiktoken_ext']
a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoSub',
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
    icon='logo.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AutoSub_Pro_v3.3_Fast_20260210',
)
