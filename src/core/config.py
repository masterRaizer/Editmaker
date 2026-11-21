"""Application settings: one JSON file in the data dir, merged over defaults."""
from __future__ import annotations

import copy
import json
import os
import platform
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

VIBES = {
    "dexter_cold_dark": "Dexter / Dark Cold",
    "dark_synth_tension": "Dark Synth",
    "phonk_aggressive": "Phonk",
    "moody_cinematic": "Cinematic",
    "custom": "Свои треки",
}

RESOLUTIONS = {
    "1080x1920": (1080, 1920),
    "1920x1080": (1920, 1080),
    "2160x3840": (2160, 3840),
    "3840x2160": (3840, 2160),
    # used by tests / quick drafts
    "540x960": (540, 960),
}

DEFAULTS: dict = {
    "source_folders": [],
    "output_folder": str(ROOT / "output"),
    "music_library": str(ROOT / "music_library"),
    "render": {
        "resolution": "1080x1920",
        "fps": 60,
        "encoder": "auto",          # auto | libx264 | h264_nvenc
        "quality": "final",         # final (crf16 slow) | draft (crf20 veryfast, for quick looks)
        "duration": 20.0,           # seconds of edit before the outro
        "variants": 4,
        "loudness_lufs": -14.0,
    },
    "vibe": "dexter_cold_dark",
    "watermark": {
        "type": "text",             # text | image
        "text": "@yourchannel",
        "image_path": "",
        "font_path": "",            # empty = auto-detect
        "color": "#FFFFFF",
        "opacity": 0.35,
        "size": 0.045,              # text: font height / video height; image: width / video width
        "position": "bottom_right", # top_left | top_right | bottom_left | bottom_right | bottom_center
        "drift": True,              # slow movement so a crop does not remove it
        "in_video": True,
        "outro": True,
        "outro_duration": 2.0,
    },
    "metadata": {
        "language": "ru",           # ru | en
        "disclaimer": "Все права на видеоряд и музыку принадлежат их правообладателям. Фан-эдит.",
        "channel_tags": [],
        "youtube_privacy": "private",
        "tiktok_privacy": "SELF_ONLY",
        "tiktok_mode": "draft",     # draft (inbox) | direct
    },
    "server": {"host": "127.0.0.1", "port": 8765},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_font() -> str:
    candidates = []
    if platform.system() == "Windows":
        windir = os.environ.get("WINDIR", "C:/Windows")
        candidates += [f"{windir}/Fonts/segoeuib.ttf", f"{windir}/Fonts/arialbd.ttf", f"{windir}/Fonts/arial.ttf"]
    elif platform.system() == "Darwin":
        candidates += ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial.ttf"]
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return ""


class Settings:
    """Thread-safe settings store backed by data_dir/config.json."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "config.json"
        self._lock = threading.Lock()
        self._data = deep_merge(DEFAULTS, self._read())

    def _read(self) -> dict:
        if self.path.is_file():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, patch: dict) -> dict:
        with self._lock:
            self._data = deep_merge(self._data, patch)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            return copy.deepcopy(self._data)

    @property
    def secrets_dir(self) -> Path:
        p = self.data_dir.parent / "secrets" if self.data_dir.name == "data" else self.data_dir / "secrets"
        p.mkdir(parents=True, exist_ok=True)
        return p
