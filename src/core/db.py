"""SQLite state store: sources, tracks, projects, variants, publications, audit log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  folder TEXT, title TEXT, scene TEXT,
  duration REAL, width INTEGER, height INTEGER, fps REAL,
  vcodec TEXT, acodec TEXT, has_audio INTEGER, bitrate INTEGER,
  size INTEGER, mtime REAL,
  analysis TEXT,
  indexed_at REAL
);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  vibe TEXT, title TEXT,
  duration REAL, bpm REAL, confidence REAL,
  analysis TEXT,
  size INTEGER, mtime REAL,
  analyzed_at REAL
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  output_dir TEXT NOT NULL,
  settings TEXT NOT NULL,
  source_ids TEXT NOT NULL,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS variants (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  style TEXT NOT NULL,
  seed INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  file_path TEXT, thumb_path TEXT,
  plan TEXT, metadata TEXT, watermark TEXT, render TEXT,
  fingerprint TEXT,
  approved_at REAL, approved_fingerprint TEXT,
  error TEXT,
  created_at REAL, updated_at REAL,
  UNIQUE(project_id, idx)
);
CREATE TABLE IF NOT EXISTS publications (
  id INTEGER PRIMARY KEY,
  variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  status TEXT NOT NULL,
  remote_id TEXT, url TEXT, response TEXT, error TEXT,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  variant_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT,
  ts REAL
);
"""

