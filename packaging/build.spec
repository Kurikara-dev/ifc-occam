# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir ビルド定義 (Phase4 Task1 packaging spike)。

onefile ではなく onedir を採用する理由:
- onefile は起動時に一時展開が発生し起動が遅い
- AV誤検知(自己解凍exeを警告するベンダーが多い)が起きやすい
"""

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules

block_cipher = None

datas = []
binaries = []
hiddenimports = []

# ifcopenshell: OpenCASCADE系のDLL/データを丸ごと集める
_ifc_datas, _ifc_binaries, _ifc_hidden = collect_all("ifcopenshell")
datas += _ifc_datas
binaries += _ifc_binaries
hiddenimports += _ifc_hidden

# scipy: 通常はhookで足りるが念のためsubmodulesを明示
hiddenimports += collect_submodules("scipy")

# fast_simplification: コンパイル済み拡張。動的ライブラリを明示的に回収する
binaries += collect_dynamic_libs("fast_simplification")
hiddenimports += collect_submodules("fast_simplification")

# uvicorn: プロトコル/ロギング実装をエントリポイントから直接importしないため明示が必要
hiddenimports += collect_submodules("uvicorn")

# web/ 静的ファイル一式をdatasとして同梱 (ifc_occam/server/app.py の WEB_DIR 解決先)
datas += [("../web", "web")]

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ifc_occam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ifc_occam",
)
