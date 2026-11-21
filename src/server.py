"""FastAPI app: JSON API + static single-page UI. Binds to 127.0.0.1 by default."""
from __future__ import annotations

import os
import platform
import string
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .core.config import ROOT
from .core.service import ApprovalRequired, BadRequest, EditMaker, NotFound
from .publishers.base import NotConfigured, PublishError

WEB = ROOT / "web"


def create_app(data_dir: Optional[Path] = None, core: Optional[EditMaker] = None) -> FastAPI:
    app = FastAPI(title="EditMaker", docs_url="/api/docs", openapi_url="/api/openapi.json")
    em = core or EditMaker(Path(data_dir or os.environ.get("EDITMAKER_DATA", ROOT / "data")))
    app.state.em = em

    @app.exception_handler(ApprovalRequired)
    async def _approval(_: Request, e: ApprovalRequired):
        return JSONResponse({"error": str(e), "code": "approval_required"}, status_code=409)

    @app.exception_handler(BadRequest)
    async def _bad(_: Request, e: BadRequest):
        return JSONResponse({"error": str(e), "code": "bad_request"}, status_code=400)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return JSONResponse({"error": f"не найдено: {e}", "code": "not_found"}, status_code=404)

    @app.exception_handler(NotConfigured)
    async def _nc(_: Request, e: NotConfigured):
        return JSONResponse({"error": str(e), "instructions": e.instructions, "code": "not_configured"}, status_code=412)

    @app.exception_handler(PublishError)
    async def _pe(_: Request, e: PublishError):
        return JSONResponse({"error": str(e), "code": "publish_error"}, status_code=502)

    # ------------------------------------------------------------ state / config
    @app.get("/api/state")
    def state():
        return {"config": em.settings.get(), "env": em.environment(), "auth": em.auth_status()}

    @app.put("/api/config")
    def put_config(patch: dict = Body(...)):
        patch.pop("server", None)  # host/port only via config file / CLI
        return em.settings.update(patch)

    # ------------------------------------------------------------ filesystem browser (folder / file pickers)
    @app.get("/api/fs/list")
    def fs_list(path: str = "", files: str = Query("", description="comma separated extensions to include")):
        exts = {"." + e.strip(".").lower() for e in files.split(",") if e.strip()}
        if not path:
            if platform.system() == "Windows":
                roots = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            else:
                roots = ["/", str(Path.home())] + [str(p) for base in ("/media", "/mnt", "/Volumes")
                                                     if Path(base).is_dir() for p in sorted(Path(base).iterdir())]
            return {"path": "", "parent": None, "dirs": [{"name": r, "path": r} for r in roots], "files": []}
        p = Path(path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, "Папка не существует")
        dirs, fl = [], []
        try:
            for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if c.name.startswith("."):
                    continue
                if c.is_dir():
                    dirs.append({"name": c.name, "path": str(c)})
                elif exts and c.suffix.lower() in exts:
                    fl.append({"name": c.name, "path": str(c), "size": c.stat().st_size})
        except PermissionError:
            raise HTTPException(403, "Нет доступа к папке")
        parent = str(p.parent) if p.parent != p else ""
        return {"path": str(p), "parent": parent, "dirs": dirs, "files": fl}

    # ------------------------------------------------------------ sources / music
    @app.post("/api/sources/scan")
    def scan_sources(body: dict = Body(default={})):
        return {"job": em.scan_sources(body.get("folders"), analyze=body.get("analyze", True))}

    @app.get("/api/sources")
    def sources():
        return em.sources()

    @app.post("/api/music/scan")
    def scan_music():
        return {"job": em.scan_music()}

    @app.get("/api/music")
    def tracks(vibe: Optional[str] = None):
        return em.tracks(vibe)

    # ------------------------------------------------------------ projects / variants
    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        return em.create_project(body)

    @app.get("/api/projects")
    def projects():
        return em.projects()

    @app.get("/api/projects/{pid}")
    def project(pid: int):
        return em.project(pid)

    @app.post("/api/projects/{pid}/variants")
    def more_variants(pid: int, body: dict = Body(default={})):
        return {"jobs": em.add_variants(pid, int(body.get("count", 2)))}

    @app.post("/api/projects/{pid}/publish-approved")
    def publish_approved(pid: int, body: dict = Body(default={})):
        return {"jobs": em.publish_approved(pid, body.get("platforms"))}

    @app.post("/api/variants/{vid}/render")
    def rerender(vid: int):
        return {"job": em.render_variant(vid)}

    @app.put("/api/variants/{vid}/metadata")
    def put_meta(vid: int, body: dict = Body(...)):
        return em.update_metadata(vid, body)

    @app.post("/api/variants/{vid}/metadata/regenerate")
    def regen_meta(vid: int):
        return em.regenerate_metadata(vid)

    @app.post("/api/variants/{vid}/approve")
    def approve(vid: int):
        return em.approve(vid)

    @app.post("/api/variants/{vid}/unapprove")
    def unapprove(vid: int):
        return em.unapprove(vid)

    @app.post("/api/variants/{vid}/publish")
    def publish(vid: int, body: dict = Body(default={})):
        return {"job": em.publish(vid, body.get("platforms"))}

    @app.post("/api/variants/{vid}/export")
    def export_variant(vid: int, body: dict = Body(...)):
        return {"job": em.export_variant(vid, body.get("format", ""))}

    @app.get("/api/sources/{sid}/thumb")
    def source_thumb(sid: int):
        try:
            return FileResponse(em.source_thumb(sid), headers={"Cache-Control": "max-age=86400"})
        except Exception:  # noqa: BLE001 - broken file → no thumbnail
            raise HTTPException(404, "нет миниатюры")

    @app.post("/api/open")
    def open_path(body: dict = Body(...)):
        em.open_in_explorer(body.get("path", ""))
        return {"ok": True}

    @app.post("/api/shutdown")
    def shutdown():
        """Browser mode: stop the local server from the UI."""
        import threading
        cb = getattr(app.state, "shutdown", None)
        if cb:
            threading.Timer(0.3, cb).start()
            return {"ok": True}
        raise HTTPException(400, "Выключение доступно только в настольном режиме")

    @app.get("/api/variants/{vid}/audit")
    def audit(vid: int):
        return em.db.audit(vid)

    # ------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def jobs(active: bool = False):
        return em.jobs.list(active_only=active)

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        j = em.jobs.get(jid)
        if not j:
            raise HTTPException(404, "job not found")
        return j

    # ------------------------------------------------------------ media (range requests handled by FileResponse)
    @app.get("/api/media")
    def media_file(path: str):
        p = Path(path)
        if not p.is_file() or not em.allowed_media(p):
            raise HTTPException(404, "Файл недоступен")
        return FileResponse(p)

    # ------------------------------------------------------------ oauth
    @app.get("/api/auth/status")
    def auth_status():
        return em.auth_status()

    @app.get("/api/auth/{platform_name}/start")
    def auth_start(platform_name: str):
        if platform_name not in em.publishers:
            raise HTTPException(404)
        return RedirectResponse(em.auth_start(platform_name))

    @app.get("/api/auth/{platform_name}/callback")
    def auth_callback(platform_name: str, code: str = "", state: Optional[str] = None, error: str = ""):
        if platform_name not in em.publishers:
            raise HTTPException(404)
        if error or not code:
            return HTMLResponse(f"<p>Авторизация не завершена: {error or 'нет кода'}.</p><p><a href='/'>Вернуться</a></p>",
                                status_code=400)
        em.auth_finish(platform_name, code, state)
        return RedirectResponse("/?auth=" + platform_name)

    @app.post("/api/auth/{platform_name}/code")
    def auth_code(platform_name: str, body: dict = Body(...)):
        """Manual fallback: paste the code or the whole redirected URL (TikTok needs https redirects)."""
        if platform_name not in em.publishers:
            raise HTTPException(404)
        em.publishers[platform_name].exchange_code(body.get("code", "").strip())
        return em.auth_status()[platform_name]

    # ------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    return app
