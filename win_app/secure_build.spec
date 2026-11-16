from pathlib import Path
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)

source = Path(SPECPATH)
version = (source.parent / "VERSION").read_text(encoding="utf-8").strip()
numeric_version = tuple(int(part) for part in version.split("-", 1)[0].split("."))
numeric_version += (0,) * (4 - len(numeric_version))
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numeric_version, prodvers=numeric_version,
                     mask=0x3F, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
    kids=[StringFileInfo([StringTable("080904B0", [
        StringStruct("FileDescription", "Beamer Secure Desktop"),
        StringStruct("FileVersion", version),
        StringStruct("OriginalFilename", "BeamerSecure.exe"),
        StringStruct("ProductName", "Beamer"),
        StringStruct("ProductVersion", version),
    ])]), VarFileInfo([VarStruct("Translation", [0x0809, 1200])])],
)
analysis = Analysis(
    [str(source / "secure_worker.py")],
    pathex=[str(source), str(source.parent)],
    binaries=[], datas=[], hiddenimports=["nacl", "_cffi_backend"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["PySide6", "PIL", "tkinter", "numpy"], noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True,
    name="BeamerSecure", debug=False, strip=False, upx=False, console=True,
    uac_admin=False,
    version=version_info,
)
bundle = COLLECT(executable, analysis.binaries, analysis.datas,
                 strip=False, upx=False, name="BeamerSecure")
