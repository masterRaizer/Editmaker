"""Indexing of source videos on disk + visual analysis (shots, motion energy)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import numpy as np

from . import ffmpeg

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov"}

_NOISE = re.compile(
    r"\[[^\]]*\]|\((?:[^)]*(?:\d{3,4}p|x26[45]|hevc|avc|aac|flac|bluray|web|bd|rip|10bit|8bit)[^)]*)\)",
    re.IGNORECASE,
)
_TOKENS = re.compile(
    r"\b(?:\d{3,4}p|[48]k|x26[45]|h\.?26[45]|hevc|avc|aac\d?(?:\.\d)?|ac3|eac3|ddp?\d?(?:\.\d)?|flac|opus|"
    r"bluray|blu-ray|bdrip|brrip|web-?dl|webrip|web|hdtv|hdr|sdr|10bit|8bit|multi|dual[- ]audio|proper|repack|"
    r"amzn|nf|dsnp|hmax|cr)\b",
    re.IGNORECASE,
)
_EPISODE = re.compile(r"\b[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})\b|\s-\s(\d{1,4})(?:v\d)?\b|\b[Ee][Pp]?\.?\s?(\d{1,4})\b")


def extract_title(path: Path) -> tuple[str, str]:
    """Return (title, scene) from a file name like
    'Dexter.S01E03.Popping.Cherry.1080p.mkv' or '[SubsPlease] Chainsaw Man - 05 (1080p) [ABCD].mkv'."""
    stem = Path(path).stem
    s = _NOISE.sub(" ", stem)
    s = re.sub(r"[._]+", " ", s)
    s = _TOKENS.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -")
    title, scene = s, ""
    m = _EPISODE.search(" " + s + " ")
    if m:
        pos = m.start() - 1 if m.start() > 0 else 0
        title = s[:pos].strip(" -")
        rest = s[pos + len(m.group(0).strip()) + 1:].strip(" -")
        if m.group(1):
            ep = f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
        else:
            ep = f"E{int(m.group(3) or m.group(4)):02d}"
        scene = (ep + (" " + rest if rest else "")).strip()
    if not title:
        title = Path(path).parent.name or s
    return title.strip(), scene.strip()


def iter_videos(folders: Iterable[str | Path]) -> Iterable[Path]:
    for folder in folders:
        root = Path(folder).expanduser()
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith("."):
                yield p


def index_file(path: Path) -> dict:
    info = ffmpeg.probe(path)
    title, scene = extract_title(path)
    st = path.stat()
    return {
        "path": str(path.resolve()),
        "folder": str(path.parent.resolve()),
        "title": title,
        "scene": scene,
        "size": st.st_size,
        "mtime": st.st_mtime,
        **info,
    }


def analyze_visual(path: Path, fps: float = 4.0) -> dict:
    """Shot boundaries, per-sample motion energy and brightness.

    Returns dict with:
      fps, motion[list float 0..1], luma[list float 0..255], shots[list (start, end)], energy(avg motion), cut_rate(shots/min)
    """
    frames, fps = ffmpeg.decode_gray_frames(path, fps=fps)
    n = len(frames)
    if n < 2:
        dur = n / fps
        return {"fps": fps, "motion": [0.0] * n, "luma": [float(f.mean()) for f in frames],
                "shots": [[0.0, dur]], "energy": 0.0, "cut_rate": 0.0}
    f = frames.astype(np.float32)
    luma = f.mean(axis=(1, 2))
    diff = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))  # n-1
    # histogram distance separates real cuts from fast motion
    hists = np.stack([np.histogram(fr, bins=32, range=(0, 255))[0] for fr in frames]).astype(np.float32)
    hists /= hists.sum(axis=1, keepdims=True) + 1e-6
    hdist = 0.5 * np.abs(np.diff(hists, axis=0)).sum(axis=1)  # 0..1
    med = float(np.median(diff)) + 1e-3
    cut_idx = [i + 1 for i in range(len(diff))
               if (hdist[i] > 0.35 and diff[i] > 12) or (diff[i] > max(30.0, 6 * med))]
    bounds = [0] + cut_idx + [n]
    shots = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b > a:
            shots.append([round(a / fps, 3), round(b / fps, 3)])
    # motion: diff excluding the cut transitions, normalized to 0..1
    motion = np.concatenate([[diff[0]], diff])
    for i in cut_idx:
        motion[i] = motion[i - 1] if i > 0 else 0
    motion = np.clip(motion / 40.0, 0, 1)
    # detail: mean absolute Laplacian (edges / texture) — prefers sharp, detailed frames over blur and flat fills
    lap = np.abs(4 * f[:, 1:-1, 1:-1] - f[:, :-2, 1:-1] - f[:, 2:, 1:-1] - f[:, 1:-1, :-2] - f[:, 1:-1, 2:])
    detail = np.clip(lap.mean(axis=(1, 2)) / 25.0, 0, 1)
    duration = n / fps
    return {
        "fps": fps,
        "motion": [round(float(x), 4) for x in motion],
        "detail": [round(float(x), 4) for x in detail],
        "luma": [round(float(x), 2) for x in luma],
        "shots": shots,
        "energy": round(float(motion.mean()), 4),
        "cut_rate": round(len(shots) / max(duration / 60.0, 1e-6), 2),
    }
