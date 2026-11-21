"""Watermark filters: in-video (whole edit) and outro (end card).

Files the filters need (font, text, png) are copied into the render work dir and referenced
by short relative names, so Windows drive letters and odd characters never reach the filtergraph.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import default_font

POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "bottom_center")


def normalize(wm: dict) -> dict:
    """Validate and fill defaults; returns the dict that is stored in meta.json/DB."""
    out = dict(wm or {})
    out["type"] = out.get("type") if out.get("type") in ("text", "image") else "text"
    out["text"] = (out.get("text") or "").strip()
    out["image_path"] = (out.get("image_path") or "").strip()
    out["opacity"] = max(0.05, min(1.0, float(out.get("opacity", 0.35))))
    out["size"] = max(0.01, min(0.5, float(out.get("size", 0.045))))
    out["position"] = out.get("position") if out.get("position") in POSITIONS else "bottom_right"
    color = str(out.get("color") or "#FFFFFF").strip()
    out["color"] = color if re.fullmatch(r"#?[0-9A-Fa-f]{6}", color) else "#FFFFFF"
    out["drift"] = bool(out.get("drift", True))
    out["in_video"] = bool(out.get("in_video", True))
    out["outro"] = bool(out.get("outro", True))
    out["outro_duration"] = max(1.0, min(5.0, float(out.get("outro_duration", 2.0))))
    out["font_path"] = (out.get("font_path") or "").strip()
    usable = (out["type"] == "text" and out["text"]) or (out["type"] == "image" and Path(out["image_path"]).is_file())
    out["usable"] = bool(usable)
    return out


def prepare_files(wm: dict, workdir: Path) -> dict:
    """Copy font/png and write the text file. Returns relative names."""
    files = {}
    if wm["type"] == "text":
        font = wm["font_path"] if wm["font_path"] and Path(wm["font_path"]).is_file() else default_font()
        if not font:
            raise RuntimeError("Не найден шрифт для текстового водяного знака: укажите font_path (.ttf)")
        shutil.copyfile(font, workdir / "wm_font.ttf")
        (workdir / "wm_text.txt").write_text(wm["text"], encoding="utf-8")
        files.update(font="wm_font.ttf", text="wm_text.txt")
    else:
        shutil.copyfile(wm["image_path"], workdir / "wm_image.png")
        files["image"] = "wm_image.png"
    return files


def _ffcolor(hex_color: str, alpha: float) -> str:
    return "0x" + hex_color.lstrip("#").upper() + f"@{alpha:.3f}"


def _xy(position: str, fw: str, fh: str, ww: str, wh: str, drift: bool, mx: str, my: str) -> tuple[str, str]:
    """x/y expressions. fw/fh: frame size names, ww/wh: watermark size names (filter specific)."""
    if position == "top_left":
        x, y = mx, my
    elif position == "top_right":
        x, y = f"{fw}-{ww}-{mx}", my
    elif position == "bottom_left":
        x, y = mx, f"{fh}-{wh}-{my}"
    elif position == "bottom_center":
        x, y = f"({fw}-{ww})/2", f"{fh}-{wh}-{my}"
    else:
        x, y = f"{fw}-{ww}-{mx}", f"{fh}-{wh}-{my}"
    if drift:
        # slow Lissajous drift, ~1-2 % of the frame: survives a careless crop, never reaches the centre
        x = f"({x})+{fw}*0.012*sin(2*PI*t/11)"
        y = f"({y})+{fh}*0.008*sin(2*PI*t/7)"
    return x, y


def in_video_filter(wm: dict, files: dict, W: int, H: int, label_in: str, label_out: str,
                    image_label: str | None = None) -> str:
    """Filter chain putting the watermark over the whole main part."""
    mx, my = f"{round(W * 0.05)}", f"{round(H * (0.09 if H > W else 0.06))}"
    a = wm["opacity"]
    if wm["type"] == "text":
        fs = max(12, round(H * wm["size"] if H > W else H * wm["size"] * 1.2))
        x, y = _xy(wm["position"], "w", "h", "tw", "th", wm["drift"], mx, my)
        return (f"[{label_in}]drawtext=fontfile={files['font']}:textfile={files['text']}:fontsize={fs}:"
                f"fontcolor={_ffcolor(wm['color'], a)}:shadowcolor=0x000000@{a * 0.6:.3f}:shadowx=2:shadowy=2:"
                f"x='{x}':y='{y}'[{label_out}]")
    w = max(16, round(W * wm["size"] * 4))  # image width share: size 0.045 -> 18 % of frame width
    x, y = _xy(wm["position"], "main_w", "main_h", "overlay_w", "overlay_h", wm["drift"], mx, my)
    return (f"[{image_label}]scale={w}:-1,format=rgba,colorchannelmixer=aa={a:.3f}[wmimg];"
            f"[{label_in}][wmimg]overlay=x='{x}':y='{y}':eof_action=repeat:format=auto,format=yuv420p[{label_out}]")


def outro_filter(wm: dict, files: dict, W: int, H: int, fps: int, duration: float, label_out: str,
                 image_label: str | None = None) -> str:
    """End card: dark background, the watermark fades in and stays clearly visible."""
    bg = f"color=c=0x0B0B0C:s={W}x{H}:r={fps}:d={duration:.3f},format=yuv420p,settb=1/{fps}"
    fade = "min(1,max(0,(t-0.35)/0.45))"
    if wm["type"] == "text":
        fs = max(16, round(min(W, H) * 0.085))
        return (f"{bg}[obg];[obg]drawtext=fontfile={files['font']}:textfile={files['text']}:fontsize={fs}:"
                f"fontcolor={_ffcolor(wm['color'], 1.0)}:alpha='{fade}':x=(w-tw)/2:y=(h-th)/2[{label_out}]")
    w = round(min(W, H) * 0.5)
    return (f"{bg}[obg];[{image_label}]scale={w}:-1,format=rgba,"
            f"fade=t=in:st=0.35:d=0.45:alpha=1[oimg];"
            f"[obg][oimg]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2:shortest=1,format=yuv420p[{label_out}]")
