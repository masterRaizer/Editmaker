"""Application service: projects, variant generation, render jobs, approval gate, publishing."""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from . import export_nle, ffmpeg as ff, media, metadata, music, planner, render, watermark
from .config import RESOLUTIONS, VIBES, Settings
from .db import DB
from ..publishers.base import NotConfigured, PublishError, PublishRequest
from ..publishers.tiktok_publisher import TikTokPublisher
from ..publishers.youtube_publisher import YouTubePublisher

STATUSES = ("draft", "rendered", "approved", "published", "failed")
PLATFORMS = ("youtube", "tiktok")


class ApprovalRequired(Exception):
    """Publishing was attempted for a variant the user has not approved (or that changed after approval)."""


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


# --------------------------------------------------------------------------- jobs
class Jobs:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
        self._io_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="io")

    def submit(self, kind: str, title: str, fn: Callable[[Callable[[float, str], None]], object], *,
               ref: Optional[dict] = None, pool: str = "io") -> str:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": kind, "title": title, "status": "queued", "progress": 0.0, "message": "",
               "result": None, "error": None, "ref": ref or {}, "created": time.time(), "finished": None}
        with self._lock:
            self._jobs[jid] = job

        def report(p: float, msg: str = "") -> None:
            job["progress"] = round(max(0.0, min(1.0, p)), 4)
            if msg:
                job["message"] = msg

        def runner():
            job["status"] = "running"
            try:
                job["result"] = fn(report)
                job["status"] = "done"
                job["progress"] = 1.0
            except Exception as e:  # noqa: BLE001
                job["status"] = "error"
                job["error"] = str(e)
                job["trace"] = traceback.format_exc()[-2000:]
            finally:
                job["finished"] = time.time()

        (self._render_pool if pool == "render" else self._io_pool).submit(runner)
        return jid

    def get(self, jid: str) -> Optional[dict]:
        j = self._jobs.get(jid)
        return {k: v for k, v in j.items() if k != "trace"} if j else None

    def list(self, active_only: bool = False) -> list[dict]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda j: j["created"], reverse=True)
        if active_only:
            items = [j for j in items if j["status"] in ("queued", "running")]
        return [{k: v for k, v in j.items() if k != "trace"} for j in items[:50]]

    def wait(self, jid: str, timeout: float = 600) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            j = self._jobs.get(jid)
            if j and j["status"] in ("done", "error"):
                return self.get(jid)
            time.sleep(0.1)
        raise TimeoutError(jid)


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\-. ]+", "", name, flags=re.UNICODE).strip().replace(" ", "_")
    return s[:60] or "project"


def file_fingerprint(path: Path) -> str:
    """size + sha256 of first and last MiB: cheap but changes on any re-render."""
    p = Path(path)
    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return h.hexdigest()[:32]


