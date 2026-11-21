from pathlib import Path

import numpy as np
import pytest

from src.core import audio, media, metadata, music, planner


@pytest.mark.parametrize("name,title,scene", [
    ("Dexter.S01E03.Popping.Cherry.1080p.WEB-DL.mkv", "Dexter", "S01E03 Popping Cherry"),
    ("[SubsPlease] Chainsaw Man - 05 (1080p) [ABCD1234].mkv", "Chainsaw Man", "E05"),
    ("Jujutsu_Kaisen_S02E10_x265_10bit.mp4", "Jujutsu Kaisen", "S02E10"),
    ("Blade Runner 2049 final fight.mov", "Blade Runner 2049 final fight", ""),
])
def test_extract_title(name, title, scene):
    assert media.extract_title(Path("/ssd/x") / name) == (title, scene)


def test_index_and_visual_analysis(media_dir):
    info = media.index_file(media_dir["clip_a"])
    assert info["width"] == 640 and info["height"] == 360
    assert info["fps"] == 30 and info["has_audio"] and abs(info["duration"] - 9.0) < 0.1
    an = media.analyze_visual(media_dir["clip_a"])
    # testsrc2 → mandelbrot → smptebars: three shots, cuts near 3 s and 6 s
    starts = [s[0] for s in an["shots"]]
    assert len(an["shots"]) == 3, an["shots"]
    assert abs(starts[1] - 3.0) <= 0.3 and abs(starts[2] - 6.0) <= 0.3
    files = list(media.iter_videos([media_dir["src"]]))
    assert len(files) == 2


@pytest.mark.parametrize("bpm", [90.0, 120.0])
def test_bpm_and_beats(media_dir, bpm):
    path = media_dir["beat"] if bpm == 120 else media_dir["beat_cold"]
    a = audio.analyze_track(path)
    assert abs(a["bpm"] - bpm) < 1.5, a["bpm"]
    period = 60 / bpm
    ib = np.diff(a["beats"])
    assert abs(float(np.median(ib)) - period) < 0.03
    # beats land on the kicks (multiples of the period)
    phase = [(b / period) % 1 for b in a["beats"][2:-2]]
    assert all(min(p, 1 - p) < 0.08 for p in phase)
    assert a["confidence"] > 0.8


def test_silence_has_no_beats():
    a = audio.analyze_signal(np.zeros(22050 * 3, dtype=np.float32))
    assert a["bpm"] == 0 and a["beats"] == []


def _sources(media_dir):
    out = []
    for i, key in enumerate(("clip_a", "clip_b"), 1):
        p = media_dir[key]
        info = media.index_file(p)
        out.append(planner.SourceInfo(i, str(p), info["duration"], info["has_audio"], info["title"], media.analyze_visual(p)))
    return out


def test_plans_are_beat_synced_and_distinct(media_dir):
    srcs = _sources(media_dir)
    track = {"path": str(media_dir["beat"]), "title": "beat", "analysis": audio.analyze_track(media_dir["beat"])}
    plans = {s: planner.build_plan(srcs, s, 1, 10.0, track, True, 2.0) for s in planner.STYLE_ORDER}
    for key, p in plans.items():
        beats = set(round(b, 3) for b in p["beats"])
        hook_half = planner.STYLES[key].get("hook_seconds", 0)
        for b in p["boundaries"]:
            if b > hook_half + 0.3:
                assert round(b, 3) in beats, (key, b)
        # clip durations add up to the timeline (transitions overlap by their length)
        overlap = sum(s["transition_in"]["dur"] for s in p["segments"]) + p["outro_fade"] / 2
        total = sum(s["clip_dur"] for s in p["segments"])
        assert abs(total - overlap - p["main_duration"]) < 0.01
        assert p["total_duration"] == pytest.approx(p["main_duration"] + 2.0)
        for s in p["segments"]:
            assert s["src_start"] >= 0
    assert plans["drop"]["avg_shot"] < plans["cold"]["avg_shot"]
    assert plans["hook"]["hook_first_cut"] <= 1.5
    assert any(s["shake"] for s in plans["drop"]["segments"])
    assert all(not s["shake"] and not s["flash"] for s in plans["flow"]["segments"])


def test_flashes_never_strobe(media_dir):
    srcs = _sources(media_dir)
    track = {"path": str(media_dir["beat"]), "title": "beat", "analysis": audio.analyze_track(media_dir["beat"])}
    for seed in range(5):
        p = planner.build_plan(srcs, "drop", seed, 12.0, track, True, 2.0)
        times = sorted(s["out_start"] - s["transition_in"]["dur"] / 2 + t for s in p["segments"] for t in s["flash"])
        assert all(b - a >= 0.44 for a, b in zip(times, times[1:]))


