"""Edit planning: turns sources + beats + a style into a cut list (the 'plan').

Timeline model: boundaries B0=0 < B1 < ... < Bn = main duration, all on music beats.
Segment k is visible on [Bk, Bk+1]. A transition of length d at boundary Bk is centred on
the beat, so segment k-1 is extended by d/2 and segment k starts d/2 earlier.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from . import audio

STYLES: dict[str, dict] = {
    "cold": {
        "name": "Cold / Dexter Pace",
        "grid_bpm": 90,
        "beats_per_cut": [2, 4, 4, 8],
        "transitions": [("cut", 0.0)] * 5 + [("fadeblack", 0.35), ("zoomin", 0.3), ("fade", 0.3)],
        "zoom": "push",           # slow push-in each shot
        "shake": 0.0,
        "flash": 0.0,
        "contrast": 1.08,
        "motion_target": 0.12,    # prefers calm, tense shots
        "music_section": "intro",
    },
    "drop": {
        "name": "High-Impact Beat / Drop",
        "grid_bpm": 140,
        "beats_per_cut": [1, 2, 2],
        "transitions": [("cut", 0.0)] * 3 + [("slideleft", 0.12), ("slideright", 0.12), ("slideup", 0.12),
                                              ("zoomin", 0.15), ("fadewhite", 0.1)],
        "zoom": "punch",          # punch-in on each cut + pulse on bass
        "shake": 12.0,            # px at 1080 wide
        "flash": 0.10,
        "contrast": 1.15,
        "motion_target": 0.6,
        "music_section": "peak",
    },
    "flow": {
        "name": "Smooth / Flow",
        "grid_bpm": 100,
        "beats_per_cut": [4, 4, 8],
        "transitions": [("fade", 0.5), ("smoothleft", 0.45), ("smoothup", 0.45), ("circleopen", 0.5),
                        ("hblur", 0.4), ("dissolve", 0.5)],
        "zoom": "drift",          # alternating slow in/out
        "shake": 0.0,
        "flash": 0.0,
        "contrast": 1.05,
        "motion_target": 0.25,
        "music_section": "middle",
    },
    "hook": {
        "name": "Fast Hook / Retention Cut",
        "grid_bpm": 128,
        "beats_per_cut": [1, 2],
        "transitions": [("cut", 0.0)] * 4 + [("zoomin", 0.12), ("slideleft", 0.1), ("fadewhite", 0.08)],
        "zoom": "punch",
        "shake": 7.0,
        "flash": 0.07,
        "contrast": 1.12,
        "motion_target": 0.5,
        "music_section": "peak",
        "hook_seconds": 1.2,
    },
}
STYLE_ORDER = ["cold", "drop", "flow", "hook"]


@dataclass
class SourceInfo:
    id: int
    path: str
    duration: float
    has_audio: bool
    title: str
    analysis: dict | None


def _music_start(beats: list[float], energy: list[float], length: float, section: str, track_dur: float,
                 rng: random.Random) -> float:
    """Choose the beat where the music excerpt starts."""
    candidates = [b for b in beats if b + length <= track_dur + 0.01] or beats[:1] or [0.0]
    if section == "intro" or not energy:
        # skip a silent lead-in: first beat after 1 s, or the first beat
        late = [b for b in candidates if b >= 1.0]
        return late[0] if late else candidates[0]
    win = max(1, int(round(length)))
    sums = []
    for b in candidates:
        s = int(b)
        seg = energy[s:s + win]
        sums.append(sum(seg) / max(1, len(seg)))
    if section == "peak":
        # start a few beats before the loudest window so the drop lands early
        best = max(range(len(candidates)), key=lambda i: sums[i])
        return candidates[max(0, best - 2)]
    # middle: median energy, randomised among close ones
    order = sorted(range(len(candidates)), key=lambda i: sums[i])
    mid = order[len(order) // 2: len(order) // 2 + 4] or order
    return candidates[rng.choice(mid)]


def _boundaries(beats: list[float], target: float, style: dict, rng: random.Random) -> list[float]:
    """Pick cut points on beats (beats already relative to excerpt start, beats[0] == 0)."""
    if len(beats) < 2:
        return [0.0, target]
    period = (beats[-1] - beats[0]) / max(1, len(beats) - 1)
    out = [0.0]
    i = 0
    hook = style.get("hook_seconds", 0.0)
    # half-beat grid for the hook part
    if hook:
        t = 0.0
        step = period / 2 if period > 0.6 else period
        while t + step < hook + 0.05:
            t += step
            out.append(round(t, 3))
        i = min(range(len(beats)), key=lambda j: abs(beats[j] - out[-1]))
        if abs(beats[i] - out[-1]) > 0.02:
            i += 1
            out.append(beats[min(i, len(beats) - 1)])
    while True:
        step = rng.choice(style["beats_per_cut"])
        j = i + step
        if j >= len(beats) or beats[j] > target + period * 0.5:
            break
        out.append(beats[j])
        i = j
    if out[-1] < target * 0.8 and i + 1 < len(beats):
        # extend with the remaining beats up to target
        j = min(len(beats) - 1, max(range(len(beats)), key=lambda k: -abs(beats[k] - target)))
        if beats[j] > out[-1]:
            out.append(beats[j])
    out = sorted(set(round(x, 3) for x in out))
    return out


def _score_window(an: dict | None, start: float, dur: float, target_motion: float) -> float:
    if not an or not an.get("motion"):
        return 0.5
    fps = an["fps"]
    a, b = int(start * fps), max(int(start * fps) + 1, int((start + dur) * fps))
    m = an["motion"][a:b]
    lum = an["luma"][a:b]
    if not m:
        return 0.0
    mean_m = sum(m) / len(m)
    mean_l = sum(lum) / len(lum) if lum else 128
    score = 1.0 - min(1.0, abs(mean_m - target_motion) / max(target_motion, 0.3))
    if mean_l < 14:          # near-black frames
        score -= 0.8
    if mean_l > 245:         # blown out / white flash
        score -= 0.4
    # crossing a shot boundary is allowed but discouraged
    for s0, s1 in an.get("shots", []):
        if s0 < start + dur and s1 > start and (start < s0 or start + dur > s1):
            if s0 > start + 0.05 and s0 < start + dur - 0.05:
                score -= 0.35
            break
    return score


class _Picker:
    def __init__(self, sources: list[SourceInfo], style: dict, rng: random.Random):
        self.sources = [s for s in sources if s.duration > 0.3]
        self.style = style
        self.rng = rng
        self.used: dict[int, list[tuple[float, float]]] = {s.id: [] for s in self.sources}
        self.last_src: int | None = None

    def pick(self, dur: float, prefer_max: bool = False) -> tuple[SourceInfo, float]:
        cands = []
        for s in self.sources:
            if s.duration <= dur:
                cands.append((0.1 if len(self.sources) > 1 else 0.5, s, 0.0))
                continue
            step = max(0.25, dur / 3)
            n = int((s.duration - dur) / step) + 1
            stride = max(1, n // 120)  # cap the work per source
            for k in range(0, n, stride):
                st = k * step
                sc = _score_window(s.analysis, st, dur, 1.0 if prefer_max else self.style["motion_target"])
                for (u0, u1) in self.used[s.id]:
                    if st < u1 and st + dur > u0:
                        sc -= 1.5
                        break
                if s.id == self.last_src and len(self.sources) > 1:
                    sc -= 0.15
                cands.append((sc, s, st))
        if not cands:
            raise ValueError("no usable source footage")
        cands.sort(key=lambda c: c[0], reverse=True)
        top = cands[: 1 if prefer_max else min(6, len(cands))]
        sc, s, st = self.rng.choice(top)
        self.used[s.id].append((st, st + dur))
        self.last_src = s.id
        return s, st


def build_plan(sources: list[SourceInfo], style_key: str, seed: int, target_duration: float,
               track: dict | None, outro: bool, outro_duration: float) -> dict:
    style = STYLES[style_key]
    rng = random.Random(seed * 7919 + STYLE_ORDER.index(style_key) if style_key in STYLE_ORDER else seed)
    music = None
    if track and track.get("analysis", {}).get("beats"):
        an = track["analysis"]
        beats_abs = an["beats"]
        start = _music_start(beats_abs, an.get("energy", []), target_duration, style["music_section"],
                             an.get("duration", 0), rng)
        beats = [round(b - start, 3) for b in beats_abs if b >= start - 1e-6]
        strong = [round(b - start, 3) for b in an.get("strong", []) if b >= start]
        bass = [round(b - start, 3) for b in an.get("bass", []) if b >= start]
        bpm = an.get("bpm", 0)
        music = {"path": track["path"], "title": track.get("title"), "bpm": bpm, "start": round(start, 3),
                 "track_duration": an.get("duration")}
        room = (an.get("duration") or 0) - start
        if room > 0:
            beats = [b for b in beats if b <= room]
    else:
        bpm = style["grid_bpm"]
        beats = audio.grid_beats(bpm, target_duration + 4)
        strong = beats[::4]
        bass = beats[::2]
    boundaries = _boundaries(beats, target_duration, style, rng)
    main_dur = boundaries[-1]

    # transitions per boundary (index 1..n-1); hook part uses hard cuts
    trans = [("cut", 0.0)]
    for k in range(1, len(boundaries) - 1):
        seg_before = boundaries[k] - boundaries[k - 1]
        seg_after = boundaries[k + 1] - boundaries[k]
        t, d = rng.choice(style["transitions"])
        if style.get("hook_seconds") and boundaries[k] < style["hook_seconds"]:
            t, d = "cut", 0.0
        d = min(d, seg_before * 0.6, seg_after * 0.6)
        if d < 0.06:
            t, d = "cut", 0.0
        trans.append((t, round(d, 3)))
    outro_fade = 0.5 if outro else 0.0

    picker = _Picker(sources, style, rng)
    segments = []
    n = len(boundaries) - 1
    for k in range(n):
        b0, b1 = boundaries[k], boundaries[k + 1]
        d_in = trans[k][1]
        d_out = trans[k + 1][1] if k + 1 < n else outro_fade
        clip_dur = round((b1 - b0) + d_in / 2 + d_out / 2, 3)
        prefer_max = bool(style.get("hook_seconds")) and k == 0
        src, st = picker.pick(clip_dur, prefer_max=prefer_max)
        out_start = b0 - d_in / 2
        local = lambda ts: [round(t - out_start, 3) for t in ts if b0 - 0.01 <= t < b1 - 0.05]  # noqa: E731
        flash = local(strong) if style["flash"] else []
        # avoid strobing: at least 0.45 s between flashes
        thinned = []
        for t in flash:
            if not thinned or t - thinned[-1] >= 0.45:
                thinned.append(t)
        zoom_mode = style["zoom"]
        if zoom_mode == "drift":
            zoom_mode = "drift_in" if k % 2 == 0 else "drift_out"
        segments.append({
            "k": k,
            "source_id": src.id,
            "path": src.path,
            "title": src.title,
            "has_audio": src.has_audio,
            "src_start": round(st, 3),
            "clip_dur": clip_dur,
            "out_start": round(b0, 3),
            "out_end": round(b1, 3),
            "transition_in": {"type": trans[k][0], "dur": d_in},
            "zoom": zoom_mode,
            "shake": local(bass) if style["shake"] else [],
            "flash": thinned,
        })
    beats_in = [b for b in beats if b <= main_dur + 1e-3]
    return {
        "style": style_key,
        "style_name": style["name"],
        "seed": seed,
        "bpm": round(float(bpm), 2),
        "music": music,
        "main_duration": round(main_dur, 3),
        "outro": outro,
        "outro_duration": outro_duration if outro else 0.0,
        "outro_fade": outro_fade,
        "total_duration": round(main_dur + (outro_duration if outro else 0.0), 3),
        "boundaries": boundaries,
        "beats": beats_in,
        "strong": [b for b in strong if b <= main_dur],
        "effects": {"shake_px": style["shake"], "flash": style["flash"], "contrast": style["contrast"]},
        "segments": segments,
        "cuts": max(0, len(segments) - 1),
        "avg_shot": round(main_dur / max(1, len(segments)), 3),
        "hook_first_cut": boundaries[1] if len(boundaries) > 1 else None,
    }


def describe(plan: dict) -> str:
    m = plan.get("music")
    src = f"{m['title']} @ {m['start']:.2f}s" if m else "звук исходника"
    return (f"{plan['style_name']}: {plan['cuts']} склеек, средний план {plan['avg_shot']:.2f}s, "
            f"BPM {plan['bpm']:.0f}, музыка: {src}")

