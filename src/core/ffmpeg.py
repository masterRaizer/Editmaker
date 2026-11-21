"""Thin wrappers around ffmpeg / ffprobe binaries."""
from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


class FFmpegError(RuntimeError):
    pass


def available() -> dict:
    info = {"ffmpeg": None, "ffprobe": None}
    for key, binary in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        try:
            out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=10).stdout
            info[key] = out.splitlines()[0] if out else None
        except (OSError, subprocess.TimeoutExpired):
            info[key] = None
    return info


@functools.lru_cache(maxsize=1)
def nvenc_usable() -> bool:
    """True only if h264_nvenc is compiled in AND a GPU accepts a tiny encode."""
    try:
        enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10).stdout
        if "h264_nvenc" not in enc:
            return False
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.2",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def gpu_name() -> Optional[str]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _rate(s: str | None) -> float:
    if not s or s in ("0/0", "N/A"):
        return 0.0
    if "/" in s:
        a, b = s.split("/", 1)
        return float(a) / float(b) if float(b) else 0.0
    return float(s)


def probe(path: Path) -> dict:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path}: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0.0)
    return {
        "duration": duration,
        "width": int(v.get("width", 0)) if v else 0,
        "height": int(v.get("height", 0)) if v else 0,
        "fps": round(_rate(v.get("avg_frame_rate") or v.get("r_frame_rate")), 3) if v else 0.0,
        "vcodec": v.get("codec_name") if v else None,
        "acodec": a.get("codec_name") if a else None,
        "has_video": v is not None,
        "has_audio": a is not None,
        "bitrate": int(data.get("format", {}).get("bit_rate") or 0),
        "sample_rate": int(a.get("sample_rate", 0)) if a else 0,
    }


_TIME_RE = re.compile(r"out_time_(?:us|ms)=(\d+)")


def run(args: list[str], *, total_seconds: float = 0.0, progress: Optional[Callable[[float], None]] = None,
        cwd: Optional[Path] = None) -> None:
    """Run ffmpeg with -progress parsing. Raises FFmpegError with the stderr tail."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
                            encoding="utf-8", errors="replace")
    stderr_lines: list[str] = []

    import threading

    def _drain():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if len(stderr_lines) > 200:
                del stderr_lines[:100]

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        m = _TIME_RE.search(line)
        if m and progress and total_seconds > 0:
            progress(min(1.0, int(m.group(1)) / 1e6 / total_seconds))
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        raise FFmpegError("ffmpeg failed: " + "".join(stderr_lines[-30:]).strip())
    if progress:
        progress(1.0)


def decode_audio(path: Path, sr: int = 22050, start: float = 0.0, duration: float = 0.0):
    """Decode mono float32 PCM via ffmpeg. Returns numpy array."""
    import numpy as np

    cmd = [FFMPEG, "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0:
        raise FFmpegError(r.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


def decode_gray_frames(path: Path, fps: float = 4.0, width: int = 96):
    """Small grayscale frames for scene/motion analysis. Returns (frames[n,h,w] uint8, fps)."""
    import numpy as np

    info = probe(path)
    if not info["has_video"] or not info["width"]:
        raise FFmpegError(f"no video stream in {path}")
    h = int(round(info["height"] * width / info["width"] / 2) * 2) or 2
    r = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-i", str(path), "-an", "-vf",
         f"fps={fps},scale={width}:{h},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, timeout=1800,
    )
    if r.returncode != 0:
        raise FFmpegError(r.stderr.decode(errors="replace")[-300:])
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = buf.size // (width * h)
    return buf[: n * width * h].reshape(n, h, width), fps


def filter_path(p: str | Path) -> str:
    """Escape a filesystem path for use inside a filtergraph option value."""
    s = str(p).replace("\\", "/")
    s = s.replace("'", r"'\''")
    s = s.replace(":", r"\:")
    return f"'{s}'"
