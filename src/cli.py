"""Command line: serve (browser UI), check, selftest. Used by run.py and by EditMaker.exe."""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from .core.paths import default_data_dir

def cmd_check() -> int:
    from .core import ffmpeg
    info = {**ffmpeg.available(), "nvenc": ffmpeg.nvenc_usable(), "gpu": ffmpeg.gpu_name(),
            "python": sys.version.split()[0]}
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ffmpeg"] and info["ffprobe"] else 1


def cmd_selftest(out_dir: Path, quality: str, resolution: str, fps: int) -> int:
    from .core import synthetic
    from .core.service import EditMaker

    out_dir = out_dir.resolve()
    data = out_dir / "_data"
    src_dir = out_dir / "_sources"
    lib = out_dir / "_music"
    paths = synthetic.make_fixture_set(src_dir, lib)
    em = EditMaker(data)
    em.settings.update({"music_library": str(lib), "output_folder": str(out_dir)})
    em.jobs.wait(em.scan_sources([str(src_dir)]))
    em.jobs.wait(em.scan_music())
    srcs = em.sources()
    res = em.create_project({
        "name": "selftest", "source_ids": [s["id"] for s in srcs], "vibe": "phonk_aggressive", "track": "auto",
        "render": {"resolution": resolution, "fps": fps, "quality": quality, "duration": 10},
        "watermark": {"type": "text", "text": "@editmaker_test", "in_video": True, "outro": True},
    })
    ok = True
    for jid in res["jobs"]:
        j = em.jobs.wait(jid, timeout=1800)
        print(j["title"], j["status"], j.get("error") or "")
        ok &= j["status"] == "done"
    p = em.project(res["project"]["id"])
    for v in p["variants"]:
        print(f"  {v['idx']:02d} {v['style_name']:<28} {v['status']:<9} {v['file_path']}")
    print("meta.json:", Path(p["output_dir"]) / "meta.json")
    print("sources:", paths)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="EditMaker")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "check", "selftest"])
    ap.add_argument("target", nargs="?", default="output/selftest_run")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--data", default=str(default_data_dir()))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--quality", default="draft", choices=["draft", "final"])
    ap.add_argument("--resolution", default="540x960")
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args(argv)
    if a.command == "check":
        return cmd_check()
    if a.command == "selftest":
        return cmd_selftest(Path(a.target), a.quality, a.resolution, a.fps)

    import uvicorn
    from .core.config import Settings
    from .server import create_app

    cfg = Settings(Path(a.data)).get()
    host = a.host or cfg["server"]["host"]
    port = a.port or cfg["server"]["port"]
    if a.port:
        Settings(Path(a.data)).update({"server": {"port": port}})
    app = create_app(Path(a.data))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"EditMaker: {url}")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0

