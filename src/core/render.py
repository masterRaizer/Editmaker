"""Plan → ffmpeg filtergraph → mp4."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from . import ffmpeg, watermark
from .config import RESOLUTIONS

GRADES = {
    "dexter_cold_dark": "eq=contrast={c}:brightness=-0.02:saturation=0.74:gamma=0.97,"
                        "colorbalance=rs=-0.06:bs=0.08:rm=-0.02:bm=0.03:rh=-0.02:bh=0.02,vignette=angle=PI/5",
    "dark_synth_tension": "eq=contrast={c}:saturation=0.95:gamma=0.96,"
                          "colorbalance=rs=0.05:bs=0.09:rm=0.02:bm=0.04:rh=0.04:bh=-0.02,vignette=angle=PI/5",
    "phonk_aggressive": "eq=contrast={c}:saturation=1.06:gamma=0.94,colorbalance=rs=0.03:rm=0.04:bh=-0.03,"
                        "curves=all='0/0 0.12/0.05 0.5/0.5 0.9/0.95 1/1'",
    "moody_cinematic": "eq=contrast={c}:saturation=0.9:gamma=0.98,"
                       "colorbalance=rs=-0.04:bs=0.05:rh=0.05:bh=-0.04,vignette=angle=PI/4.5",
    "custom": "eq=contrast={c}:saturation=1.0",
}


def encoder_args(encoder: str, quality: str) -> list[str]:
    if encoder == "auto":
        encoder = "h264_nvenc" if ffmpeg.nvenc_usable() else "libx264"
    if encoder == "h264_nvenc":
        cq = "18" if quality == "final" else "23"
        preset = "p6" if quality == "final" else "p3"
        return ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", cq, "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if quality == "final":
        return ["-c:v", "libx264", "-crf", "16", "-preset", "slow", "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


def resolved_encoder(encoder: str) -> str:
    if encoder == "auto":
        return "h264_nvenc" if ffmpeg.nvenc_usable() else "libx264"
    return encoder


def _impulses(times: list[float], amp: float, decay: float, osc: Optional[str] = None, var: str = "T") -> str:
    """Sum of decaying impulses starting at given local times; '0' if none."""
    parts = []
    for b in times[:24]:
        body = f"{amp:.4f}*exp(-({var}-{b:.3f})*{decay})*gte({var},{b:.3f})"
        if osc:
            body += f"*{osc}"
        parts.append(body)
    return "+".join(parts) if parts else "0"


def segment_video_filter(seg: dict, idx: int, W: int, H: int, fps: int, vibe: str, contrast: float,
                         shake_px: float, flash_amp: float, quality: str) -> str:
    D = seg["clip_dur"]
    pre = 1.5 if quality == "final" else 1.0
    PW, PH = int(round(W * pre / 2) * 2), int(round(H * pre / 2) * 2)
    T = f"(on/{fps})"
    zoom = seg["zoom"]
    p = f"min(1,{T}/{D:.3f})"
    ease = f"({p}*{p}*(3-2*{p}))"
    if zoom == "push":
        z = f"1.02+0.07*{ease}"
    elif zoom == "drift_in":
        z = f"1.0+0.1*{ease}"
    elif zoom == "drift_out":
        z = f"1.1-0.1*{ease}"
    elif zoom == "punch":
        pulses = _impulses(seg.get("shake", []), 0.035, 9.0, var=T)
        z = f"1.07+0.09*exp(-{T}*9)+{pulses}"
    else:
        z = "1.0"
    sx = sy = "0"
    if shake_px and seg.get("shake"):
        amp = shake_px * pre * W / 1080.0
        sx = _impulses(seg["shake"], amp, 14.0, osc=f"sin({T}*73)", var=T)
        sy = _impulses(seg["shake"], amp * 0.6, 14.0, osc=f"cos({T}*61)", var=T)
    x = f"max(0,min(iw-iw/zoom,(iw-iw/zoom)/2+{sx}))"
    y = f"max(0,min(ih-ih/zoom,(ih-ih/zoom)/2+{sy}))"
    chain = [
        f"[{idx}:v]fps={fps}",
        f"scale={PW}:{PH}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={PW}:{PH}",
        "setsar=1",
        f"tpad=stop_mode=clone:stop_duration={D:.3f}",
        f"trim=duration={D:.3f}",
        "setpts=PTS-STARTPTS",
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={fps}",
        GRADES.get(vibe, GRADES["custom"]).format(c=f"{contrast:.3f}"),
    ]
    if flash_amp and seg.get("flash"):
        br = _impulses(seg["flash"], flash_amp, 10.0, var="t")
        chain.append(f"eq=brightness='{br}':eval=frame")
    chain += ["format=yuv420p", f"settb=1/{fps}", "setpts=N/(" + str(fps) + "*TB)"]
    return ",".join(chain) + f"[v{idx}]"


def segment_audio_filter(seg: dict, idx: int) -> str:
    D = seg["clip_dur"]
    if seg.get("has_audio"):
        src = f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={D:.3f}"
    else:
        src = f"anullsrc=r=48000:cl=stereo,atrim=duration={D:.3f},aformat=sample_fmts=fltp:channel_layouts=stereo"
    return f"{src},asetpts=PTS-STARTPTS[a{idx}]"


def build_graph(plan: dict, *, W: int, H: int, fps: int, vibe: str, wm: dict, wm_files: dict, quality: str,
                loudness: float, music_input: Optional[int], image_input: Optional[int]) -> str:
    segs = plan["segments"]
    fx = plan["effects"]
    parts = []
    for i, seg in enumerate(segs):
        parts.append(segment_video_filter(seg, i, W, H, fps, vibe, fx["contrast"], fx["shake_px"], fx["flash"], quality))
        if music_input is None:
            parts.append(segment_audio_filter(seg, i))

    # video chain with transitions centred on the beat
    cur, L = "v0", segs[0]["clip_dur"]
    acur = "a0"
    for k in range(1, len(segs)):
        t = segs[k]["transition_in"]
        nxt, out = f"v{k}", f"x{k}"
        if t["type"] == "cut" or t["dur"] <= 0:
            parts.append(f"[{cur}][{nxt}]concat=n=2:v=1:a=0,settb=1/{fps}[{out}]")
            L += segs[k]["clip_dur"]
            if music_input is None:
                parts.append(f"[{acur}][a{k}]concat=n=2:v=0:a=1[ax{k}]")
        else:
            off = L - t["dur"]
            parts.append(f"[{cur}][{nxt}]xfade=transition={t['type']}:duration={t['dur']:.3f}:offset={off:.3f}[{out}]")
            L = off + segs[k]["clip_dur"]
            if music_input is None:
                parts.append(f"[{acur}][a{k}]acrossfade=d={t['dur']:.3f}:c1=tri:c2=tri[ax{k}]")
        cur = out
        acur = f"ax{k}"

    # in-video watermark
    img_main = img_outro = None
    if image_input is not None:
        if wm["in_video"] and plan["outro"]:
            parts.append(f"[{image_input}:v]split=2[wmsrc1][wmsrc2]")
            img_main, img_outro = "wmsrc1", "wmsrc2"
        elif wm["in_video"]:
            img_main = f"{image_input}:v"
        else:
            img_outro = f"{image_input}:v"
    if wm["usable"] and wm["in_video"]:
        parts.append(watermark.in_video_filter(wm, wm_files, W, H, cur, "vmain", img_main))
        cur = "vmain"

    total = plan["total_duration"]
    if plan["outro"]:
        fade = plan["outro_fade"]
        odur = plan["outro_duration"] + fade / 2
        if wm["usable"]:
            parts.append(watermark.outro_filter(wm, wm_files, W, H, fps, odur, "vout_o", img_outro))
        else:
            parts.append(f"color=c=0x0B0B0C:s={W}x{H}:r={fps}:d={odur:.3f},format=yuv420p,settb=1/{fps}[vout_o]")
        off = L - fade
        parts.append(f"[{cur}][vout_o]xfade=transition=fadeblack:duration={fade:.3f}:offset={off:.3f},"
                     f"trim=duration={total:.3f}[vout]")
    else:
        parts.append(f"[{cur}]fade=t=out:st={max(0, total - 0.3):.3f}:d=0.3,trim=duration={total:.3f}[vout]")

    # audio
    fade_len = max(0.4, plan["outro_duration"] * 0.9) if plan["outro"] else 0.4
    tail = (f"apad,atrim=duration={total:.3f},afade=t=in:d=0.03,"
            f"afade=t=out:st={max(0.0, total - fade_len):.3f}:d={fade_len:.3f},"
            f"loudnorm=I={loudness}:TP=-1.5:LRA=11,aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")
    if music_input is not None:
        parts.append(f"[{music_input}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,{tail}")
    else:
        parts.append(f"[{acur}]{tail}")
    return ";\n".join(parts)


def render(plan: dict, out_path: Path, *, resolution: str, fps: int, vibe: str, wm: dict, encoder: str,
           quality: str, loudness: float = -14.0, progress: Optional[Callable[[float], None]] = None) -> dict:
    W, H = RESOLUTIONS[resolution]
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wm = watermark.normalize(wm)
    plan = dict(plan)
    workdir = Path(tempfile.mkdtemp(prefix="editmaker_"))
    try:
        wm_files = watermark.prepare_files(wm, workdir) if wm["usable"] else {}
        args: list[str] = []
        for seg in plan["segments"]:
            args += ["-ss", f"{seg['src_start']:.3f}", "-t", f"{seg['clip_dur'] + 0.5:.3f}", "-i", seg["path"]]
        n = len(plan["segments"])
        music_input = image_input = None
        if plan.get("music"):
            m = plan["music"]
            args += ["-ss", f"{m['start']:.3f}", "-t", f"{plan['total_duration'] + 0.5:.3f}", "-i", m["path"]]
            music_input = n
            n += 1
        if wm["usable"] and wm["type"] == "image":
            args += ["-loop", "1", "-framerate", str(fps), "-t", f"{plan['total_duration'] + 1:.3f}", "-i", wm_files["image"]]
            image_input = n
            n += 1
        graph = build_graph(plan, W=W, H=H, fps=fps, vibe=vibe, wm=wm, wm_files=wm_files, quality=quality,
                            loudness=loudness, music_input=music_input, image_input=image_input)
        (workdir / "graph.txt").write_text(graph, encoding="utf-8")
        tmp_out = workdir / "out.mp4"
        args += ["-filter_complex_script", "graph.txt", "-map", "[vout]", "-map", "[aout]",
                 "-r", str(fps), *encoder_args(encoder, quality),
                 "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
                 "-movflags", "+faststart", "-t", f"{plan['total_duration']:.3f}", str(tmp_out)]
        ffmpeg.run(args, total_seconds=plan["total_duration"], progress=progress, cwd=workdir)
        shutil.move(str(tmp_out), out_path)
        thumb = out_path.with_suffix(".jpg")
        try:
            ffmpeg.run(["-ss", f"{min(1.0, plan['total_duration'] / 3):.2f}", "-i", str(out_path), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "4", str(thumb)])
        except ffmpeg.FFmpegError:
            thumb = None
        info = ffmpeg.probe(out_path)
        return {"file": str(out_path), "thumb": str(thumb) if thumb else None, "probe": info,
                "encoder": resolved_encoder(encoder), "watermark": {k: v for k, v in wm.items() if k != "usable"}
                | {"applied_in_video": wm["usable"] and wm["in_video"], "applied_outro": wm["usable"] and plan["outro"]}}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
