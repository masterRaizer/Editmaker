"""Music library organised by vibe folders + automatic track choice."""
from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from . import audio
from .config import VIBES
from .db import DB

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}

# target tempo / energy per vibe, used by auto-pick
VIBE_PROFILE = {
    "dexter_cold_dark": {"bpm": 90, "energy": 0.25},
    "moody_cinematic": {"bpm": 85, "energy": 0.2},
    "dark_synth_tension": {"bpm": 110, "energy": 0.45},
    "phonk_aggressive": {"bpm": 135, "energy": 0.8},
    "custom": {"bpm": 115, "energy": 0.5},
}


def ensure_library(root: Path) -> None:
    for v in VIBES:
        (Path(root) / v).mkdir(parents=True, exist_ok=True)


def scan_library(db: DB, root: Path, progress=None) -> dict:
    root = Path(root)
    ensure_library(root)
    files = [(v, p) for v in VIBES for p in sorted((root / v).rglob("*")) if p.suffix.lower() in AUDIO_EXT]
    analyzed, skipped, errors = 0, 0, []
    for i, (vibe, p) in enumerate(files):
        st = p.stat()
        existing = db.track_by_path(str(p.resolve()))
        if existing and existing["size"] == st.st_size and abs(existing["mtime"] - st.st_mtime) < 1e-3:
            skipped += 1
        else:
            try:
                a = audio.analyze_track(p)
                db.upsert_track(str(p.resolve()), vibe, p.stem, st.st_size, st.st_mtime, a)
                analyzed += 1
            except Exception as e:  # noqa: BLE001 - one broken file must not stop the scan
                errors.append(f"{p.name}: {e}")
        if progress:
            progress((i + 1) / max(1, len(files)))
    return {"total": len(files), "analyzed": analyzed, "cached": skipped, "errors": errors}


def suggest_vibe(sources_analysis: list[dict]) -> str:
    """Map visual dynamics of the sources to a vibe."""
    if not sources_analysis:
        return "dexter_cold_dark"
    energy = sum(a.get("energy", 0) for a in sources_analysis) / len(sources_analysis)
    cut_rate = sum(a.get("cut_rate", 0) for a in sources_analysis) / len(sources_analysis)
    if energy > 0.35 or cut_rate > 30:
        return "phonk_aggressive"
    if energy > 0.18 or cut_rate > 15:
        return "dark_synth_tension"
    if cut_rate < 6:
        return "moody_cinematic"
    return "dexter_cold_dark"


def target_bpm(vibe: str, sources_analysis: list[dict]) -> float:
    base = VIBE_PROFILE.get(vibe, VIBE_PROFILE["custom"])["bpm"]
    if sources_analysis:
        energy = sum(a.get("energy", 0) for a in sources_analysis) / len(sources_analysis)
        base += (energy - 0.3) * 40  # more motion → a bit faster
    return max(70.0, min(170.0, base))


def pick_track(db: DB, vibe: str, sources_analysis: list[dict], min_duration: float, seed: int = 0) -> Optional[dict]:
    """Choose the track whose BPM fits the footage. Falls back to other vibes, then None."""
    want = target_bpm(vibe, sources_analysis)
    rng = random.Random(seed)
    pools = [db.tracks(vibe)] + [db.tracks(v) for v in VIBES if v != vibe]
    for pool in pools:
        usable = [t for t in pool if (t["duration"] or 0) >= min_duration * 0.6 and (t["bpm"] or 0) > 0]
        if not usable:
            continue

        def cost(t):
            b = t["bpm"]
            # half/double time counts as a match
            d = min(abs(b - want), abs(b * 2 - want), abs(b / 2 - want))
            return d + (0 if t["duration"] >= min_duration else 15) - 10 * (t.get("confidence") or 0)

        usable.sort(key=cost)
        top = usable[: min(3, len(usable))]
        choice = rng.choice(top)
        return db.track_by_path(choice["path"])
    return None
