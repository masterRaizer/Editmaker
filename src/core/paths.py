"""Where things live: dev checkout vs. frozen Windows build (PyInstaller)."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Read-only bundled files (web/, ffmpeg/, music_library skeleton)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return REPO_ROOT


def app_dir() -> Path:
    """Folder of EditMaker.exe (frozen) or the repo (dev)."""
    return Path(sys.executable).parent if is_frozen() else REPO_ROOT


def default_data_dir() -> Path:
    env = os.environ.get("EDITMAKER_DATA")
    if env:
        return Path(env)
    if not is_frozen():
        return REPO_ROOT / "data"
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "EditMaker" / "data"
    return Path.home() / ".editmaker" / "data"


def _user_dir(kind: str) -> Path:
    home = Path.home()
    names = {"videos": ("Videos", "Видео"), "music": ("Music", "Музыка")}[kind]
    for n in names:
        if (home / n).is_dir():
            return home / n
    return home / names[0]


def default_output_dir() -> Path:
    return (_user_dir("videos") / "EditMaker") if is_frozen() else REPO_ROOT / "output"


def default_music_dir() -> Path:
    if not is_frozen():
        return REPO_ROOT / "music_library"
    return _user_dir("music") / "EditMaker" / "music_library"


def _bundled(name: str) -> str | None:
    exe = name + (".exe" if platform.system() == "Windows" else "")
    for base in (resource_root() / "ffmpeg", app_dir() / "ffmpeg", app_dir()):
        p = base / exe
        if p.is_file():
            return str(p)
    return None


def ffmpeg_binary(name: str) -> str:
    """ffmpeg / ffprobe: env override → bundled next to the app → PATH."""
    env = os.environ.get("EDITMAKER_" + name.upper())
    if env and Path(env).is_file():
        return env
    return _bundled(name) or shutil.which(name) or name
