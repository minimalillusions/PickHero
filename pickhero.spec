# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for PickHero.

Build with:  pyinstaller pickhero.spec --noconfirm
Output:      dist/PickHero.exe  (single-file, windowed)
"""

import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH) / "build_support"))
from bundle_policy import apply_policy

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
)

block_cipher = None

# ── Data files ──────────────────────────────────────────────────────────────
# sounddevice bundles the PortAudio shared library as package data.
datas = []
datas += collect_data_files("sounddevice")

# The three looks are three typefaces. Without these the build runs but every
# look falls back to Segoe UI, which is the one thing the bundling was for.
# The licences go with them: SIL OFL requires it.
datas += [("pickhero/assets/fonts", "pickhero/assets/fonts")]
datas += [("licenses", "licenses")]

# ── Native binaries / C extensions ──────────────────────────────────────────
# aubio  — C pitch/onset detection library
# pygame — SDL2 + mixer + image + font shared libraries
# numpy  — C core (multiarray, umath, etc.) — sometimes missed by analysis
binaries = []
binaries += collect_dynamic_libs("aubio")
binaries += collect_dynamic_libs("pygame")
binaries += collect_dynamic_libs("numpy")

# ── VC++ Runtime ────────────────────────────────────────────────────────────
# Bundle the Visual C++ runtime so the exe works on machines without it.
# Python ships these DLLs in its install directory.
_python_dir = os.path.dirname(sys.executable)
for _pattern in ("vcruntime*.dll", "msvcp*.dll"):
    for _dll in glob.glob(os.path.join(_python_dir, _pattern)):
        binaries.append((_dll, "."))

# ── Analysis ────────────────────────────────────────────────────────────────
a = Analysis(
    ["pickhero/main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # Audio capture & detection
        "aubio",
        "numpy",
        "sounddevice",
        # UI
        "pygame",
        "pygame.midi",
        "pickhero.ui.paint_studio",
        "pickhero.ui.paint_stage",
        "pickhero.ui.paint_hifi",
        "pickhero.lang.de",
        "pickhero.lang.en",
        # Tab loading — the pip package is "pyguitarpro", but the import is "guitarpro"
        "guitarpro",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# This application and its wheels support Windows x64. Avoid shipping unused
# x86/ARM64 PortAudio binaries. Keep x64 ASIO for existing input configurations.
# pygame's last-resort font uses the unchanged, already licensed Geist file.
a.binaries, a.datas = apply_policy(a.binaries, a.datas, SPECPATH)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PickHero",
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
