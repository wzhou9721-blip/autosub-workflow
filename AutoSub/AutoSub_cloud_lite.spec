# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# UI runtime
tmp_ret = collect_all('qfluentwidgets')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

# Cloud Lite keeps ffmpeg for extracting audio from video files, but excludes
# local Whisper inference stacks. Users who need local transcription should use
# the Full build or install the local component separately.
binaries += [('bin/ffmpeg.exe', 'bin'), ('bin/7za.exe', 'bin')]
datas += [('logo.ico', '.'), ('glossary_example.txt', '.')]

excludes = [
    'tkinter',
    'matplotlib',
    'ipython',
    'notebook',
    'scipy.notebook',
    'unittest',
    'faster_whisper',
    'ctranslate2',
    'onnxruntime',
    'modelscope',
    'av',
]

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
    name='AutoSub_Cloud_Lite_v1.0.0',
)