JSON_COLS = {"analysis", "settings", "source_ids", "plan", "metadata", "watermark", "render", "response"}


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    for k in list(d):
        if k in JSON_COLS and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def _enc(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- generic helpers
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        with self._lock:
            return _row(self.conn.execute(sql, args).fetchone())

    def exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(_enc(a) for a in args))
            self.conn.commit()
            return cur.lastrowid

    def update(self, table: str, row_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.exec(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))

    # ---- sources
    def upsert_source(self, info: dict, analysis: Optional[dict] = None) -> int:
        existing = self.one("SELECT id, size, mtime, analysis FROM sources WHERE path=?", (info["path"],))
        cols = ["folder", "title", "scene", "duration", "width", "height", "fps", "vcodec", "acodec",
                "has_audio", "bitrate", "size", "mtime"]
        vals = [info.get(c) for c in cols]
        vals[cols.index("has_audio")] = int(bool(info.get("has_audio")))
        now = time.time()
        if existing:
            unchanged = existing["size"] == info["size"] and abs((existing["mtime"] or 0) - info["mtime"]) < 1e-3
            keep = existing["analysis"] if unchanged and analysis is None else analysis
            self.exec(
                f"UPDATE sources SET {', '.join(c + '=?' for c in cols)}, analysis=?, indexed_at=? WHERE id=?",
                (*vals, _enc(keep), now, existing["id"]),
            )
            return existing["id"]
        return self.exec(
            f"INSERT INTO sources (path, {', '.join(cols)}, analysis, indexed_at) VALUES (?, {', '.join('?' * len(cols))}, ?, ?)",
            (info["path"], *vals, _enc(analysis), now),
        )

    def sources(self, ids: Optional[list[int]] = None, with_analysis: bool = False) -> list[dict]:
        cols = "*" if with_analysis else ("id, path, folder, title, scene, duration, width, height, fps, vcodec, acodec, "
                                          "has_audio, bitrate, size, mtime, indexed_at, analysis IS NOT NULL AS analyzed")
        if ids is not None:
            if not ids:
                return []
            ph = ",".join("?" * len(ids))
            return self.q(f"SELECT {cols} FROM sources WHERE id IN ({ph}) ORDER BY id", tuple(ids))
        return self.q(f"SELECT {cols} FROM sources ORDER BY folder, path")

    # ---- tracks
    def upsert_track(self, path: str, vibe: str, title: str, size: int, mtime: float, analysis: dict) -> int:
        existing = self.one("SELECT id FROM tracks WHERE path=?", (path,))
        args = (vibe, title, analysis.get("duration"), analysis.get("bpm"), analysis.get("confidence"),
                _enc(analysis), size, mtime, time.time())
        if existing:
            self.exec("UPDATE tracks SET vibe=?, title=?, duration=?, bpm=?, confidence=?, analysis=?, size=?, mtime=?, "
                      "analyzed_at=? WHERE id=?", (*args, existing["id"]))
            return existing["id"]
        return self.exec("INSERT INTO tracks (vibe, title, duration, bpm, confidence, analysis, size, mtime, analyzed_at, path) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (*args, path))

    def track_by_path(self, path: str) -> Optional[dict]:
        return self.one("SELECT * FROM tracks WHERE path=?", (path,))

    def tracks(self, vibe: Optional[str] = None) -> list[dict]:
        cols = "id, path, vibe, title, duration, bpm, confidence"
        if vibe:
            return self.q(f"SELECT {cols} FROM tracks WHERE vibe=? ORDER BY title", (vibe,))
        return self.q(f"SELECT {cols} FROM tracks ORDER BY vibe, title")

    # ---- projects / variants
    def create_project(self, name: str, slug: str, output_dir: str, settings: dict, source_ids: list[int]) -> int:
        return self.exec("INSERT INTO projects (name, slug, output_dir, settings, source_ids, created_at) VALUES (?,?,?,?,?,?)",
                         (name, slug, output_dir, settings, source_ids, time.time()))

    def project(self, pid: int) -> Optional[dict]:
        return self.one("SELECT * FROM projects WHERE id=?", (pid,))

    def projects(self) -> list[dict]:
        return self.q("SELECT p.*, (SELECT COUNT(*) FROM variants v WHERE v.project_id=p.id) AS variant_count "
                      "FROM projects p ORDER BY p.created_at DESC")

    def add_variant(self, project_id: int, idx: int, style: str, seed: int, render: dict, watermark: dict) -> int:
        now = time.time()
        return self.exec("INSERT INTO variants (project_id, idx, style, seed, status, render, watermark, created_at, updated_at) "
                         "VALUES (?,?,?,?, 'draft', ?,?,?,?)", (project_id, idx, style, seed, render, watermark, now, now))

    def variant(self, vid: int) -> Optional[dict]:
        return self.one("SELECT * FROM variants WHERE id=?", (vid,))

    def variants(self, project_id: int) -> list[dict]:
        return self.q("SELECT * FROM variants WHERE project_id=? ORDER BY idx", (project_id,))

    def set_variant(self, vid: int, **fields) -> None:
        fields["updated_at"] = time.time()
        self.update("variants", vid, **fields)

    def next_variant_idx(self, project_id: int) -> int:
        r = self.one("SELECT COALESCE(MAX(idx), 0) AS m FROM variants WHERE project_id=?", (project_id,))
        return int(r["m"]) + 1

    # ---- publications / audit
    def add_publication(self, variant_id: int, platform: str, status: str, remote_id: str = None, url: str = None,
                        response: dict = None, error: str = None) -> int:
        return self.exec("INSERT INTO publications (variant_id, platform, status, remote_id, url, response, error, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?)", (variant_id, platform, status, remote_id, url, response, error, time.time()))

    def publications(self, variant_id: int) -> list[dict]:
        return self.q("SELECT * FROM publications WHERE variant_id=? ORDER BY created_at", (variant_id,))

    def log(self, variant_id: Optional[int], action: str, detail: Any = None) -> None:
        self.exec("INSERT INTO audit (variant_id, action, detail, ts) VALUES (?,?,?,?)",
                  (variant_id, action, _enc(detail) if detail is not None else None, time.time()))

    def audit(self, variant_id: int) -> list[dict]:
        return self.q("SELECT * FROM audit WHERE variant_id=? ORDER BY ts", (variant_id,))
