"""TikTok Content Posting API v2 publisher."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path

from ._http import json_or_empty, request
from .base import NotConfigured, ProgressFn, PublishError, PublishRequest, PublishResult

MB = 1024 * 1024
MIN_CHUNK = 5 * MB
MAX_CHUNK = 64 * MB
SCOPES = "user.info.basic,video.upload,video.publish"
JSON_CT = "application/json; charset=UTF-8"


def plan_chunks(size: int, chunk_size: int) -> list[tuple[int, int]]:
    """Return inclusive (start, end) byte ranges following TikTok chunking rules."""
    if size <= 0:
        raise ValueError("size must be positive")
    if size < MIN_CHUNK:
        return [(0, size - 1)]
    cs = max(MIN_CHUNK, min(MAX_CHUNK, chunk_size))
    count = max(size // cs, 1)
    ranges = []
    for i in range(count):
        start = i * cs
        end = size - 1 if i == count - 1 else start + cs - 1
        ranges.append((start, end))
    return ranges


class TikTokPublisher:
    def __init__(self, secrets_dir: Path, redirect_uri: str, *,
                 auth_base: str = "https://www.tiktok.com",
                 api_base: str = "https://open.tiktokapis.com",
                 chunk_size: int = 10 * MB,
                 poll_interval: float = 3.0, poll_timeout: float = 300.0):
        self.secrets_dir = Path(secrets_dir)
        self.redirect_uri = redirect_uri
        self.auth_base = auth_base.rstrip("/")
        self.api_base = api_base.rstrip("/")
        self.chunk_size = chunk_size
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    @property
    def app_file(self) -> Path:
        return self.secrets_dir / "tiktok_app.json"

    @property
    def token_file(self) -> Path:
        return self.secrets_dir / "tiktok_token.json"

    def _instructions(self) -> str:
        return (
            "Подключение TikTok:\n"
            "1. Откройте developers.tiktok.com и создайте приложение (Manage apps → Connect an app).\n"
            "2. Добавьте продукты Login Kit и Content Posting API.\n"
            "3. Включите scopes: video.upload, video.publish (и user.info.basic).\n"
            f"4. Зарегистрируйте Redirect URI: {self.redirect_uri}. TikTok требует https; для локального "
            "использования зарегистрируйте любой https-адрес, затем после авторизации вставьте "
            "полный URL редиректа (или значение code) в поле «Код TikTok» в интерфейсе.\n"
            f"5. Сохраните client_key и client_secret в {self.app_file} "
            '(JSON: {"client_key": "...", "client_secret": "..."}).\n'
            "6. Нажмите «Подключить TikTok» в интерфейсе.\n"
            "Важно: пока приложение не прошло аудит TikTok, прямая публикация разрешена только "
            "с приватностью SELF_ONLY и только для приватных аккаунтов."
        )

    def _app(self) -> tuple[str, str]:
        try:
            d = json.loads(self.app_file.read_text(encoding="utf-8"))
            return d["client_key"], d["client_secret"]
        except (OSError, ValueError, KeyError, TypeError):
            raise NotConfigured("TikTok: не найден tiktok_app.json", self._instructions())

    def _load_token(self) -> dict:
        try:
            tok = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tok = None
        if not isinstance(tok, dict) or not tok.get("access_token"):
            raise NotConfigured("TikTok: нет авторизации (нажмите «Подключить TikTok»)",
                                self._instructions())
        return tok

    def _save_token(self, tok: dict) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok), encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def status(self) -> dict:
        configured = True
        try:
            self._app()
        except NotConfigured:
            configured = False
        authorized = False
        if configured:
            try:
                self._load_token()
                authorized = True
            except NotConfigured:
                pass
        return {"configured": configured, "authorized": authorized,
                "instructions": self._instructions(), "redirect_uri": self.redirect_uri}

    def auth_url(self, state: str) -> str:
        key, _ = self._app()
        q = urllib.parse.urlencode({
            "client_key": key, "scope": SCOPES, "response_type": "code",
            "redirect_uri": self.redirect_uri, "state": state}, safe=",")
        return f"{self.auth_base}/v2/auth/authorize/?{q}"

    def _token_request(self, form: dict) -> dict:
        status, _, body = request(
            "POST", f"{self.api_base}/v2/oauth/token/",
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Cache-Control": "no-cache"})
        js = json_or_empty(body)
        if status != 200 or "access_token" not in js:
            raise PublishError(
                f"TikTok OAuth: HTTP {status} {js.get('error_description') or js.get('error') or ''}".strip())
        return js

    def _store(self, js: dict, old: dict | None = None) -> dict:
        now = time.time()
        old = old or {}
        tok = {
            "access_token": js["access_token"],
            "refresh_token": js.get("refresh_token") or old.get("refresh_token", ""),
            "expires_at": now + float(js.get("expires_in", 86400)),
            "refresh_expires_at": now + float(js.get("refresh_expires_in", 31536000)),
            "open_id": js.get("open_id") or old.get("open_id", ""),
            "scope": js.get("scope") or old.get("scope", ""),
        }
        self._save_token(tok)
        return tok

    def exchange_code(self, code_or_url: str) -> None:
        key, secret = self._app()
        code = (code_or_url or "").strip()
        if "code=" in code or code.startswith("http"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
            if "code" not in qs:
                raise PublishError("TikTok: в URL нет параметра code")
            code = qs["code"][0]
        if not code:
            raise PublishError("TikTok: пустой код авторизации")
        js = self._token_request({
            "client_key": key, "client_secret": secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": self.redirect_uri})
        self._store(js)

    def _access_token(self) -> str:
        tok = self._load_token()
        if float(tok.get("expires_at", 0)) < time.time() + 60:
            key, secret = self._app()
            if not tok.get("refresh_token"):
                raise NotConfigured("TikTok: нет refresh_token, переподключите аккаунт",
                                    self._instructions())
            js = self._token_request({
                "client_key": key, "client_secret": secret,
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
            tok = self._store(js, tok)
        return tok["access_token"]

    # ---- publish ----
    @staticmethod
    def build_caption(title: str, description: str, tags: list[str]) -> str:
        text = (title or "").strip()
        if description and description.strip():
            text = f"{text}\n\n{description.strip()}" if text else description.strip()
        lower = text.lower()
        extra = []
        for t in tags or []:
            t = (t or "").strip().lstrip("#").replace(" ", "")
            if t and f"#{t}".lower() not in lower and f"#{t}" not in extra:
                extra.append(f"#{t}")
        if extra:
            text = (text + " " if text else "") + " ".join(extra)
        return text[:2200]

    def _api(self, path: str, body: dict, token: str) -> dict:
        status, _, raw = request(
            "POST", f"{self.api_base}{path}", json_body=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": JSON_CT})
        js = json_or_empty(raw)
        err = js.get("error") or {}
        if status >= 400 or err.get("code", "ok") != "ok":
            raise PublishError(f"{err.get('code', 'http_' + str(status))}: {err.get('message', '')}")
        return js

    def publish(self, req: PublishRequest, progress: ProgressFn = None) -> PublishResult:
        path = Path(req.video_path)
        if not path.is_file():
            raise PublishError(f"Файл не найден: {path}")
        opts = req.options or {}
        mode = opts.get("mode", "direct")
        if mode not in ("direct", "draft"):
            raise PublishError(f"Неизвестный режим TikTok: {mode!r} (direct|draft)")
        size = path.stat().st_size
        if size <= 0:
            raise PublishError("Пустой видеофайл")
        token = self._access_token()
        ranges = plan_chunks(size, self.chunk_size)
        chunk_len = ranges[0][1] - ranges[0][0] + 1 if len(ranges) > 1 else size
        source_info = {"source": "FILE_UPLOAD", "video_size": size,
                       "chunk_size": chunk_len, "total_chunk_count": len(ranges)}

        if mode == "direct":
            info = self._api("/v2/post/publish/creator_info/query/", {}, token)
            allowed = (info.get("data") or {}).get("privacy_level_options") or []
            if req.privacy not in allowed:
                raise PublishError(
                    f"Приватность {req.privacy!r} недоступна для этого аккаунта TikTok; "
                    f"допустимо: {', '.join(allowed) or 'нет'}")
            body = {
                "post_info": {
                    "title": self.build_caption(req.title, req.description, req.tags),
                    "privacy_level": req.privacy,
                    "disable_comment": bool(opts.get("disable_comment", False)),
                    "disable_duet": bool(opts.get("disable_duet", False)),
                    "disable_stitch": bool(opts.get("disable_stitch", False)),
                    "video_cover_timestamp_ms": int(opts.get("cover_ms", 1000)),
                },
                "source_info": source_info,
            }
            init = self._api("/v2/post/publish/video/init/", body, token)
        else:
            init = self._api("/v2/post/publish/inbox/video/init/",
                             {"source_info": source_info}, token)
        data = init.get("data") or {}
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("TikTok: init без publish_id/upload_url")

        with open(path, "rb") as f:
            for i, (a, b) in enumerate(ranges):
                f.seek(a)
                chunk = f.read(b - a + 1)
                status, _, rbody = request(
                    "PUT", upload_url, data=chunk,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                             "Content-Range": f"bytes {a}-{b}/{size}"})
                last = i == len(ranges) - 1
                if status not in ((200, 201) if last else (201,)):
                    raise PublishError(f"TikTok upload: HTTP {status} {rbody[:200].decode('utf-8', 'replace')}")
                if progress:
                    progress((b + 1) / size)

        return self._poll(publish_id, token)

    def _poll(self, publish_id: str, token: str) -> PublishResult:
        deadline = time.monotonic() + self.poll_timeout
        js: dict = {}
        while True:
            js = self._api("/v2/post/publish/status/fetch/", {"publish_id": publish_id}, token)
            d = js.get("data") or {}
            st = d.get("status")
            if st == "PUBLISH_COMPLETE":
                ids = d.get("publicaly_available_post_id") or []
                url = f"https://www.tiktok.com/@/video/{ids[0]}" if ids else None
                return PublishResult("tiktok", "published", publish_id, url, js)
            if st == "SEND_TO_USER_INBOX":
                return PublishResult("tiktok", "draft_sent", publish_id, None, js)
            if st == "FAILED":
                raise PublishError(f"TikTok: публикация не удалась: {d.get('fail_reason', 'unknown')}")
            if time.monotonic() >= deadline:
                return PublishResult("tiktok", "processing", publish_id, None, js)
            time.sleep(self.poll_interval)
