# PyInstaller spec. EDITMAKER_ONEFILE=1 -> single EditMaker.exe, otherwise a folder (used for the MSI).
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ONEFILE = os.environ.get("EDITMAKER_ONEFILE") == "1"
FF = os.path.join(ROOT, "packaging", "ffmpeg")

datas = [(os.path.join(ROOT, "web"), "web"), (os.path.join(ROOT, "music_library"), "music_library")]
binaries = [(os.path.join(FF, n), "ffmpeg") for n in ("ffmpeg.exe", "ffprobe.exe") if os.path.isfile(os.path.join(FF, n))]
hidden = (collect_submodules("uvicorn") + collect_submodules("webview") + collect_submodules("src")
          + ["multipart", "python_multipart"])

a = Analysis(
    [os.path.join(ROOT, "packaging", "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = os.path.join(ROOT, "packaging", "editmaker.png")  # converted to .ico by PyInstaller (Pillow)
icon = icon if os.path.isfile(icon) else None

if ONEFILE:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="EditMaker", console=False, upx=False, icon=icon)
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EditMaker", console=False, upx=False, icon=icon)
    coll = COLLECT(exe, a.binaries, a.datas, name="EditMaker", upx=False)
