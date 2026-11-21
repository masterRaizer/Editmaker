import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.publishers.base import NotConfigured, PublishError, PublishRequest
from src.publishers.tiktok_publisher import MB, TikTokPublisher, plan_chunks
from src.publishers.youtube_publisher import YouTubePublisher


class Fake:
    """Shared state of the fake server."""

    def __init__(self):
        self.log = []                # (method, path, headers, body)
        self.yt_received = bytearray()
        self.yt_total = 0
        self.yt_fail_next_put = 0    # number of PUT data requests to answer with 503
        self.yt_init_body = None
        self.yt_seen_tokens = []
        self.tt_uploads = []
        self.tt_init_body = None
        self.tt_status_calls = 0
        self.tt_final_status = "PUBLISH_COMPLETE"
        self.tt_privacy_options = ["SELF_ONLY"]
        self.tt_inbox_init = None


@pytest.fixture
def fake():
    state = Fake()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _host(self):
            return f"http://127.0.0.1:{self.server.server_address[1]}"

        def do_POST(self):
            body = self._read()
            path = self.path
            state.log.append(("POST", path, dict(self.headers), body))
            if path == "/token":
                form = urllib.parse.parse_qs(body.decode())
                if form["grant_type"] == ["authorization_code"]:
                    assert form["code"] == ["the-code"]
                    return self._send(200, {"access_token": "at1", "refresh_token": "rt1",
                                            "expires_in": 3600, "scope": "s"})
                assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt1"]
                return self._send(200, {"access_token": "at2", "expires_in": 3600})
            if path.startswith("/upload/youtube/v3/videos"):
                state.yt_seen_tokens.append(self.headers.get("Authorization"))
                state.yt_init_body = json.loads(body)
                state.yt_total = int(self.headers["X-Upload-Content-Length"])
                return self._send(200, headers={"Location": self._host() + "/session/1"})
            if path == "/v2/oauth/token/":
                form = urllib.parse.parse_qs(body.decode())
                if form["grant_type"] == ["authorization_code"]:
                    assert form["code"] == ["tt-code"]
                    return self._send(200, {"access_token": "tta", "refresh_token": "ttr",
                                            "expires_in": 86400, "refresh_expires_in": 100000,
                                            "open_id": "oid", "scope": "video.upload"})
                return self._send(200, {"access_token": "tta2", "expires_in": 86400})
            if path.startswith("/v2/post/publish/"):
                assert self.headers["Authorization"].startswith("Bearer ")
                js = json.loads(body) if body else {}
                if path == "/v2/post/publish/creator_info/query/":
                    return self._send(200, {"data": {"privacy_level_options": state.tt_privacy_options},
                                            "error": {"code": "ok", "message": ""}})
                if path in ("/v2/post/publish/video/init/", "/v2/post/publish/inbox/video/init/"):
                    if path.endswith("/video/init/") and "inbox" not in path:
                        state.tt_init_body = js
                    else:
                        state.tt_inbox_init = js
                    return self._send(200, {"data": {"publish_id": "pub1",
                                                     "upload_url": self._host() + "/tt-upload"},
                                            "error": {"code": "ok", "message": ""}})
                if path == "/v2/post/publish/status/fetch/":
                    state.tt_status_calls += 1
                    assert js == {"publish_id": "pub1"}
                    if state.tt_status_calls < 2:
                        return self._send(200, {"data": {"status": "PROCESSING_UPLOAD"},
                                                "error": {"code": "ok", "message": ""}})
                    d = {"status": state.tt_final_status}
                    if state.tt_final_status == "PUBLISH_COMPLETE":
                        d["publicaly_available_post_id"] = [7001]
                    if state.tt_final_status == "FAILED":
                        d["fail_reason"] = "file_format_check_failed"
                    return self._send(200, {"data": d, "error": {"code": "ok", "message": ""}})
            self._send(404, {"error": "nope"})

        def do_PUT(self):
            body = self._read()
            state.log.append(("PUT", self.path, dict(self.headers), body))
            cr = self.headers.get("Content-Range", "")
            if self.path == "/session/1":
                state.yt_seen_tokens.append(self.headers.get("Authorization"))
                if cr.startswith("bytes */"):
                    n = len(state.yt_received)
                    h = {"Range": f"bytes=0-{n - 1}"} if n else {}
                    return self._send(308, headers=h)
                if state.yt_fail_next_put > 0:
                    state.yt_fail_next_put -= 1
                    return self._send(503, b"busy")
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                a, b, tot = map(int, m.groups())
                if a != len(state.yt_received):
                    return self._send(400, b"bad offset")
                state.yt_received += body
                if b + 1 == tot:
                    return self._send(200, {"id": "abc123"})
                return self._send(308, headers={"Range": f"bytes=0-{b}"})
            if self.path == "/tt-upload":
                state.tt_uploads.append((cr, body))
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", cr)
                a, b, tot = map(int, m.groups())
                return self._send(201 if b + 1 < tot else 201)
            self._send(404)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    state.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(os.urandom(200 * 1024 + 123))
    return p


