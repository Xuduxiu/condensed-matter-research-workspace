# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None

hiddenimports = (
    collect_submodules("streamlit")
    + collect_submodules("paper_intake")
    + [
        "pandas",
        "pydantic",
        "dotenv",
        "httpx",
        "pymupdf",
    ]
)

datas = [
    ("app.py", "."),
    ("README.md", "."),
    ("README_for_user.md", "."),
    ("docs/windows_distribution.md", "docs"),
    ("docs/zotero_integration_plan.md", "docs"),
    (".env.example", "config"),
]
datas += collect_data_files("streamlit")
datas += copy_metadata("streamlit")

a = Analysis(
    ["launcher.py"],
    pathex=[],
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
    [],
    exclude_binaries=True,
    name="LabPaperIntake",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name="LabPaperIntake",
)