def meta_fingerprint(meta: dict | None) -> str:
    m = {k: v for k, v in (meta or {}).items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(m, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- app
class EditMaker:
    def __init__(self, data_dir: Path, *, publishers: Optional[dict] = None):
        self.settings = Settings(data_dir)
        self.db = DB(Path(data_dir) / "editmaker.db")
        self.jobs = Jobs()
        cfg = self.settings.get()
        music.ensure_library(Path(cfg["music_library"]))
        port = cfg["server"]["port"]
        base = f"http://127.0.0.1:{port}"
        self.publishers = publishers or {
            "youtube": YouTubePublisher(self.settings.secrets_dir, f"{base}/api/auth/youtube/callback"),
            "tiktok": TikTokPublisher(self.settings.secrets_dir,
                                      cfg.get("tiktok_redirect_uri") or f"{base}/api/auth/tiktok/callback"),
        }
        self._oauth_states: dict[str, str] = {}

    # ---------------------------------------------------------------- environment
    def environment(self) -> dict:
        from . import ffmpeg
        return {**ffmpeg.available(), "nvenc": ffmpeg.nvenc_usable(), "gpu": ffmpeg.gpu_name(),
                "vibes": VIBES, "resolutions": list(RESOLUTIONS), "styles": {k: v["name"] for k, v in planner.STYLES.items()}}

    # ---------------------------------------------------------------- sources
    def scan_sources(self, folders: Optional[list[str]] = None, analyze: bool = True) -> str:
        cfg = self.settings.get()
        if folders is not None:
            cfg = self.settings.update({"source_folders": [str(Path(f).expanduser()) for f in folders]})
        folders = cfg["source_folders"]

        def work(report):
            files = list(media.iter_videos(folders))
            done, errors = 0, []
            for i, p in enumerate(files):
                report(i / max(1, len(files)), p.name)
                try:
                    info = media.index_file(p)
                    existing = self.db.one("SELECT size, mtime, analysis FROM sources WHERE path=?", (info["path"],))
                    fresh = existing and existing["analysis"] and existing["size"] == info["size"] \
                        and abs(existing["mtime"] - info["mtime"]) < 1e-3
                    an = None
                    if analyze and not fresh and info["has_video"]:
                        an = media.analyze_visual(p)
                    self.db.upsert_source(info, an)
                    done += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{p.name}: {e}")
            return {"files": len(files), "indexed": done, "errors": errors}

        return self.jobs.submit("scan_sources", "Сканирование исходников", work)

    def sources(self) -> list[dict]:
        return self.db.sources()

    # ---------------------------------------------------------------- music
    def scan_music(self) -> str:
        root = Path(self.settings.get()["music_library"])

        def work(report):
            return music.scan_library(self.db, root, progress=lambda p: report(p))

        return self.jobs.submit("scan_music", "Анализ музыки", work)

    def tracks(self, vibe: Optional[str] = None) -> list[dict]:
        return self.db.tracks(vibe)

    # ---------------------------------------------------------------- projects
    def create_project(self, payload: dict) -> dict:
        cfg = self.settings.get()
        source_ids = [int(x) for x in payload.get("source_ids") or []]
        if not source_ids:
            raise BadRequest("Выберите хотя бы один исходник")
        if not self.db.sources(source_ids):
            raise BadRequest("Исходники не найдены, пересканируйте папки")
        render_cfg = {**cfg["render"], **(payload.get("render") or {})}
        if render_cfg["resolution"] not in RESOLUTIONS:
            raise BadRequest(f"Неизвестное разрешение {render_cfg['resolution']}")
        vibe = payload.get("vibe") or cfg["vibe"]
        if vibe != "auto" and vibe not in VIBES:
            raise BadRequest(f"Неизвестный вайб {vibe}")
        wm = watermark.normalize({**cfg["watermark"], **(payload.get("watermark") or {})})
        if wm["type"] == "image" and wm["image_path"] and not Path(wm["image_path"]).is_file():
            raise BadRequest("PNG водяного знака не найден: " + wm["image_path"])
        output_root = Path(payload.get("output_folder") or cfg["output_folder"]).expanduser()
        output_root.mkdir(parents=True, exist_ok=True)
        name = (payload.get("name") or "").strip() or self.db.sources(source_ids[:1])[0]["title"] or "edit"
        slug = slugify(name)
        out_dir = output_root / slug
        n = 2
        while out_dir.exists() and any(out_dir.iterdir()):
            out_dir = output_root / f"{slug}-{n}"
            n += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "vibe": vibe,
            "track": payload.get("track") or "auto",   # auto | none | <path>
            "render": render_cfg,
            "watermark": {k: v for k, v in wm.items() if k != "usable"},
            "metadata": {**cfg["metadata"], **(payload.get("metadata") or {})},
        }
        pid = self.db.create_project(name, out_dir.name, str(out_dir), settings, source_ids)
        self.settings.update({"output_folder": str(output_root), "vibe": vibe if vibe != "auto" else cfg["vibe"],
                              "watermark": settings["watermark"]})
        count = int(payload.get("variants") or render_cfg.get("variants") or 4)
        jobs = self.add_variants(pid, count)
        return {"project": self.project(pid), "jobs": jobs}

    def projects(self) -> list[dict]:
        return self.db.projects()

    def project(self, pid: int) -> dict:
        p = self.db.project(pid)
        if not p:
            raise NotFound(f"project {pid}")
        p["variants"] = [self._variant_view(v) for v in self.db.variants(pid)]
        return p

    def _variant_view(self, v: dict) -> dict:
        v = dict(v)
        v["publications"] = self.db.publications(v["id"])
        v["style_name"] = planner.STYLES.get(v["style"], {}).get("name", v["style"])
        v["approval_valid"] = self._approval_valid(v) if v["status"] in ("approved", "published") else False
        if isinstance(v.get("plan"), dict):
            v["plan_summary"] = planner.describe(v["plan"])
        return v

    def add_variants(self, pid: int, count: int = 4) -> list[str]:
        p = self.db.project(pid)
        if not p:
            raise NotFound(f"project {pid}")
        count = max(1, min(12, int(count)))
        jobs = []
        start = self.db.next_variant_idx(pid)
        for i in range(count):
            idx = start + i
            style = planner.STYLE_ORDER[(idx - 1) % len(planner.STYLE_ORDER)]
            seed = random.randint(1, 10**9) if idx > len(planner.STYLE_ORDER) else idx
            vid = self.db.add_variant(pid, idx, style, seed, p["settings"]["render"], p["settings"]["watermark"])
            self.db.log(vid, "created", {"style": style, "seed": seed})
            jobs.append(self.render_variant(vid))
        return jobs

    # ---------------------------------------------------------------- rendering
    def _sources_for(self, p: dict) -> list[planner.SourceInfo]:
        rows = self.db.sources(p["source_ids"], with_analysis=True)
        out = []
        for r in rows:
            if not Path(r["path"]).is_file():
                continue
            an = r.get("analysis")
            if an is None:
                try:
                    an = media.analyze_visual(Path(r["path"]))
                    self.db.update("sources", r["id"], analysis=json.dumps(an))
                except Exception:  # noqa: BLE001 - render can still proceed without analysis
                    an = None
            out.append(planner.SourceInfo(r["id"], r["path"], r["duration"] or 0, bool(r["has_audio"]),
                                          r["title"] or "", an))
        if not out:
            raise BadRequest("Файлы исходников недоступны (диск отключён?)")
        return out

    def _resolve_track(self, p: dict, srcs: list[planner.SourceInfo], vibe: str, seed: int, length: float):
        choice = p["settings"].get("track") or "auto"
        if choice == "none":
            return None, "Выбран звук исходника"
        if choice != "auto":
            t = self.db.track_by_path(choice)
            if not t and Path(choice).is_file():
                from . import audio
                st = Path(choice).stat()
                self.db.upsert_track(str(Path(choice).resolve()), "custom", Path(choice).stem, st.st_size, st.st_mtime,
                                     audio.analyze_track(Path(choice)))
                t = self.db.track_by_path(str(Path(choice).resolve()))
            if t:
                return t, None
            return None, f"Трек не найден: {choice}; использован звук исходника"
        t = music.pick_track(self.db, vibe, [s.analysis for s in srcs if s.analysis], length, seed)
        if not t:
            return None, ("В music_library/ нет проанализированных треков — использован звук исходника "
                          "(нормализация до -14 LUFS). Добавьте треки в папки вайбов и нажмите «Сканировать музыку».")
        return t, None

    def render_variant(self, vid: int) -> str:
        v = self.db.variant(vid)
        if not v:
            raise NotFound(f"variant {vid}")
        p = self.db.project(v["project_id"])
        self.db.set_variant(vid, status="draft", error=None)

        def work(report):
            report(0.01, "план монтажа")
            srcs = self._sources_for(p)
            vibe = p["settings"]["vibe"]
            if vibe == "auto":
                vibe = music.suggest_vibe([s.analysis for s in srcs if s.analysis])
            r = p["settings"]["render"]
            wm = watermark.normalize(p["settings"]["watermark"])
            track, note = self._resolve_track(p, srcs, vibe, v["seed"], float(r["duration"]))
            plan = planner.build_plan(srcs, v["style"], v["seed"], float(r["duration"]), track,
                                      outro=wm["outro"], outro_duration=wm["outro_duration"])
            plan["vibe"] = vibe
            plan["note"] = note
            self.db.set_variant(vid, plan=plan)
            out = Path(p["output_dir"]) / f"variant_{v['idx']:02d}.mp4"
            res = render.render(plan, out, resolution=r["resolution"], fps=int(r["fps"]), vibe=vibe, wm=wm,
                                encoder=r.get("encoder", "auto"), quality=r.get("quality", "final"),
                                loudness=float(r.get("loudness_lufs", -14.0)),
                                progress=lambda f: report(0.02 + 0.96 * f, "рендер"))
            fp = file_fingerprint(out)
            meta = v.get("metadata") or self._generate_meta(p, v, plan, vibe)
            self.db.set_variant(vid, status="rendered", file_path=res["file"], thumb_path=res["thumb"],
                                fingerprint=fp, watermark=res["watermark"], metadata=meta,
                                render={**r, "encoder_used": res["encoder"], "probe": res["probe"]},
                                approved_at=None, approved_fingerprint=None, error=None)
            self.db.log(vid, "rendered", {"file": res["file"], "fingerprint": fp})
            self.write_meta_json(p["id"])
            return {"variant_id": vid, "file": res["file"]}

        def guarded(report):
            try:
                return work(report)
            except Exception as e:
                self.db.set_variant(vid, status="failed", error=str(e)[-1500:])
                self.db.log(vid, "render_failed", str(e)[-500:])
                self.write_meta_json(p["id"])
                raise

        return self.jobs.submit("render", f"{p['name']} · вариант {v['idx']:02d}", guarded,
                                ref={"project_id": p["id"], "variant_id": vid}, pool="render")

    def _generate_meta(self, p: dict, v: dict, plan: dict, vibe: str) -> dict:
        rows = self.db.sources(p["source_ids"])
        used = {s["source_id"] for s in plan["segments"]}
        rows = [r for r in rows if r["id"] in used] or rows
        m = p["settings"]["metadata"]
        return metadata.generate(
            titles=[r["title"] for r in rows], scenes=[r["scene"] for r in rows], paths=[r["path"] for r in rows],
            vibe=vibe, style=v["style"], language=m.get("language", "ru"), disclaimer=m.get("disclaimer", ""),
            channel_tags=m.get("channel_tags", []), youtube_privacy=m.get("youtube_privacy", "private"),
            tiktok_privacy=m.get("tiktok_privacy", "SELF_ONLY"), tiktok_mode=m.get("tiktok_mode", "draft"),
            music_title=(plan.get("music") or {}).get("title"), seed=v["seed"])

    def write_meta_json(self, pid: int) -> Path:
        p = self.db.project(pid)
        variants = []
        for v in self.db.variants(pid):
            variants.append({
                "idx": v["idx"], "file": Path(v["file_path"]).name if v["file_path"] else None,
                "style": v["style"], "style_name": planner.STYLES.get(v["style"], {}).get("name"),
                "status": v["status"], "seed": v["seed"], "fingerprint": v["fingerprint"],
                "approved_at": v["approved_at"], "error": v["error"],
                "watermark": v["watermark"], "metadata": v["metadata"],
                "plan": None if not isinstance(v["plan"], dict) else {
                    k: v["plan"].get(k) for k in ("style_name", "bpm", "music", "main_duration", "total_duration",
                                                  "cuts", "avg_shot", "boundaries", "outro", "outro_duration", "vibe", "note")},
                "render": v["render"],
                "publications": [{k: pub[k] for k in ("platform", "status", "remote_id", "url", "created_at")}
                                 for pub in self.db.publications(v["id"])],
            })
        doc = {"project": p["name"], "created_at": p["created_at"], "settings": p["settings"],
               "sources": [{"path": s["path"], "title": s["title"], "scene": s["scene"]} for s in self.db.sources(p["source_ids"])],
               "variants": variants}
        path = Path(p["output_dir"]) / "meta.json"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ---------------------------------------------------------------- metadata editing
    def update_metadata(self, vid: int, meta: dict) -> dict:
        v = self._get_variant(vid)
        cur = v.get("metadata") or {}
        new = {**cur}
        for plat in PLATFORMS:
            if plat in meta and isinstance(meta[plat], dict):
                new[plat] = {**cur.get(plat, {}), **meta[plat]}
        for key in ("disclaimer", "platforms"):
            if key in meta:
                new[key] = meta[key]
        if "platforms" in new:
            new["platforms"] = [x for x in new["platforms"] if x in PLATFORMS]
        changed = meta_fingerprint(new) != meta_fingerprint(cur)
        fields = {"metadata": new}
        if changed and v["status"] == "approved":
            # approval covered the old text — the user must approve again
            fields.update(status="rendered", approved_at=None, approved_fingerprint=None)
            self.db.log(vid, "approval_revoked", "metadata changed")
        self.db.set_variant(vid, **fields)
        self.db.log(vid, "metadata_updated", None)
        self.write_meta_json(v["project_id"])
        return self._variant_view(self.db.variant(vid))

    def regenerate_metadata(self, vid: int) -> dict:
        v = self._get_variant(vid)
        if not isinstance(v.get("plan"), dict):
            raise BadRequest("Вариант ещё не отрендерен")
        p = self.db.project(v["project_id"])
        v2 = {**v, "seed": random.randint(1, 10**9)}
        meta = self._generate_meta(p, v2, v["plan"], v["plan"].get("vibe", p["settings"]["vibe"]))
        old = v.get("metadata") or {}
        meta["platforms"] = old.get("platforms", meta["platforms"])
        return self.update_metadata(vid, meta)

    # ---------------------------------------------------------------- approval gate
    def _get_variant(self, vid: int) -> dict:
        v = self.db.variant(vid)
        if not v:
            raise NotFound(f"variant {vid}")
        return v

    def _approval_fp(self, v: dict) -> str:
        return f"{v['fingerprint']}:{meta_fingerprint(v.get('metadata'))}"

    def _approval_valid(self, v: dict) -> bool:
        if not v.get("approved_fingerprint") or not v.get("file_path") or not Path(v["file_path"]).is_file():
            return False
        try:
            current = f"{file_fingerprint(Path(v['file_path']))}:{meta_fingerprint(v.get('metadata'))}"
        except OSError:
            return False
        return current == v["approved_fingerprint"]

    def approve(self, vid: int) -> dict:
        v = self._get_variant(vid)
        if v["status"] not in ("rendered", "approved"):
            raise BadRequest(f"Одобрить можно только отрендеренный вариант (сейчас: {v['status']})")
        if not v["file_path"] or not Path(v["file_path"]).is_file():
            raise BadRequest("Файл варианта не найден")
        fp = file_fingerprint(Path(v["file_path"]))
        if fp != v["fingerprint"]:
            raise BadRequest("Файл изменён после рендера — перерендерите вариант")
        self.db.set_variant(vid, status="approved", approved_at=time.time(), approved_fingerprint=self._approval_fp(v))
        self.db.log(vid, "approved", {"fingerprint": fp})
        self.write_meta_json(v["project_id"])
        return self._variant_view(self.db.variant(vid))

    def unapprove(self, vid: int) -> dict:
        v = self._get_variant(vid)
        if v["status"] == "approved":
            self.db.set_variant(vid, status="rendered", approved_at=None, approved_fingerprint=None)
            self.db.log(vid, "unapproved", None)
            self.write_meta_json(v["project_id"])
        return self._variant_view(self.db.variant(vid))

    def ensure_publishable(self, v: dict) -> None:
        """The single gate every publish path goes through."""
        if v["status"] not in ("approved", "published"):
            raise ApprovalRequired(f"Вариант {v['idx']:02d} не одобрен (статус: {v['status']}). Сначала нажмите «Одобрить».")
        if not self._approval_valid(v):
            raise ApprovalRequired("Файл или метаданные изменились после одобрения — одобрите вариант заново.")

    # ---------------------------------------------------------------- publishing
    def publish(self, vid: int, platforms: Optional[list[str]] = None) -> str:
        v = self._get_variant(vid)
        self.ensure_publishable(v)  # raise synchronously so the UI gets a 409, not a failed job
        meta = v.get("metadata") or {}
        platforms = [x for x in (platforms or meta.get("platforms") or []) if x in PLATFORMS]
        if not platforms:
            raise BadRequest("Не выбрана ни одна платформа")
        for plat in platforms:
            st = self.publishers[plat].status()
            if not st.get("authorized"):
                raise NotConfigured(f"{plat}: нет авторизации", st.get("instructions", ""))

        def work(report):
            fresh = self._get_variant(vid)
            self.ensure_publishable(fresh)  # re-check at execution time
            results = {}
            for i, plat in enumerate(platforms):
                req = self._request_for(fresh, plat)
                base = i / len(platforms)
                try:
                    res = self.publishers[plat].publish(
                        req, progress=lambda f, b=base: report(b + f / len(platforms), f"загрузка {plat}"))
                    self.db.add_publication(vid, plat, res.status, res.remote_id, res.url, res.raw)
                    self.db.log(vid, "published", {"platform": plat, "id": res.remote_id, "status": res.status})
                    results[plat] = {"status": res.status, "url": res.url, "id": res.remote_id}
                except (PublishError, OSError) as e:
                    self.db.add_publication(vid, plat, "error", error=str(e)[:1000])
                    self.db.log(vid, "publish_failed", {"platform": plat, "error": str(e)[:500]})
                    results[plat] = {"status": "error", "error": str(e)}
            if any(r["status"] != "error" for r in results.values()):
                self.db.set_variant(vid, status="published")
            self.write_meta_json(fresh["project_id"])
            return results

        return self.jobs.submit("publish", f"Публикация варианта {v['idx']:02d}", work,
                                ref={"project_id": v["project_id"], "variant_id": vid})

    def publish_approved(self, pid: int, platforms: Optional[list[str]] = None) -> list[str]:
        approved = [v for v in self.db.variants(pid) if v["status"] == "approved"]
        if not approved:
            raise ApprovalRequired("В проекте нет одобренных вариантов")
        return [self.publish(v["id"], platforms) for v in approved]

    def _request_for(self, v: dict, plat: str) -> PublishRequest:
        meta = v.get("metadata") or {}
        m = meta.get(plat, {})
        if plat == "youtube":
            return PublishRequest(Path(v["file_path"]), m.get("title", ""), m.get("description", ""),
                                  list(m.get("tags") or []), m.get("privacy", "private"),
                                  {"category_id": "24", "made_for_kids": False})
        return PublishRequest(Path(v["file_path"]), m.get("title", ""), m.get("description", ""),
                              list(m.get("hashtags") or []), m.get("privacy", "SELF_ONLY"),
                              {"mode": m.get("mode", "draft")})

    # ---------------------------------------------------------------- oauth
    def auth_status(self) -> dict:
        return {k: p.status() for k, p in self.publishers.items()}

    def auth_start(self, platform: str) -> str:
        state = uuid.uuid4().hex
        self._oauth_states[state] = platform
        return self.publishers[platform].auth_url(state)

    def auth_finish(self, platform: str, code: str, state: Optional[str]) -> None:
        if state is not None and self._oauth_states.pop(state, None) != platform:
            raise BadRequest("Неверный state OAuth — начните авторизацию заново")
        self.publishers[platform].exchange_code(code)

    # ---------------------------------------------------------------- export to NLE / CapCut
    EXPORT_FORMATS = ("fcpxml", "edl", "capcut")

    def export_variant(self, vid: int, fmt: str) -> str:
        if fmt not in self.EXPORT_FORMATS:
            raise BadRequest(f"Формат экспорта: {', '.join(self.EXPORT_FORMATS)}")
        v = self._get_variant(vid)
        if not isinstance(v.get("plan"), dict):
            raise BadRequest("Сначала дождитесь рендера варианта")
        p = self.db.project(v["project_id"])
        r = p["settings"]["render"]
        out = Path(p["output_dir"]) / "export" / f"variant_{v['idx']:02d}"
        name = f"{p['slug']}_v{v['idx']:02d}"

        def work(report):
            res = export_nle.export(v["plan"], out, fmt, resolution=r["resolution"], fps=int(r["fps"]),
                                    vibe=v["plan"].get("vibe", "custom"), wm=p["settings"]["watermark"], name=name,
                                    progress=lambda f: report(f, "экспорт"))
            self.db.log(vid, "exported", res)
            return res

        title = {"fcpxml": "DaVinci / Final Cut", "edl": "EDL", "capcut": "пакет CapCut"}[fmt]
        return self.jobs.submit("export", f"Экспорт {title} · вариант {v['idx']:02d}", work,
                                ref={"project_id": p["id"], "variant_id": vid},
                                pool="render" if fmt == "capcut" else "io")

    def source_thumb(self, sid: int) -> Path:
        rows = self.db.sources([sid])
        if not rows:
            raise NotFound(f"source {sid}")
        src = rows[0]
        thumbs = self.settings.data_dir / "thumbs"
        thumbs.mkdir(exist_ok=True)
        out = thumbs / f"{sid}_{int(src['mtime'] or 0)}.jpg"
        if not out.is_file():
            t = max(0.0, (src["duration"] or 0) * 0.25)
            ff.run(["-ss", f"{t:.2f}", "-i", src["path"], "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(out)])
        return out

    # ---------------------------------------------------------------- files
    def open_in_explorer(self, path: str) -> None:
        import os
        import subprocess
        import sys
        p = Path(path)
        target = p if p.is_dir() else p.parent
        if not target.is_dir() or not (self.allowed_media(target) or self.allowed_media(p)):
            raise BadRequest("Папка недоступна")
        if sys.platform.startswith("win"):
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(target))  # noqa: S606 - local desktop app
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def allowed_media(self, path: Path) -> bool:
        path = Path(path).resolve()
        wm_img = self.settings.get()["watermark"].get("image_path")
        if wm_img and path == Path(wm_img).resolve() and path.suffix.lower() == ".png":
            return True  # live preview of the PNG watermark
        roots = [Path(p["output_dir"]).resolve() for p in self.db.projects()]
        roots += [Path(f).resolve() for f in self.settings.get()["source_folders"]]
        return any(path == r or r in path.parents for r in roots)
