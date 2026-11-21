"""Common types for platform publishers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


class PublishError(Exception):
    """Upload failed (HTTP error, API error, file problem)."""


class NotConfigured(PublishError):
    """API keys or OAuth tokens are missing. `instructions` says what to do."""

    def __init__(self, message: str, instructions: str = ""):
        super().__init__(message)
        self.instructions = instructions


@dataclass
class PublishRequest:
    video_path: Path
    title: str
    description: str
    tags: list[str] = field(default_factory=list)
    # YouTube: private | unlisted | public
    # TikTok: SELF_ONLY | MUTUAL_FOLLOW_FRIENDS | FOLLOWER_OF_CREATOR | PUBLIC_TO_EVERYONE
    privacy: str = "private"
    # free-form per-platform options, e.g. {"mode": "draft"} for TikTok,
    # {"category_id": "24", "made_for_kids": False} for YouTube
    options: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    platform: str          # "youtube" | "tiktok"
    status: str            # "published" | "processing" | "draft_sent"
    remote_id: str
    url: Optional[str] = None
    raw: dict = field(default_factory=dict)


ProgressFn = Optional[Callable[[float], None]]  # fraction 0..1
