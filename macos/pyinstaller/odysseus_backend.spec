# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(os.environ.get("ODYSSEUS_REPO_ROOT", Path.cwd())).resolve()
MACOS = ROOT / "macos"

datas = [
    (str(ROOT / "static"), "static"),
    (str(ROOT / "config"), "config"),
    (str(ROOT / "licenses"), "licenses"),
    # Bundled helper binaries (e.g. dtach for Code Station session persistence) so the
    # packaged app doesn't depend on a Homebrew install. The executable bit is preserved.
    (str(ROOT / "vendor"), "vendor"),
    (str(ROOT / "requirements.txt"), "."),
]

for package_dir in ("core", "routes", "src", "services", "scripts", "mcp_servers"):
    datas.append((str(ROOT / package_dir), package_dir))

datas += collect_data_files("chromadb")

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "fastembed",
    "chromadb",
]
chromadb_hiddenimports = [
    module
    for module in collect_submodules("chromadb")
    if not module.startswith("chromadb.test")
]
hiddenimports = sorted(set(hiddenimports + chromadb_hiddenimports + collect_submodules("fastembed")))

a = Analysis(
    [str(MACOS / "backend_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="odysseus_backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="odysseus_backend",
)
