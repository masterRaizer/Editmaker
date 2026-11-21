"""Timeline export (FCPXML / EDL / CapCut pack), WiX source generation, desktop helpers."""
import xml.dom.minidom
from pathlib import Path

import pytest

from src.core import audio, export_nle, media, planner


@pytest.fixture(scope="module")
def plan(media_dir):
    srcs = []
    for i, key in enumerate(("clip_a", "clip_b"), 1):
        p = media_dir[key]
        info = media.index_file(p)
        srcs.append(planner.SourceInfo(i, str(p), info["duration"], info["has_audio"], info["title"], None))
    track = {"path": str(media_dir["beat"]), "title": "beat", "analysis": audio.analyze_track(media_dir["beat"])}
    return planner.build_plan(srcs, "drop", 4, 5.0, track, True, 2.0)


def test_fcpxml_is_valid_and_cut_on_beats(plan, tmp_path):
    res = export_nle.export(plan, tmp_path, "fcpxml", resolution="1080x1920", fps=60, vibe="custom", wm={}, name="t")
    doc = xml.dom.minidom.parse(res["path"])
    spine = doc.getElementsByTagName("spine")[0]
    clips = [c for c in spine.childNodes if getattr(c, "tagName", "") == "asset-clip"]
    assert len(clips) == len(plan["segments"])
    offsets = [int(c.getAttribute("offset").split("/")[0]) / 60 for c in clips]
    for off, b in zip(offsets, plan["boundaries"]):
        assert abs(off - b) <= 1 / 60 + 1e-6
    music = [c for c in doc.getElementsByTagName("asset-clip") if c.getAttribute("lane") == "-1"]
    assert len(music) == 1 and music[0].getElementsByTagName("marker")
    assert (tmp_path / "beats.txt").is_file()


def test_edl(plan, tmp_path):
    res = export_nle.export(plan, tmp_path, "edl", resolution="1080x1920", fps=60, vibe="custom", wm={}, name="t")
    text = Path(res["path"]).read_text(encoding="utf-8")
    events = [ln for ln in text.splitlines() if ln[:3].isdigit()]
    assert len(events) == len(plan["segments"]) + 1          # + music on A
    assert events[0].split()[6] == "00:00:00:00"
    assert "FCM: NON-DROP FRAME" in text


def test_capcut_pack(plan, tmp_path):
    wm = {"type": "text", "text": "@pack", "in_video": True, "outro": True}
    res = export_nle.export(plan, tmp_path, "capcut", resolution="540x960", fps=30, vibe="custom", wm=wm, name="t")
    folder = Path(res["folder"])
    clips = sorted(folder.glob("[0-9][0-9]_*.mp4"))
    assert len(clips) == len(plan["segments"])
    durations = [media.ffmpeg.probe(c)["duration"] for c in clips]
    visible = [s["out_end"] - s["out_start"] for s in plan["segments"]]
    for d, v in zip(durations, visible):
        assert abs(d - v) < 0.1
    assert (folder / "music.wav").is_file() and (folder / "watermark.png").is_file()
    assert (folder / "beats.txt").is_file()


def test_wxs_generation(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_wxs", Path(__file__).parents[1] / "packaging" / "make_wxs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = tmp_path / "EditMaker"
    (app / "_internal" / "ffmpeg").mkdir(parents=True)
    (app / "EditMaker.exe").write_bytes(b"x")
    (app / "_internal" / "ffmpeg" / "ffmpeg.exe").write_bytes(b"x")
    doc = xml.dom.minidom.parseString(mod.build(app, "1.2.3"))
    assert doc.getElementsByTagName("Package")[0].getAttribute("Version") == "1.2.3"
    assert len(doc.getElementsByTagName("Component")) == 2
    assert {s.getAttribute("Directory") for s in doc.getElementsByTagName("Shortcut")} == {"ProgramMenuFolder", "DesktopFolder"}
    ids = [e.getAttribute("Id") for e in doc.getElementsByTagName("*")
           if e.getAttribute("Id") and not e.tagName.endswith("Ref")]
    assert len(ids) == len(set(ids)) and all(len(i) <= 72 for i in ids)


def test_free_port_and_frozen_paths(monkeypatch, tmp_path):
    from src import desktop
    from src.core import paths
    port = desktop._free_port(0)
    assert 0 < port < 65536
    monkeypatch.setenv("EDITMAKER_DATA", str(tmp_path / "d"))
    assert paths.default_data_dir() == tmp_path / "d"
    fake = tmp_path / "ff.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("EDITMAKER_FFMPEG", str(fake))
    assert paths.ffmpeg_binary("ffmpeg") == str(fake)