def test_plan_without_music_uses_grid(media_dir):
    p = planner.build_plan(_sources(media_dir), "cold", 3, 8.0, None, False, 2.0)
    assert p["music"] is None and p["bpm"] == 90 and p["outro_duration"] == 0
    assert p["total_duration"] == p["main_duration"]


def test_music_library_scan_and_pick(media_dir, tmp_path):
    from src.core.db import DB
    db = DB(tmp_path / "t.db")
    r = music.scan_library(db, media_dir["lib"])
    assert r["analyzed"] == 2 and not r["errors"]
    assert music.scan_library(db, media_dir["lib"])["cached"] == 2
    t = music.pick_track(db, "phonk_aggressive", [{"energy": 0.6, "cut_rate": 40}], 10.0)
    assert t["vibe"] == "phonk_aggressive" and abs(t["bpm"] - 120) < 2
    # empty vibe folder falls back to another vibe
    assert music.pick_track(db, "moody_cinematic", [], 10.0) is not None
    assert music.suggest_vibe([{"energy": 0.6, "cut_rate": 40}]) == "phonk_aggressive"
    assert music.suggest_vibe([{"energy": 0.05, "cut_rate": 2}]) == "moody_cinematic"


def test_metadata_generation():
    m = metadata.generate(titles=["Dexter", "Night Hunter"], scenes=["S01E03", "E05"],
                          paths=["/ssd/Dexter.mkv", "/ssd/anime/[SubsPlease] Night Hunter - 05.mkv"],
                          vibe="dexter_cold_dark", style="cold", language="ru", disclaimer="Фан-эдит.",
                          channel_tags=["mychannel"], youtube_privacy="unlisted", tiktok_privacy="SELF_ONLY",
                          tiktok_mode="draft", music_title="Track", seed=1)
    yt, tt = m["youtube"], m["tiktok"]
    assert yt["title"].startswith("Dexter") and len(yt["title"]) <= 100 and "#shorts" in yt["title"]
    assert "Фан-эдит." in yt["description"] and "Фан-эдит." in tt["description"]
    assert "mychannel" in yt["tags"] and "#mychannel" in tt["hashtags"]
    assert "#animeedit" in tt["hashtags"]
    assert all(h.startswith("#") for h in tt["hashtags"])
    assert yt["privacy"] == "unlisted" and tt["mode"] == "draft"


def test_downbeats_and_drop(tmp_path):
    from src.core import ffmpeg as ff
    # kick every 0.5 s, accented every 2 s (bar start), quiet until 12 s then loud → drop at 12 s
    expr = ("0.9*sin(2*PI*(55+80*exp(-mod(t,0.5)*30))*t)*exp(-mod(t,0.5)*12)"
            "*(1+1.5*lt(mod(t,2),0.5))*if(gt(t,12),1,0.25)")
    p = tmp_path / "drop.wav"
    ff.run(["-f", "lavfi", "-i", f"aevalsrc='{expr}':s=44100:d=30", str(p)])
    a = audio.analyze_track(p)
    phase = [d % 2.0 for d in a["downbeats"][1:-1]]
    assert all(min(x, 2 - x) < 0.08 for x in phase), a["downbeats"][:6]
    assert a["drops"] and abs(a["drops"][0] - 12.0) < 0.6


def test_effects_in_plans(media_dir):
    srcs = _sources(media_dir)
    track = {"path": str(media_dir["beat"]), "title": "beat", "analysis": audio.analyze_track(media_dir["beat"])}
    ramps = glitch = impacts = 0
    for seed in range(1, 8):
        p = planner.build_plan(srcs, "drop", seed, 10.0, track, True, 2.0)
        types = [s["transition_in"]["type"] for s in p["segments"]]
        assert all(a != b or a == "cut" for a, b in zip(types, types[1:])), types
        for s in p["segments"]:
            if s["speed"]:
                ramps += 1
                sp = s["speed"]
                o = s["clip_dur"] * sp["split"]
                assert abs(s["src_dur"] - (o * sp["s1"] + (s["clip_dur"] - o) * sp["s2"])) < 0.01
            else:
                assert s["src_dur"] == s["clip_dur"]
            glitch += len(s["glitch"])
            impacts += len(s["impact"])
            assert s["transition_out"]["type"]
    assert ramps and glitch and impacts
    calm = planner.build_plan(srcs, "drop", 1, 10.0, track, True, 2.0, intensity=0.5)
    assert calm["effects"]["shake_px"] < planner.STYLES["drop"]["shake"]
    # no ramps without music (source audio would drift)
    assert not any(s["speed"] for s in planner.build_plan(srcs, "flow", 2, 10.0, None, True, 2.0)["segments"])
