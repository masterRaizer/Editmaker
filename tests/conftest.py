import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core import synthetic  # noqa: E402


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    """Synthetic sources + beat tracks shared by all tests (made once)."""
    d = tmp_path_factory.mktemp("media")
    src = d / "sources"
    lib = d / "music"
    clip_a = synthetic.make_clip(src / "Dexter.S01E03.Popping.Cherry.1080p.WEB-DL.mp4", 9.0, size="640x360")
    clip_b = synthetic.make_clip(src / "anime" / "[SubsPlease] Night Hunter - 05 (1080p) [ABCD1234].mkv", 6.0,
                                 size="640x360", audio=False)
    beat = synthetic.make_beat(lib / "phonk_aggressive" / "beat_120.wav", 120.0, 20.0)
    beat_cold = synthetic.make_beat(lib / "dexter_cold_dark" / "cold_90.wav", 90.0, 24.0)
    return {"root": d, "src": src, "lib": lib, "clip_a": clip_a, "clip_b": clip_b, "beat": beat, "beat_cold": beat_cold}
