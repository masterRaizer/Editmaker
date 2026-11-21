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


def encoder_args(encoder: str, quality: str, fps: int = 60, tune: str = "film") -> list[str]:
    if encoder == "auto":
        encoder = "h264_nvenc" if ffmpeg.nvenc_usable() else "libx264"
    gop = ["-g", str(fps * 2), "-bf", "3"]
    if encoder == "h264_nvenc":
        if quality == "final":
            return ["-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "17", "-b:v", "0",
                    "-spatial-aq", "1", "-aq-strength", "10", "-rc-lookahead", "32", "-profile:v", "high",
                    "-pix_fmt", "yuv420p", *gop]
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0", "-pix_fmt", "yuv420p", *gop]
    if quality == "final":
        # aq-mode 3 keeps dark gradients from banding; tune animation/film by source type
        return ["-c:v", "libx264", "-crf", "16", "-preset", "slow", "-tune", tune,
                "-x264-params", "aq-mode=3:aq-strength=0.9", "-profile:v", "high", "-pix_fmt", "yuv420p", *gop]
    return ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", *gop]


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


WHIP = ("slide", "smooth", "wipe", "cover", "reveal")


def _windows(times: list[float], length: float) -> str:
    return "+".join(f"between(t,{t:.3f},{t + length:.3f})" for t in times[:16])


def _transition_blur(kind: str, t0: float, t1: float, W: int) -> list[str]:
    """Motion blur that sells a transition: directional for whips/slides, gaussian for zooms.
    Two strengths: light over the whole transition, heavy around the cut in the middle."""
    if t1 - t0 < 0.03:
        return []
    mid0, mid1 = t0 + (t1 - t0) * 0.25, t0 + (t1 - t0) * 0.75
    k = W / 1080.0
    if kind.startswith(WHIP):
        angle = 90 if kind.endswith(("up", "down")) else 0
        return [f"dblur=angle={angle}:radius={max(2, round(10 * k))}:enable='between(t,{t0:.3f},{t1:.3f})'",
                f"dblur=angle={angle}:radius={max(4, round(28 * k))}:enable='between(t,{mid0:.3f},{mid1:.3f})'"]
    if kind.startswith("zoom"):
        return [f"gblur=sigma={2.5 * k:.2f}:enable='between(t,{t0:.3f},{t1:.3f})'",
                f"gblur=sigma={6 * k:.2f}:enable='between(t,{mid0:.3f},{mid1:.3f})'"]
    return []


def segment_video_filter(seg: dict, idx: int, W: int, H: int, fps: int, vibe: str, contrast: float,
                         shake_px: float, flash_amp: float, quality: str, intensity: float = 1.0,
                         slowmo: str = "blend") -> str:
    """slowmo: blend (frame blending, fast) | flow (optical flow via minterpolate, slow) | off (duplicate frames)."""
    D = seg["clip_dur"]
    speed = seg.get("speed")
    final = quality == "final"
    # optical-flow slow motion needs frames at output size; zoom headroom then comes from zoom >= 1.07
    slowed = bool(speed and min(speed["s1"], speed["s2"]) < 1.0)
    flow = bool(final and slowmo == "flow" and slowed)
    blend = bool(slowmo in ("blend", "flow") and slowed and not flow)
    pre = 1.5 if final and not flow else 1.0
    PW, PH = int(round(W * pre / 2) * 2), int(round(H * pre / 2) * 2)
    T = f"(on/{fps})"
    zoom = seg["zoom"]
    p = f"min(1,{T}/{D:.3f})"
    ease = f"({p}*{p}*(3-2*{p}))"
    if zoom == "push":
        z = f"1.03+0.07*{ease}"
    elif zoom == "drift_in":
        z = f"1.02+0.1*{ease}"
    elif zoom == "drift_out":
        z = f"1.12-0.1*{ease}"
    elif zoom == "punch":
        pulses = _impulses(seg.get("shake", []), 0.035 * intensity, 9.0, var=T)
        z = f"1.07+{0.09 * intensity:.3f}*exp(-{T}*9)+{pulses}"
    else:
        z = "1.0"
    sx = sy = "0"
    if shake_px and seg.get("shake"):
        amp = shake_px * pre * W / 1080.0
        sx = _impulses(seg["shake"], amp, 14.0, osc=f"sin({T}*73)", var=T)
        sy = _impulses(seg["shake"], amp * 0.6, 14.0, osc=f"cos({T}*61)", var=T)
    x = f"max(0,min(iw-iw/zoom,(iw-iw/zoom)/2+{sx}))"
    y = f"max(0,min(ih-ih/zoom,(ih-ih/zoom)/2+{sy}))"
    chain = [f"[{idx}:v]setpts=PTS-STARTPTS"]
    if speed:
        # piecewise speed: input time T → screen time; s1 before the split point, s2 after
        a = D * speed["split"] * speed["s1"]
        s1, s2 = speed["s1"], speed["s2"]
        chain.append(f"setpts='if(lt(T,{a:.4f}),T/{s1},{a:.4f}/{s1}+(T-{a:.4f})/{s2})/TB'")
    if flow:
        chain += [f"scale={PW}:{PH}:force_original_aspect_ratio=increase:flags=lanczos", f"crop={PW}:{PH}", "setsar=1",
                  f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:scd=fdiff"]
    else:
        rate = f"framerate=fps={fps}:interp_start=32:interp_end=224:scene=10" if blend else f"fps={fps}"
        chain += [rate, f"scale={PW}:{PH}:force_original_aspect_ratio=increase:flags=lanczos",
                  f"crop={PW}:{PH}", "setsar=1"]
    chain += [
        f"tpad=stop_mode=clone:stop_duration={D:.3f}",
        f"trim=duration={D:.3f}",
        "setpts=PTS-STARTPTS",
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={fps}",
        GRADES.get(vibe, GRADES["custom"]).format(c=f"{contrast:.3f}"),
    ]
    t_in, t_out = seg.get("transition_in", {}), seg.get("transition_out", {})
    chain += _transition_blur(t_in.get("type", "cut"), 0.0, t_in.get("dur", 0.0), W)
    chain += _transition_blur(t_out.get("type", "cut"), D - t_out.get("dur", 0.0), D, W)
    if seg.get("glitch"):
        sh = max(2, round(10 * intensity * W / 1080))
        chain.append(f"rgbashift=rh=-{sh}:bh={sh}:gv={sh // 3}:edge=smear:enable='{_windows(seg['glitch'], 0.08)}'")
    if seg.get("impact"):
        # impact frame: two inverted frames exactly on the drop
        chain.append(f"negate=enable='{_windows(seg['impact'], 1.6 / fps)}'")
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
                loudness: float, music_input: Optional[int], image_input: Optional[int],
                loudnorm: Optional[str] = None, slowmo: str = "blend") -> str:
    segs = plan["segments"]
    fx = plan["effects"]
    parts = []
    for i, seg in enumerate(segs):
        parts.append(segment_video_filter(seg, i, W, H, fps, vibe, fx["contrast"], fx["shake_px"], fx["flash"], quality,
                                          fx.get("intensity", 1.0), slowmo))
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

    # finishing on the whole timeline: crisp detail, then fine film grain (also hides banding in dark gradients)
    if quality == "final":
        finish = ["unsharp=5:5:0.55:5:5:0.0"]
        if fx.get("grain"):
            finish.append(f"noise=alls={max(1, round(6 * fx['grain']))}:allf=t")
        parts.append(f"[{cur}]{','.join(finish)}[fin]")
        cur = "fin"

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
            f"{loudnorm or f'loudnorm=I={loudness}:TP=-1.5:LRA=11'},aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[aout]")
    if music_input is not None:
        parts.append(f"[{music_input}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,{tail}")
    else:
        parts.append(f"[{acur}]{tail}")
    return ";\n".join(parts)