def write_yt_client(d: Path, key="web"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "youtube_client_secret.json").write_text(
        json.dumps({key: {"client_id": "cid", "client_secret": "csec"}}))


def make_yt(fake, tmp_path):
    s = tmp_path / "secrets"
    write_yt_client(s, "installed")
    y = YouTubePublisher(s, "http://localhost/cb", auth_base=fake.base,
                         oauth_base=fake.base, api_base=fake.base)
    y.chunk_size = 64 * 1024
    y.retry_sleep = 0
    return y


# ---------------- YouTube ----------------

def test_yt_not_configured(tmp_path):
    y = YouTubePublisher(tmp_path, "http://x/cb")
    st = y.status()
    assert st["configured"] is False and st["authorized"] is False
    assert "youtube_client_secret.json" in st["instructions"]
    assert st["redirect_uri"] == "http://x/cb"
    with pytest.raises(NotConfigured) as e:
        y.auth_url("s")
    assert e.value.instructions
    with pytest.raises(NotConfigured):
        y._access_token()


def test_yt_status_and_auth_url(fake, tmp_path):
    y = make_yt(fake, tmp_path)
    assert y.status()["configured"] and not y.status()["authorized"]
    url = y.auth_url("st8")
    assert url.startswith(fake.base + "/o/oauth2/v2/auth?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/youtube.upload"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["response_type"] == ["code"] and q["state"] == ["st8"] and q["client_id"] == ["cid"]
    with pytest.raises(NotConfigured):
        y._access_token()  # configured but no token


def test_yt_exchange_and_refresh(fake, tmp_path):
    y = make_yt(fake, tmp_path)
    y.exchange_code("the-code")
    assert y.status()["authorized"]
    tok = json.loads(y.token_file.read_text())
    assert tok["access_token"] == "at1" and tok["refresh_token"] == "rt1"
    assert y._access_token() == "at1"
    tok["expires_at"] = time.time() - 10
    y.token_file.write_text(json.dumps(tok))
    assert y._access_token() == "at2"
    assert json.loads(y.token_file.read_text())["refresh_token"] == "rt1"


def test_yt_publish_chunked_with_retry(fake, tmp_path, video):
    y = make_yt(fake, tmp_path)
    y.exchange_code("the-code")
    fake.yt_fail_next_put = 1
    prog = []
    res = y.publish(PublishRequest(video, "T<i>tle", "d", ["#a", "", "b"], "unlisted",
                                   {"category_id": "22"}), prog.append)
    assert res.platform == "youtube" and res.status == "published"
    assert res.remote_id == "abc123" and res.url == "https://youtu.be/abc123"
    assert bytes(fake.yt_received) == video.read_bytes()
    assert fake.yt_init_body["snippet"]["title"] == "Title"
    assert fake.yt_init_body["snippet"]["tags"] == ["a", "b"]
    assert fake.yt_init_body["snippet"]["categoryId"] == "22"
    assert fake.yt_init_body["status"] == {"privacyStatus": "unlisted",
                                           "selfDeclaredMadeForKids": False}
    assert any(m == "PUT" and h.get("Content-Range", "").startswith("bytes */")
               for m, _, h, _ in fake.log)
    assert prog and prog[-1] == 1.0 and prog == sorted(prog)


def test_yt_retry_exhausted(fake, tmp_path, video):
    y = make_yt(fake, tmp_path)
    y.exchange_code("the-code")
    fake.yt_fail_next_put = 100
    with pytest.raises(PublishError):
        y.publish(PublishRequest(video, "t", "d"))


def test_yt_validation(fake, tmp_path, video):
    y = make_yt(fake, tmp_path)
    y.exchange_code("the-code")
    with pytest.raises(PublishError):
        y.publish(PublishRequest(video, "t", "d", privacy="PUBLIC_TO_EVERYONE"))
    with pytest.raises(PublishError):
        y.publish(PublishRequest(tmp_path / "missing.mp4", "t", "d"))


def test_yt_tag_trimming_and_limits(fake, tmp_path, video):
    y = make_yt(fake, tmp_path)
    y.exchange_code("the-code")
    tags = [f"#{'x' * 99}{i}" for i in range(10)]  # 100 chars each
    res = y.publish(PublishRequest(video, "t" * 150, "d" * 6000, tags, "private"))
    assert res.status == "published"
    sn = fake.yt_init_body["snippet"]
    assert len(sn["title"]) == 100 and len(sn["description"]) == 5000
    kept = sn["tags"]
    assert len(kept) == 4 and sum(map(len, kept)) + len(kept) - 1 <= 500
    assert all(not t.startswith("#") for t in kept)


# ---------------- TikTok ----------------

def make_tt(fake, tmp_path):
    s = tmp_path / "secrets"
    s.mkdir(exist_ok=True)
    (s / "tiktok_app.json").write_text(json.dumps({"client_key": "ck", "client_secret": "cs"}))
    t = TikTokPublisher(s, "https://example.com/cb", auth_base=fake.base,
                        api_base=fake.base, poll_interval=0, poll_timeout=5)
    return t


def test_plan_chunks():
    assert plan_chunks(1000, 10 * MB) == [(0, 999)]
    assert plan_chunks(5 * MB - 1, 10 * MB) == [(0, 5 * MB - 2)]
    # 25 MB / 10 MB -> 2 chunks, last takes remainder
    r = plan_chunks(25 * MB, 10 * MB)
    assert r == [(0, 10 * MB - 1), (10 * MB, 25 * MB - 1)]
    # chunk size clamped up to 5 MB
    r = plan_chunks(12 * MB, 1 * MB)
    assert r == [(0, 5 * MB - 1), (5 * MB, 12 * MB - 1)]
    # clamped down to 64 MB
    r = plan_chunks(200 * MB, 500 * MB)
    assert len(r) == 3 and r[0] == (0, 64 * MB - 1) and r[-1][1] == 200 * MB - 1
    # contiguous
    for rr in (plan_chunks(77 * MB + 5, 8 * MB),):
        assert rr[0][0] == 0
        assert all(rr[i][1] + 1 == rr[i + 1][0] for i in range(len(rr) - 1))


def test_tt_not_configured(tmp_path):
    t = TikTokPublisher(tmp_path, "https://x/cb")
    st = t.status()
    assert st["configured"] is False and st["authorized"] is False
    assert "SELF_ONLY" in st["instructions"] and st["redirect_uri"] == "https://x/cb"
    with pytest.raises(NotConfigured):
        t.auth_url("s")
    with pytest.raises(NotConfigured):
        t.exchange_code("c")


def test_tt_auth_and_exchange(fake, tmp_path):
    t = make_tt(fake, tmp_path)
    url = t.auth_url("zz")
    assert url.startswith(fake.base + "/v2/auth/authorize/?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["client_key"] == ["ck"] and q["state"] == ["zz"]
    assert q["scope"] == ["user.info.basic,video.upload,video.publish"]
    t.exchange_code("https://example.com/cb?code=tt-code&state=zz")
    tok = json.loads(t.token_file.read_text())
    assert tok["access_token"] == "tta" and tok["open_id"] == "oid"
    assert t.status()["authorized"]
    tok["expires_at"] = time.time() - 5
    t.token_file.write_text(json.dumps(tok))
    assert t._access_token() == "tta2"
    assert json.loads(t.token_file.read_text())["refresh_token"] == "ttr"
    t.exchange_code("tt-code")  # raw code


def test_tt_direct_publish(fake, tmp_path, video):
    t = make_tt(fake, tmp_path)
    t.exchange_code("tt-code")
    prog = []
    res = t.publish(PublishRequest(video, "Title", "Desc #keep", ["keep", "new"], "SELF_ONLY"),
                    prog.append)
    assert res.platform == "tiktok" and res.status == "published" and res.remote_id == "pub1"
    assert res.url.endswith("/video/7001")
    pi = fake.tt_init_body["post_info"]
    assert pi["title"] == "Title\n\nDesc #keep #new"
    assert pi["privacy_level"] == "SELF_ONLY" and pi["video_cover_timestamp_ms"] == 1000
    si = fake.tt_init_body["source_info"]
    size = video.stat().st_size
    assert si == {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size,
                  "total_chunk_count": 1}
    assert len(fake.tt_uploads) == 1
    assert fake.tt_uploads[0][0] == f"bytes 0-{size - 1}/{size}"
    assert fake.tt_uploads[0][1] == video.read_bytes()
    assert prog == [1.0]
    assert fake.tt_status_calls == 2


def test_tt_privacy_mismatch(fake, tmp_path, video):
    t = make_tt(fake, tmp_path)
    t.exchange_code("tt-code")
    with pytest.raises(PublishError) as e:
        t.publish(PublishRequest(video, "t", "d", privacy="PUBLIC_TO_EVERYONE"))
    assert "SELF_ONLY" in str(e.value)
    assert fake.tt_uploads == []


def test_tt_draft(fake, tmp_path, video):
    t = make_tt(fake, tmp_path)
    t.exchange_code("tt-code")
    fake.tt_final_status = "SEND_TO_USER_INBOX"
    res = t.publish(PublishRequest(video, "t", "d", options={"mode": "draft"}))
    assert res.status == "draft_sent" and res.remote_id == "pub1"
    assert fake.tt_init_body is None
    assert list(fake.tt_inbox_init) == ["source_info"]
    assert len(fake.tt_uploads) == 1


def test_tt_failed_and_timeout(fake, tmp_path, video):
    t = make_tt(fake, tmp_path)
    t.exchange_code("tt-code")
    fake.tt_final_status = "FAILED"
    with pytest.raises(PublishError, match="file_format_check_failed"):
        t.publish(PublishRequest(video, "t", "d", privacy="SELF_ONLY"))
    fake.tt_final_status = "PROCESSING_DOWNLOAD"
    fake.tt_status_calls = 0
    t.poll_timeout = 0.05
    res = t.publish(PublishRequest(video, "t", "d", privacy="SELF_ONLY"))
    assert res.status == "processing"
