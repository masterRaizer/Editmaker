"""Minimal urllib-based HTTP helper. Does not raise on HTTP error statuses."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional


def request(method: str, url: str, *, headers: Optional[dict] = None,
            json_body=None, data: Optional[bytes] = None,
            timeout: float = 60) -> tuple[int, dict, bytes]:
    hdrs = dict(headers or {})
    payload = data
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        if not any(k.lower() == "content-type" for k in hdrs):
            hdrs["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=payload, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body


def json_or_empty(body: bytes) -> dict:
    try:
        val = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return val if isinstance(val, dict) else {}
