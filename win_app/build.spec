from pathlib import Path


source_dir = Path(SPECPATH)
# Every module of the app's folder and of core/, by name: the same set the import probe loads from
# the built exe, so a module the analysis misses fails the probe and is never left out by drift here.
app_modules = sorted(path.stem for path in source_dir.glob("*.py") if path.stem != "kvm_bridge_win")
core_modules = sorted(f"core.{path.stem}" for path in (source_dir.parent / "core").glob("*.py") if path.stem != "__init__")

analysis = Analysis(
    [str(source_dir / "kvm_bridge_win.py")],
    # The repo root, for core/, which both apps share.
    pathex=[str(source_dir), str(source_dir.parent)],
    binaries=[],
    datas=[(str(source_dir / "Beamer.ico"), "."), (str(source_dir / "assets"), "assets"), (str(source_dir.parent / "VERSION"), ".")],
    # core/effects.py imports its fx_* modules by name, which PyInstaller's analysis cannot see; they are
    # among core_modules. segno is imported inside a try in qr_code.py, so it is named here as well.
    hiddenimports=["nacl", "_cffi_backend", "segno"] + app_modules + core_modules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PIL", "pystray", "tkinter", "numpy", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia"],
    noarchive=False,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="Beamer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(source_dir / "Beamer.ico"),
    # Embeds a requireAdministrator manifest so the receiver always runs elevated.
    # Windows UIPI silently drops SendInput events aimed at a focused elevated
    # window (e.g. an admin terminal) when the sender isn't elevated too.
    uac_admin=True,
)
