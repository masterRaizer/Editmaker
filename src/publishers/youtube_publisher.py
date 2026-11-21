"""YouTube Data API v3 publisher (OAuth2 + resumable upload)."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from ._http import json_or_empty, request
from .base import NotConfigured, ProgressFn, PublishError, PublishRequest, PublishResult

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
PRIVACY = ("private", "unlisted", "public")
MAX_RETRIES = 5


class YouTubePublisher:
    def __init__(self, secrets_dir: Path, redirect_uri: str, *,
                 auth_base: str = "https://accounts.google.com",
                 oauth_base: str = "https://oauth2.googleapis.com",
                 api_base: str = "https://www.googleapis.com",
                 chunk_size: int = 8 * 1024 * 1024):
        self.secrets_dir = Path(secrets_dir)
        self.redirect_uri = redirect_uri
        self.auth_base = auth_base.rstrip("/")
        self.oauth_base = oauth_base.rstrip("/")
        self.api_base = api_base.rstrip("/")
        self.chunk_size = chunk_size
        self.retry_sleep = 1.0

    # ---- files ----
    @property
    def client_file(self) -> Path:
        return self.secrets_dir / "youtube_client_secret.json"

    @property
    def token_file(self) -> Path:
        return self.secrets_dir / "youtube_token.json"

    def _instructions(self) -> str:
        return (
            "Подключение YouTube:\n"
            "1. Откройте Google Cloud Console (console.cloud.google.com) и создайте проект.\n"
            "2. APIs & Services → Library → включите «YouTube Data API v3».\n"
            "3. OAuth consent screen: заполните название, добавьте себя в Test users.\n"
            "4. Credentials → Create credentials → OAuth client ID, тип «Desktop app».\n"
            "5. Скачайте JSON и сохраните как "
            f"{self.client_file}\n"
            f"6. Если выбрали тип «Web application», добавьте Authorized redirect URI: {self.redirect_uri}\n"
            "7. Нажмите «Подключить YouTube» в интерфейсе и разрешите доступ."
        )

    def _client(self) -> tuple[str, str]:
        try:
            data = json.loads(self.client_file.read_text(encoding="utf-8"))
            sect = data.get("installed") or data.get("web") or {}
            cid, secret = sect["client_id"], sect["client_secret"]
        except (OSError, ValueError, KeyError, AttributeError):
            raise NotConfigured("YouTube: не найден youtube_client_secret.json",
                                self._instructions())
        return cid, secret

    def _load_token(self) -> dict:
        try:
            tok = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tok = None
        if not isinstance(tok, dict) or not tok.get("access_token"):
            raise NotConfigured("YouTube: нет авторизации (нажмите «Подключить YouTube»)",
                                self._instructions())
        return tok

    def _save_token(self, tok: dict) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps(tok), encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    # ---- public auth API ----
    def status(self) -> dict:
        configured = True
        try:
            self._client()
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
        cid, _ = self._client()
        q = urllib.parse.urlencode({
            "client_id": cid, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state,
        })
        return f"{self.auth_base}/o/oauth2/v2/auth?{q}"

    def _token_request(self, form: dict) -> dict:
        data = urllib.parse.urlencode(form).encode()
        status, _, body = request(
            "POST", f"{self.oauth_base}/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        js = json_or_empty(body)
        if status != 200 or "access_token" not in js:
            raise PublishError(f"YouTube OAuth: HTTP {status} {js.get('error_description') or js.get('error') or ''}".strip())
        return js

    def exchange_code(self, code: str) -> None:
        cid, secret = self._client()
        js = self._token_request({
            "code": code, "client_id": cid, "client_secret": secret,
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code"})
        self._save_token({
            "access_token": js["access_token"],
            "refresh_token": js.get("refresh_token", ""),
            "expires_at": time.time() + float(js.get("expires_in", 3600)),
            "scope": js.get("scope", SCOPE)})

    def _refresh(self, tok: dict) -> dict:
        cid, secret = self._client()
        if not tok.get("refresh_token"):
            raise NotConfigured("YouTube: нет refresh_token, переподключите аккаунт",
                                self._instructions())
        js = self._token_request({
            "client_id": cid, "client_secret": secret,
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        new = {
            "access_token": js["access_token"],
            "refresh_token": js.get("refresh_token") or tok["refresh_token"],
            "expires_at": time.time() + float(js.get("expires_in", 3600)),
            "scope": js.get("scope", tok.get("scope", SCOPE))}
        self._save_token(new)
        return new

    def _access_token(self, force_refresh: bool = False) -> str:
        tok = self._load_token()
        if force_refresh or float(tok.get("expires_at", 0)) < time.time() + 60:
            tok = self._refresh(tok)
        return tok["access_token"]

    # ---- publish ----
    @staticmethod
    def _clean_tags(tags: list[str]) -> list[str]:
        out, total = [], 0
        for t in tags:
            t = (t or "").strip().lstrip("#").strip()
            if not t:
                continue
            add = len(t) + (1 if out else 0)
            if total + add > 500:
                break
            out.append(t)
            total += add
        return out

    def publish(self, req: PublishRequest, progress: ProgressFn = None) -> PublishResult:
        path = Path(req.video_path)
        if not path.is_file():
            raise PublishError(f"Файл не найден: {path}")
        if req.privacy not in PRIVACY:
            raise PublishError(f"Недопустимая приватность YouTube: {req.privacy!r}; допустимо: {', '.join(PRIVACY)}")
        title = (req.title or "").replace("<", "").replace(">", "").strip()[:100]
        if not title:
            raise PublishError("Пустое название видео")
        desc = (req.description or "").replace("<", "").replace(">", "")[:5000]
        total = path.stat().st_size
        if total <= 0:
            raise PublishError("Пустой видеофайл")
        opts = req.options or {}
        meta = {
            "snippet": {"title": title, "description": desc,
                        "tags": self._clean_tags(req.tags),
                        "categoryId": str(opts.get("category_id", "24"))},
            "status": {"privacyStatus": req.privacy,
                       "selfDeclaredMadeForKids": bool(opts.get("made_for_kids", False))},
        }
        self._token = self._access_token()
        session_url = self._init_session(meta, total)
        js = self._upload(session_url, path, total, progress)
        vid = js.get("id")
        if not vid:
            raise PublishError("YouTube: ответ без id видео")
        return PublishResult(platform="youtube", status="published", remote_id=vid,
                             url=f"https://youtu.be/{vid}", raw=js)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _init_session(self, meta: dict, total: int) -> str:
        url = (f"{self.api_base}/upload/youtube/v3/videos"
               "?uploadType=resumable&part=snippet,status")
        refreshed = False
        for _ in range(2):
            headers = {**self._auth(), "Content-Type": "application/json; charset=UTF-8",
                       "X-Upload-Content-Type": "video/mp4",
                       "X-Upload-Content-Length": str(total)}
            status, hdrs, body = request("POST", url, headers=headers, json_body=meta)
            if status == 401 and not refreshed:
                refreshed = True
                self._token = self._access_token(force_refresh=True)
                continue
            if status != 200 or not hdrs.get("location"):
                raise PublishError(f"YouTube init: HTTP {status} {body[:300].decode('utf-8', 'replace')}")
            return hdrs["location"]
        raise PublishError("YouTube init: не удалось авторизоваться")

    def _query_offset(self, session_url: str, total: int) -> tuple[int, Optional[dict]]:
        status, hdrs, body = request(
            "PUT", session_url, data=b"",
            headers={**self._auth(), "Content-Range": f"bytes */{total}",
                     "Content-Length": "0"})
        if status in (200, 201):
            return total, json_or_empty(body)
        if status == 308:
            return _next_offset(hdrs.get("range")), None
        raise PublishError(f"YouTube: не удалось узнать статус загрузки, HTTP {status}")

    def _upload(self, session_url: str, path: Path, total: int, progress: ProgressFn) -> dict:
        offset = 0
        refreshed = False
        fails = 0
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                chunk = f.read(self.chunk_size)
                end = offset + len(chunk) - 1
                headers = {**self._auth(), "Content-Type": "video/mp4",
                           "Content-Length": str(len(chunk)),
                           "Content-Range": f"bytes {offset}-{end}/{total}"}
                try:
                    status, hdrs, body = request("PUT", session_url, headers=headers, data=chunk)
                except OSError:
                    status, hdrs, body = 599, {}, b""
                if status == 401 and not refreshed:
                    refreshed = True
                    self._token = self._access_token(force_refresh=True)
                    continue
                if status in (200, 201):
                    if progress:
                        progress(1.0)
                    return json_or_empty(body)
                if status == 308:
                    offset = _next_offset(hdrs.get("range"))
                    if progress:
                        progress(min(offset / total, 1.0))
                    continue
                if status >= 500:
                    fails += 1
                    if fails > MAX_RETRIES:
                        raise PublishError(f"YouTube upload: HTTP {status} после {MAX_RETRIES} повторов")
                    time.sleep(self.retry_sleep * (2 ** (fails - 1)))
                    try:
                        offset, done = self._query_offset(session_url, total)
                    except OSError:
                        continue
                    if done is not None:
                        if progress:
                            progress(1.0)
                        return done
                    continue
                raise PublishError(f"YouTube upload: HTTP {status} {body[:300].decode('utf-8', 'replace')}")


def _next_offset(range_header: Optional[str]) -> int:
    if not range_header:
        return 0
    try:
        return int(range_header.split("-")[-1]) + 1
    except ValueError:
        return 0
