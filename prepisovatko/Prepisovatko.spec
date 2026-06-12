# PyInstaller spec — macOS .app bundle pro Přepisovátko.
# Build (na Macu, ve složce prepisovatko/):  pyinstaller Prepisovatko.spec
# Předpoklady: bin/ obsahuje whisper-cli + ffmpeg (arm64), models/ modely,
# assets/icon.icns vygenerované z assets/icon.png (viz MAC_BUILD.md).
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = [
    ("assets", "assets"),
    ("bin", "bin"),
    ("models", "models"),
]
# CustomTkinter potřebuje svoje témata/assety; sherpa-onnx nosí nativní knihovny
datas += collect_data_files("customtkinter")
binaries = collect_dynamic_libs("sherpa_onnx")

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["sherpa_onnx"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Prepisovatko",
    console=False,
    target_arch="arm64",
)

coll = COLLECT(exe, a.binaries, a.datas, name="Prepisovatko")

app = BUNDLE(
    coll,
    name="Přepisovátko.app",
    icon="assets/icon.icns" if sys.platform == "darwin" else None,
    bundle_identifier="cz.tnlrk.prepisovatko",
    info_plist={
        "CFBundleDisplayName": "Přepisovátko",
        "CFBundleShortVersionString": "1.2.0",
        "NSHighResolutionCapable": True,
    },
)
