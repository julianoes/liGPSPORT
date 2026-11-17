# PyInstaller spec for the macOS (arm64) app bundle.
#
# From the repo root, with '.[gui]' and pyinstaller installed:
#
#   python packaging/macos/make_icon.py build/macos/ligpsport.icns
#   pyinstaller --noconfirm packaging/macos/ligpsport-gui.spec
#
# Produces dist/liGPSPORT.app. CI wraps it in a .dmg
# (.github/workflows/macos-app.yml).

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]
ICON = ROOT / "build" / "macos" / "ligpsport.icns"
VERSION = re.search(
    r'__version__ = "([^"]+)"', (ROOT / "ligpsport" / "__init__.py").read_text()
).group(1)

# bleak picks its backend at runtime, and ligpsport imports the
# simulator lazily, so static analysis misses both.
hiddenimports = collect_submodules(
    "ligpsport", filter=lambda name: name != "ligpsport.bluez"
) + collect_submodules(
    "bleak",
    filter=lambda name: not any(x in name for x in ("bluezdbus", "winrt", "p4android")),
)

a = Analysis(
    [str(ROOT / "packaging" / "macos" / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "ligpsport.bluez", "dbus_fast"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="liGPSPORT",
    console=False,
    target_arch="arm64",
    argv_emulation=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="liGPSPORT")

app = BUNDLE(
    coll,
    name="liGPSPORT.app",
    icon=str(ICON),
    bundle_identifier="io.github.ligpsport",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "liGPSPORT",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        # Without this key macOS kills the app the moment CoreBluetooth
        # is touched (bleak's first scan).
        "NSBluetoothAlwaysUsageDescription": (
            "liGPSPORT connects to your iGPSPORT bike computer over Bluetooth "
            "to download activities and upload routes."
        ),
    },
)
