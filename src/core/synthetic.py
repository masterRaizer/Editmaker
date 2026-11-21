"""Synthetic test media: source clips with scene changes and a beat track with a known tempo."""
from __future__ import annotations

from pathlib import Path

from . import ffmpeg


def make_clip(path: Path, duration: float = 18.0, size: str = "1280x720", fps: int = 30, audio: bool = True) -> Path:
    """Three visually distinct 'shots' (testsrc2 → mandelbrot → smptebars) + optional tone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = duration / 3
    args = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={part:.3f}",
            "-f", "lavfi", "-i", f"mandelbrot=s={size}:r={fps}",
            "-f", "lavfi", "-i", f"smptebars=s={size}:r={fps}:d={part:.3f}"]
    graph = f"[1:v]trim=duration={part:.3f},setpts=PTS-STARTPTS[m];[0:v][m][2:v]concat=n=3:v=1:a=0[v]"
    maps = ["-map", "[v]"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=330:d={duration:.3f}"]
        maps += ["-map", "3:a", "-c:a", "aac"]
    ffmpeg.run([*args, "-filter_complex", graph, *maps, "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-t", f"{duration:.3f}", str(path)])
    return path


def make_beat(path: Path, bpm: float = 120.0, duration: float = 30.0) -> Path:
    """Kick drum (pitch-swept sine with decay) on every beat + hat on off-beats."""
    path.parent.mkdir(parents=True, exist_ok=True)
    p = 60.0 / bpm
    expr = (f"0.9*sin(2*PI*(55+80*exp(-mod(t,{p:.6f})*30))*t)*exp(-mod(t,{p:.6f})*12)"
            f"+0.15*sin(2*PI*3000*t)*exp(-mod(t+{p / 2:.6f},{p:.6f})*60)")
    ffmpeg.run(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d={duration:.3f}", "-ac", "2", str(path)])
    return path


def make_fixture_set(src_dir: Path, music_dir: Path) -> dict:
    a = make_clip(src_dir / "Dexter.S01E03.Popping.Cherry.1080p.WEB-DL.mp4", 18.0)
    b = make_clip(src_dir / "anime" / "[SubsPlease] Night Hunter - 05 (1080p) [ABCD1234].mkv", 12.0, audio=False)
    beat = make_beat(music_dir / "phonk_aggressive" / "test_beat_120.wav", 120.0, 30.0)
    beat2 = make_beat(music_dir / "dexter_cold_dark" / "test_cold_90.wav", 90.0, 40.0)
    return {"clips": [str(a), str(b)], "tracks": [str(beat), str(beat2)]}
