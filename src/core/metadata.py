"""Title / description / disclaimer / tags generation per variant and platform (template based, offline)."""
from __future__ import annotations

import random
import re
import time

VIBE_WORDS = {
    "dexter_cold_dark": {"ru": ["холодный расчёт", "тёмный пассажир", "без эмоций", "ночная охота"],
                         "en": ["cold blooded", "dark passenger", "no emotions", "night hunt"],
                         "tags": ["dark edit", "cold edit", "dexter edit", "sigma edit"]},
    "dark_synth_tension": {"ru": ["на грани", "тёмный синт", "напряжение", "неон в темноте"],
                           "en": ["on the edge", "dark synth", "tension", "neon in the dark"],
                           "tags": ["dark synth", "synthwave edit", "dark edit"]},
    "phonk_aggressive": {"ru": ["без пощады", "фонк-режим", "удар за ударом", "в полную силу"],
                         "en": ["no mercy", "phonk mode", "hit after hit", "full power"],
                         "tags": ["phonk edit", "phonk", "badass edit", "aggressive edit"]},
    "moody_cinematic": {"ru": ["тишина перед бурей", "кинематограф", "одиночество", "последний кадр"],
                        "en": ["calm before the storm", "cinematic", "loneliness", "final frame"],
                        "tags": ["cinematic edit", "moody edit", "aesthetic edit"]},
    "custom": {"ru": ["эдит", "момент", "атмосфера"], "en": ["edit", "moment", "vibe"], "tags": ["edit"]},
}
STYLE_WORDS = {
    "cold": {"ru": "холодный монтаж", "en": "cold cut"},
    "drop": {"ru": "на бите", "en": "beat drop"},
    "flow": {"ru": "плавный монтаж", "en": "smooth flow"},
    "hook": {"ru": "быстрый монтаж", "en": "fast cut"},
}
ANIME_HINTS = re.compile(r"anime|аниме|subsplease|erai|horriblesubs|\[\w+\]|crunchyroll", re.IGNORECASE)


def _hashtag(s: str) -> str:
    return "#" + re.sub(r"[^\wА-Яа-яЁё]", "", s.replace(" ", "")).lower()


def _is_anime(paths: list[str]) -> bool:
    return any(ANIME_HINTS.search(p or "") for p in paths)


def generate(*, titles: list[str], scenes: list[str], paths: list[str], vibe: str, style: str, language: str,
             disclaimer: str, channel_tags: list[str], youtube_privacy: str, tiktok_privacy: str,
             tiktok_mode: str, music_title: str | None, seed: int) -> dict:
    rng = random.Random(seed)
    lang = language if language in ("ru", "en") else "ru"
    vw = VIBE_WORDS.get(vibe, VIBE_WORDS["custom"])
    main = next((t for t in titles if t), "Edit")
    others = [t for t in dict.fromkeys(titles) if t and t != main]
    hook = rng.choice(vw[lang])
    style_word = STYLE_WORDS.get(style, STYLE_WORDS["drop"])[lang]
    anime = _is_anime(paths)
    kind = ("аниме-эдит" if anime else "dark edit") if lang == "ru" else ("anime edit" if anime else "dark edit")

    yt_title = f"{main} — {hook} | {kind}"
    if len(yt_title) > 90:
        yt_title = yt_title[:90].rstrip()
    yt_title += " #shorts"

    scene_line = ", ".join(s for s in dict.fromkeys(scenes) if s)
    if lang == "ru":
        body = [f"{main}: {hook}. {style_word.capitalize()} под тёмный вайб."]
        if others:
            body.append("Также: " + ", ".join(others) + ".")
        if scene_line:
            body.append(f"Сцены: {scene_line}.")
        if music_title:
            body.append(f"Музыка: {music_title}.")
    else:
        body = [f"{main}: {hook}. {style_word.capitalize()} with a dark vibe."]
        if others:
            body.append("Also: " + ", ".join(others) + ".")
        if scene_line:
            body.append(f"Scenes: {scene_line}.")
        if music_title:
            body.append(f"Music: {music_title}.")

    base_tags = [main.lower(), f"{main.lower()} edit"] + [o.lower() for o in others[:3]] + vw["tags"]
    base_tags += ["anime edit", "anime"] if anime else ["series edit"]
    base_tags += ["edit", "shorts"] + [t for t in channel_tags if t]
    yt_tags = list(dict.fromkeys(t.strip() for t in base_tags if t.strip()))

    tt_tags = [_hashtag(main), _hashtag(main + "edit")] + [_hashtag(t) for t in vw["tags"][:2]]
    tt_tags += ["#animeedit", "#anime"] if anime else ["#darkedit"]
    tt_tags += ["#edit", "#fyp"] + [_hashtag(t) for t in channel_tags if t]
    tt_tags = list(dict.fromkeys(t for t in tt_tags if len(t) > 1))

    description = "\n\n".join(filter(None, [" ".join(body), disclaimer.strip()]))
    yt_desc = description + "\n\n" + " ".join(["#shorts"] + tt_tags[:3])
    tt_caption = f"{main} — {hook}"
    return {
        "youtube": {"title": yt_title, "description": yt_desc, "tags": yt_tags, "privacy": youtube_privacy},
        "tiktok": {"title": tt_caption, "description": description, "hashtags": tt_tags,
                   "privacy": tiktok_privacy, "mode": tiktok_mode},
        "disclaimer": disclaimer,
        "platforms": ["youtube", "tiktok"],
        "generated_at": time.time(),
    }
