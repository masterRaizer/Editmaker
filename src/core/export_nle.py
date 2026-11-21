"""Hand the cut over to a manual editor.

* FCPXML 1.10 — DaVinci Resolve (File → Import → Timeline) and Final Cut Pro.
* EDL CMX3600 — Premiere Pro, Resolve, Vegas, Avid.
* CapCut pack — CapCut has no timeline import, so the pack holds the shots already cut,
  scaled and graded in order, the music excerpt, the watermark as PNG and the beat times.

Cuts are exported as hard cuts on the beat; transitions/effects are left to the editor.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import quoteattr

from . import ffmpeg, render, watermark
from .config import RESOLUTIONS


def _visible(seg: dict) -> tuple[float, float, float]:
    """(timeline start, timeline duration, source in) of the part of a segment that is on screen."""
    d_in = seg["transition_in"]["dur"]
    return seg["out_start"], seg["out_end"] - seg["out_start"], seg["src_start"] + d_in / 2


def _rt(seconds: float, fps: int) -> str:
    return f"{int(round(seconds * fps))}/{fps}s"


def _tc(seconds: float, fps: int) -> str:
    f = int(round(seconds * fps))
    return f"{f // (3600 * fps):02d}:{f // (60 * fps) % 60:02d}:{f // fps % 60:02d}:{f % fps:02d}"


def _uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def fcpxml(plan: dict, *, resolution: str, fps: int, name: str, probes: dict[str, dict]) -> str:
    W, H = RESOLUTIONS[resolution]
    total = plan["main_duration"]
    paths = list(dict.fromkeys(s["path"] for s in plan["segments"]))
    res, assets = [f'<format id="seq" frameDuration="1/{fps}s" width="{W}" height="{H}"/>'], {}
    for i, p in enumerate(paths, 1):
        pr = probes.get(p, {})
        sfps = max(1, round(pr.get("fps") or fps))
        res.append(f'<format id="f{i}" frameDuration="1/{sfps}s" width="{pr.get("width", W)}" height="{pr.get("height", H)}"/>')
        dur = pr.get("duration") or 3600
        a = "1" if pr.get("has_audio", True) else "0"
        res.append(f'<asset id="r{i}" name={quoteattr(Path(p).stem)} start="0s" duration="{_rt(dur, fps)}" hasVideo="1" '
                   f'hasAudio="{a}" format="f{i}"><media-rep kind="original-media" src={quoteattr(_uri(p))}/></asset>')
        assets[p] = f"r{i}"
    music = plan.get("music")
    if music:
        res.append(f'<asset id="rm" name={quoteattr(music.get("title") or "music")} start="0s" '
                   f'duration="{_rt(music.get("track_duration") or total + 60, fps)}" hasVideo="0" hasAudio="1" '
                   f'audioSources="1" audioChannels="2" audioRate="48000">'
                   f'<media-rep kind="original-media" src={quoteattr(_uri(music["path"]))}/></asset>')
    clips = []
    for k, seg in enumerate(plan["segments"]):
        start, dur, src_in = _visible(seg)
        inner = ""
        if k == 0 and music:
            marks = "".join(f'<marker start="{_rt(music["start"] + b, fps)}" duration="1/{fps}s" value="beat"/>'
                            for b in plan.get("beats", []))
            inner = (f'<asset-clip ref="rm" lane="-1" offset="{_rt(src_in, fps)}" name="music" '
                     f'start="{_rt(music["start"], fps)}" duration="{_rt(total, fps)}">{marks}</asset-clip>')
        clips.append(f'<asset-clip ref="{assets[seg["path"]]}" offset="{_rt(start, fps)}" name={quoteattr(seg["title"] or "clip")} '
                     f'start="{_rt(src_in, fps)}" duration="{_rt(dur, fps)}"'
                     + (' audioRole="dialogue" ' if not music else " ")
                     + (f'>{inner}</asset-clip>' if inner else "/>"))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.10">\n'
            f'<resources>\n{chr(10).join(res)}\n</resources>\n<library><event name={quoteattr(name)}>'
            f'<project name={quoteattr(name)}><sequence format="seq" duration="{_rt(total, fps)}" tcStart="0s" '
            f'tcFormat="NDF" audioLayout="stereo" audioRate="48k"><spine>\n{chr(10).join(clips)}\n</spine></sequence>'
            '</project></event></library>\n</fcpxml>\n')


def edl(plan: dict, *, fps: int, name: str) -> str:
    lines = [f"TITLE: {name}", "FCM: NON-DROP FRAME", ""]
    n = 0
    for seg in plan["segments"]:
        start, dur, src_in = _visible(seg)
        n += 1
        reel = "AX"
        lines.append(f"{n:03d}  {reel:<8} V     C        {_tc(src_in, fps)} {_tc(src_in + dur, fps)} "
                     f"{_tc(start, fps)} {_tc(start + dur, fps)}")
        lines.append(f"* FROM CLIP NAME: {Path(seg['path']).name}")
        lines.append("")
    m = plan.get("music")
    if m:
        n += 1
        lines.append(f"{n:03d}  AX       A     C        {_tc(m['start'], fps)} {_tc(m['start'] + plan['main_duration'], fps)} "
                     f"{_tc(0, fps)} {_tc(plan['main_duration'], fps)}")
        lines.append(f"* FROM CLIP NAME: {Path(m['path']).name}")
        lines.append("")
    return "\n".join(lines)


def beat_sheet(plan: dict) -> str:
    out = ["Время в секундах от начала ролика.", "", f"BPM: {plan['bpm']}"]
    if plan.get("music"):
        out.append(f"Музыка: {Path(plan['music']['path']).name}, начало фрагмента в треке: {plan['music']['start']:.3f} с")
    out += ["", "Склейки:"] + [f"  {b:.3f}" for b in plan["boundaries"][1:-1]]
    out += ["", "Сильные доли (вспышки, удары):"] + [f"  {b:.3f}" for b in plan.get("strong", [])]
    out += ["", "Все доли:"] + [f"  {b:.3f}" for b in plan.get("beats", [])]
    return "\n".join(out) + "\n"


def capcut_pack(plan: dict, out_dir: Path, *, resolution: str, fps: int, vibe: str, wm: dict,
                progress: Optional[Callable[[float], None]] = None) -> Path:
    W, H = RESOLUTIONS[resolution]
    out_dir.mkdir(parents=True, exist_ok=True)
    segs = plan["segments"]
    steps = len(segs) + 2
    grade = render.GRADES.get(vibe, render.GRADES["custom"]).format(c=f"{plan['effects']['contrast']:.3f}")
    for k, seg in enumerate(segs):
        start, dur, src_in = _visible(seg)
        name = f"{k + 1:02d}_{start:06.3f}s.mp4"
        vf = (f"fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1,"
              f"{grade},format=yuv420p")
        ffmpeg.run(["-ss", f"{src_in:.3f}", "-i", seg["path"], "-t", f"{dur:.3f}", "-vf", vf, "-an",
                    "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", str(out_dir / name)])
        if progress:
            progress((k + 1) / steps)
    m = plan.get("music")
    if m:
        ffmpeg.run(["-ss", f"{m['start']:.3f}", "-i", m["path"], "-t", f"{plan['total_duration']:.3f}",
                    "-ar", "48000", "-ac", "2", str(out_dir / "music.wav")])
    wm = watermark.normalize(wm)
    if wm["usable"]:
        if wm["type"] == "image":
            shutil.copyfile(wm["image_path"], out_dir / "watermark.png")
        else:
            work = out_dir / "_wm"
            work.mkdir(exist_ok=True)
            files = watermark.prepare_files(wm, work)
            fs = max(16, round(min(W, H) * 0.085))
            ffmpeg.run(["-f", "lavfi", "-i", f"color=c=black@0.0:s={W}x{max(64, fs * 2)},format=rgba",
                        "-vf", f"drawtext=fontfile={files['font']}:textfile={files['text']}:fontsize={fs}:"
                               f"fontcolor=0x{wm['color'].lstrip('#')}:x=(w-tw)/2:y=(h-th)/2",
                        "-frames:v", "1", str(out_dir / "watermark.png")], cwd=work)
            shutil.rmtree(work, ignore_errors=True)
    if progress:
        progress((steps - 1) / steps)
    (out_dir / "beats.txt").write_text(beat_sheet(plan), encoding="utf-8")
    (out_dir / "КАК_СОБРАТЬ_В_CAPCUT.txt").write_text(
        "1. CapCut → Новый проект → Импорт → выделите все файлы 01_…mp4 по порядку и music.wav.\n"
        "2. Перетащите клипы на дорожку по порядку имён: они уже обрезаны по долям и встанут встык.\n"
        "3. music.wav положите на аудиодорожку с нуля. Склейки совпадут с битом.\n"
        "4. Переходы и эффекты ставьте на склейки; время сильных долей — в beats.txt.\n"
        "5. watermark.png добавьте наложением (Overlay) на всю длину и в конец ролика.\n",
        encoding="utf-8")
    if progress:
        progress(1.0)
    return out_dir


def export(plan: dict, out_dir: Path, fmt: str, *, resolution: str, fps: int, vibe: str, wm: dict, name: str,
           progress: Optional[Callable[[float], None]] = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "fcpxml":
        probes = {}
        for p in dict.fromkeys(s["path"] for s in plan["segments"]):
            try:
                probes[p] = ffmpeg.probe(Path(p))
            except ffmpeg.FFmpegError:
                probes[p] = {}
        path = out_dir / f"{name}.fcpxml"
        path.write_text(fcpxml(plan, resolution=resolution, fps=fps, name=name, probes=probes), encoding="utf-8")
        (out_dir / "beats.txt").write_text(beat_sheet(plan), encoding="utf-8")
        return {"format": fmt, "path": str(path), "folder": str(out_dir)}
    if fmt == "edl":
        path = out_dir / f"{name}.edl"
        path.write_text(edl(plan, fps=fps, name=name), encoding="utf-8")
        (out_dir / "beats.txt").write_text(beat_sheet(plan), encoding="utf-8")
        return {"format": fmt, "path": str(path), "folder": str(out_dir)}
    if fmt == "capcut":
        folder = capcut_pack(plan, out_dir / "capcut", resolution=resolution, fps=fps, vibe=vibe, wm=wm, progress=progress)
        return {"format": fmt, "path": str(folder), "folder": str(folder)}
    raise ValueError(f"unknown export format {fmt}")

