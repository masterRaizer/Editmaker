"""Render output, watermark presence, approval gate, API endpoints."""
import subprocess
from pathlib import Path

import numpy as np
import pytest

from src.core import ffmpeg, media, planner, render
from src.core.service import ApprovalRequired, BadRequest, EditMaker
from src.publishers.base import PublishResult

W, H, FPS = 540, 960, 30


def frame(path: Path, t: float) -> np.ndarray:
    r = subprocess.run([ffmpeg.FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(H, W).astype(np.float32)


def loudness(path: Path) -> float:
    r = subprocess.run([ffmpeg.FFMPEG, "-hide_banner", "-i", str(path), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    line = [ln for ln in r.stderr.splitlines() if ln.strip().startswith("I:")][-1]
    return float(line.split()[1])


@pytest.fixture(scope="module")
def plan(media_dir):
    p = media_dir["clip_a"]
    info = media.index_file(p)
    src = [planner.SourceInfo(1, str(p), info["duration"], True, info["title"], media.analyze_visual(p))]
    from src.core import audio
    track = {"path": str(media_dir["beat"]), "title": "beat", "analysis": audio.analyze_track(media_dir["beat"])}
    return planner.build_plan(src, "drop", 2, 4.0, track, True, 2.0)


WM = {"type": "text", "text": "@WATERMARK", "drift": False, "opacity": 0.9, "size": 0.05,
      "position": "bottom_right", "in_video": True, "outro": True}


def test_render_output_format(plan, tmp_path):
    out = tmp_path / "v.mp4"
    res = render.render(plan, out, resolution="540x960", fps=FPS, vibe="phonk_aggressive", wm=WM,
                        encoder="libx264", quality="draft")
    pr = res["probe"]
    assert (pr["width"], pr["height"], pr["fps"]) == (W, H, FPS)
    assert pr["vcodec"] == "h264" and pr["acodec"] == "aac" and pr["sample_rate"] == 48000
    assert abs(pr["duration"] - plan["total_duration"]) < 0.15
    assert Path(res["thumb"]).is_file()
    assert res["watermark"]["applied_in_video"] and res["watermark"]["applied_outro"]
    assert abs(loudness(out) - (-14.0)) < 1.5


def test_watermark_in_video_and_outro(plan, tmp_path):
    on = tmp_path / "on.mp4"
    off = tmp_path / "off.mp4"
    render.render(plan, on, resolution="540x960", fps=FPS, vibe="custom", wm=WM, encoder="libx264", quality="draft")
    render.render(plan, off, resolution="540x960", fps=FPS, vibe="custom", wm={**WM, "in_video": False},
                  encoder="libx264", quality="draft")
    t = plan["main_duration"] / 2
    a, b = frame(on, t), frame(off, t)
    corner = (slice(int(H * 0.75), H), slice(int(W * 0.4), W))
    centre = (slice(int(H * 0.3), int(H * 0.6)), slice(int(W * 0.2), int(W * 0.8)))
    assert np.abs(a[corner] - b[corner]).mean() > 2.0      # watermark drawn in the corner
    assert np.abs(a[centre] - b[centre]).mean() < 1.0      # centre of the frame untouched
    # outro: dark card with the watermark clearly visible in the middle
    end = frame(on, plan["total_duration"] - 0.4)
    mid = end[int(H * 0.4):int(H * 0.6), :]
    assert end[:int(H * 0.2)].mean() < 25
    assert mid.max() > 200


def test_outro_disabled(plan, tmp_path, media_dir):
    p2 = dict(plan)
    p2 = planner.build_plan([planner.SourceInfo(1, str(media_dir["clip_a"]), 9.0, True, "Dexter", None)], "cold", 1, 4.0,
                            None, False, 2.0)
    out = tmp_path / "noout.mp4"
    res = render.render(p2, out, resolution="540x960", fps=FPS, vibe="custom", wm={**WM, "outro": False},
                        encoder="libx264", quality="draft")
    assert abs(res["probe"]["duration"] - p2["main_duration"]) < 0.15
    assert res["watermark"]["applied_outro"] is False


def test_image_watermark(plan, tmp_path):
    png = tmp_path / "wm.png"
    subprocess.run([ffmpeg.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=200x60,format=rgba",
                    "-frames:v", "1", str(png)], check=True)
    out = tmp_path / "img.mp4"
    res = render.render(plan, out, resolution="540x960", fps=FPS, vibe="custom",
                        wm={**WM, "type": "image", "image_path": str(png)}, encoder="libx264", quality="draft")
    assert res["watermark"]["type"] == "image"
    end = frame(out, plan["total_duration"] - 0.4)
    assert end[int(H * 0.45):int(H * 0.55), int(W * 0.4):int(W * 0.6)].mean() > 200


# ------------------------------------------------------------------ service + approval gate
class FakePublisher:
    def __init__(self, name):
        self.name, self.calls = name, []

    def status(self):
        return {"configured": True, "authorized": True, "instructions": ""}

    def publish(self, req, progress=None):
        assert req.video_path.is_file()
        self.calls.append(req)
        if progress:
            progress(1.0)
        return PublishResult(self.name, "published", f"{self.name}-id", f"https://example.test/{self.name}", {})


@pytest.fixture(scope="module")
def app(media_dir, tmp_path_factory):
    d = tmp_path_factory.mktemp("app")
    pubs = {"youtube": FakePublisher("youtube"), "tiktok": FakePublisher("tiktok")}
    em = EditMaker(d / "data", publishers=pubs)
    em.settings.update({"music_library": str(media_dir["lib"]), "output_folder": str(d / "out")})
    assert em.jobs.wait(em.scan_sources([str(media_dir["src"])]))["status"] == "done"
    assert em.jobs.wait(em.scan_music())["status"] == "done"
    res = em.create_project({
        "name": "Gate test", "source_ids": [s["id"] for s in em.sources()], "vibe": "auto", "track": "auto",
        "render": {"resolution": "540x960", "fps": 30, "quality": "draft", "duration": 4},
        "watermark": {"type": "text", "text": "@gate", "in_video": True, "outro": True},
    })
    for j in res["jobs"]:
        job = em.jobs.wait(j, 600)
        assert job["status"] == "done", job
    return em, res["project"]["id"], pubs


def test_project_layout(app):
    em, pid, _ = app
    p = em.project(pid)
    assert [v["style"] for v in p["variants"]] == planner.STYLE_ORDER
    out = Path(p["output_dir"])
    for i in range(1, 5):
        assert (out / f"variant_{i:02d}.mp4").is_file()
    import json
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert len(meta["variants"]) == 4
    assert all(v["watermark"]["applied_in_video"] and v["watermark"]["applied_outro"] for v in meta["variants"])
    assert all(v["status"] == "rendered" for v in meta["variants"])


def test_publish_requires_approval(app):
    em, pid, pubs = app
    v = em.project(pid)["variants"][0]
    with pytest.raises(ApprovalRequired):
        em.publish(v["id"], ["youtube"])
    with pytest.raises(ApprovalRequired):
        em.publish_approved(pid, ["youtube"])
    assert not pubs["youtube"].calls


def test_approve_publish_flow(app):
    em, pid, pubs = app
    v = em.project(pid)["variants"][1]
    em.approve(v["id"])
    job = em.jobs.wait(em.publish(v["id"], ["youtube", "tiktok"]))
    assert job["status"] == "done", job
    assert job["result"]["youtube"]["status"] == "published"
    v2 = em.project(pid)["variants"][1]
    assert v2["status"] == "published" and len(v2["publications"]) == 2
    req = pubs["youtube"].calls[-1]
    assert req.title == v2["metadata"]["youtube"]["title"] and req.privacy == "private"
    assert pubs["tiktok"].calls[-1].options["mode"] == "draft"


def test_metadata_edit_revokes_approval(app):
    em, pid, pubs = app
    v = em.project(pid)["variants"][2]
    em.approve(v["id"])
    em.update_metadata(v["id"], {"youtube": {"title": "Новый заголовок"}})
    v2 = em.db.variant(v["id"])
    assert v2["status"] == "rendered"
    with pytest.raises(ApprovalRequired):
        em.publish(v["id"], ["youtube"])


def test_file_change_after_approval_blocks(app):
    em, pid, pubs = app
    v = em.project(pid)["variants"][3]
    em.approve(v["id"])
    with open(v["file_path"], "ab") as f:
        f.write(b"tamper")
    with pytest.raises(ApprovalRequired):
        em.publish(v["id"], ["youtube"])
    with pytest.raises(BadRequest):
        em.approve(v["id"])


def test_more_variants_and_rerender_clear_approval(app):
    em, pid, _ = app
    jobs = em.add_variants(pid, 1)
    assert em.jobs.wait(jobs[0], 600)["status"] == "done"
    p = em.project(pid)
    assert len(p["variants"]) == 5 and p["variants"][-1]["idx"] == 5
    vid = p["variants"][0]["id"]
    em.approve(vid)
    assert em.jobs.wait(em.render_variant(vid), 600)["status"] == "done"
    assert em.db.variant(vid)["status"] == "rendered"


# ------------------------------------------------------------------ API
def test_api(app):
    from fastapi.testclient import TestClient
    from src.server import create_app
    em, pid, _ = app
    c = TestClient(create_app(core=em))
    st = c.get("/api/state").json()
    assert "youtube" in st["auth"] and st["env"]["ffmpeg"]
    assert c.get("/").status_code == 200 and "Водяной знак" in c.get("/").text
    assert c.get("/static/app.js").status_code == 200
    proj = c.get(f"/api/projects/{pid}").json()
    vid = proj["variants"][0]["id"]
    r = c.post(f"/api/variants/{vid}/publish", json={"platforms": ["youtube"]})
    assert r.status_code == 409 and r.json()["code"] == "approval_required"
    assert c.post(f"/api/variants/{vid}/approve").json()["status"] == "approved"
    assert c.post(f"/api/variants/{vid}/publish", json={"platforms": ["youtube"]}).status_code == 200
    f = proj["variants"][0]["file_path"]
    m = c.get("/api/media", params={"path": f}, headers={"Range": "bytes=0-99"})
    assert m.status_code == 206 and len(m.content) == 100
    assert c.get("/api/media", params={"path": "/etc/passwd"}).status_code == 404
    fs = c.get("/api/fs/list", params={"path": str(Path(f).parent), "files": "mp4"}).json()
    assert any(x["name"].endswith(".mp4") for x in fs["files"])
    assert c.post("/api/projects", json={"source_ids": []}).status_code == 400
    assert c.put("/api/config", json={"vibe": "phonk_aggressive"}).json()["vibe"] == "phonk_aggressive"


def test_final_quality_effects_render(media_dir, tmp_path):
    """Final quality exercises every effect filter: ramps + frame blending, whip/zoom blur, RGB shift,
    impact frame, sharpen, grain, two-pass loudnorm."""
    from src.core import audio
    p = media_dir["clip_a"]
    info = media.index_file(p)
    src = [planner.SourceInfo(1, str(p), info["duration"], True, info["title"], media.analyze_visual(p))]
    track = {"path": str(media_dir["beat"]), "title": "beat", "analysis": audio.analyze_track(media_dir["beat"])}
    plan = None
    for seed in range(1, 30):
        cand = planner.build_plan(src, "drop", seed, 4.0, track, True, 1.0)
        segs = cand["segments"]
        if any(s["speed"] for s in segs) and any(s["glitch"] for s in segs) and any(s["impact"] for s in segs):
            plan = cand
            break
    assert plan, "no seed produced all effects"
    plan["effects"]["grain"] = 0.5
    assert render.measure_music(plan, -14.0).startswith("loudnorm=I=-14.0")
    out = tmp_path / "final.mp4"
    res = render.render(plan, out, resolution="540x960", fps=FPS, vibe="dexter_cold_dark", wm=WM,
                        encoder="libx264", quality="final")
    assert abs(res["probe"]["duration"] - plan["total_duration"]) < 0.15
    assert abs(loudness(out) - (-14.0)) < 1.5
    # the impact frame is inverted: average brightness jumps against the frame just before
    t = plan["impact_time"]
    before, at = frame(out, t - 0.1), frame(out, t + 0.5 / FPS)
    assert abs(at.mean() - before.mean()) > 20


def test_delete_variant(app):
    em, pid, _ = app
    p = em.project(pid)
    v = [x for x in p["variants"] if x["status"] != "published"][-1]
    f = Path(v["file_path"])
    assert f.is_file()
    after = em.delete_variant(v["id"])
    assert not f.exists() and all(x["id"] != v["id"] for x in after["variants"])
    pub = [x for x in after["variants"] if x["status"] == "published"]
    if pub:
        with pytest.raises(BadRequest):
            em.delete_variant(pub[0]["id"])