def _place(tmp: Path, target: Path) -> Path:
    """Move the finished file into place. On Windows the old file may be locked by the preview player
    for a moment: retry, then fall back to variant_01_2.mp4, variant_01_3.mp4 …"""
    import time
    for _ in range(6):
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(tmp), target)
            return target
        except PermissionError:
            time.sleep(0.5)
    n = 2
    while True:
        alt = target.with_name(f"{target.stem}_{n}{target.suffix}")
        if not alt.exists():
            shutil.move(str(tmp), alt)
            return alt
        n += 1


def measure_music(plan: dict, loudness: float) -> Optional[str]:
    """First loudnorm pass on the music excerpt; returns a linear (non-pumping) second-pass filter."""
    import json
    import re
    import subprocess
    m = plan.get("music")
    if not m:
        return None
    total = plan["total_duration"]
    fade_len = max(0.4, plan["outro_duration"] * 0.9) if plan["outro"] else 0.4
    af = (f"aresample=48000,apad,atrim=duration={total:.3f},afade=t=out:st={max(0.0, total - fade_len):.3f}:d={fade_len:.3f},"
          f"loudnorm=I={loudness}:TP=-1.5:LRA=11:print_format=json")
    try:
        r = subprocess.run([ffmpeg.FFMPEG, "-hide_banner", "-nostdin", "-ss", f"{m['start']:.3f}", "-t", f"{total + 0.5:.3f}",
                            "-i", m["path"], "-af", af, "-f", "null", "-"], capture_output=True, text=True, timeout=300,
                           **ffmpeg._NOWIN)
        js = json.loads(re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)[-1])
        if float(js["input_i"]) < -70:  # silence: nothing to normalise
            return None
        return (f"loudnorm=I={loudness}:TP=-1.5:LRA=11:measured_I={js['input_i']}:measured_TP={js['input_tp']}:"
                f"measured_LRA={js['input_lra']}:measured_thresh={js['input_thresh']}:offset={js['target_offset']}:"
                f"linear=true")
    except (IndexError, KeyError, ValueError, OSError, subprocess.TimeoutExpired):
        return None


def render(plan: dict, out_path: Path, *, resolution: str, fps: int, vibe: str, wm: dict, encoder: str,
           quality: str, loudness: float = -14.0, progress: Optional[Callable[[float], None]] = None,
           slowmo: str = "blend") -> dict:
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
            args += ["-ss", f"{seg['src_start']:.3f}", "-t", f"{seg.get('src_dur', seg['clip_dur']) + 0.5:.3f}",
                     "-i", seg["path"]]
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
        loud2 = measure_music(plan, loudness) if music_input is not None else None
        graph = build_graph(plan, W=W, H=H, fps=fps, vibe=vibe, wm=wm, wm_files=wm_files, quality=quality,
                            loudness=loudness, music_input=music_input, image_input=image_input,
                            loudnorm=loud2, slowmo=slowmo)
        (workdir / "graph.txt").write_text(graph, encoding="utf-8")
        tmp_out = workdir / "out.mp4"
        args += [*ffmpeg.filter_script_args("graph.txt"), "-map", "[vout]", "-map", "[aout]",
                 "-r", str(fps), *encoder_args(encoder, quality, fps, "animation" if plan.get("anime") else "film"),
                 "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
                 "-movflags", "+faststart", "-t", f"{plan['total_duration']:.3f}", str(tmp_out)]
        ffmpeg.run(args, total_seconds=plan["total_duration"], progress=progress, cwd=workdir)
        out_path = _place(tmp_out, out_path)
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
