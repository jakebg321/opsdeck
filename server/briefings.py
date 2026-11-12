"""Spoken agent briefings from an operator-configured directory.

Set OPSDECK_BRIEFINGS_DIR to a folder holding one JSON sidecar per audio
file: <id>.mp3 (or .m4a/.ogg) plus <id>.json {"id","title",...} and an
optional <id>.txt transcript. Without the setting the panel reports
configured:false and the phone shows its setup state instead.

The audio route requires the same Authorization header as every other
API route. The phone fetches the bytes with that header and plays them
through an object URL, so the bearer token never appears in a URL.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

BRIEFING_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
AUDIO_SUFFIXES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}
MAX_BRIEFINGS = 100


def briefings_dir() -> Path | None:
    raw = os.environ.get("OPSDECK_BRIEFINGS_DIR", "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _audio_path(base: Path, briefing_id: str) -> Path | None:
    for suffix in AUDIO_SUFFIXES:
        candidate = base / f"{briefing_id}{suffix}"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def make_briefings_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/briefings")
    def list_briefings(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(authorization)
        base = briefings_dir()
        if base is None or not base.is_dir():
            return {"configured": False, "briefings": []}
        try:
            metas = sorted(base.glob("*.json"), reverse=True)[:MAX_BRIEFINGS]
        except OSError:
            metas = []
        items: list[dict[str, Any]] = []
        for path in metas:
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            briefing_id = str(meta.get("id") or "")
            if not BRIEFING_ID_RE.fullmatch(briefing_id):
                continue
            if path.stem != briefing_id:
                continue
            try:
                resolved = path.resolve()
                base_resolved = base.resolve()
            except OSError:
                continue
            if resolved.parent != base_resolved:
                continue
            if _audio_path(base, briefing_id) is None:
                continue
            try:
                txt_resolved = (base_resolved / f"{briefing_id}.txt").resolve()
                if txt_resolved.parent == base_resolved and txt_resolved.is_file():
                    meta["text"] = txt_resolved.read_text(encoding="utf-8").strip()
                else:
                    meta["text"] = meta.get("text", "")
            except OSError:
                meta["text"] = meta.get("text", "")
            items.append(meta)
        return {"configured": True, "briefings": items}

    @router.get("/api/briefings/{briefing_id}/audio")
    def briefing_audio(briefing_id: str,
                       authorization: str | None = Header(default=None)) -> FileResponse:
        require_token(authorization)
        if not BRIEFING_ID_RE.fullmatch(briefing_id):
            raise HTTPException(status_code=400, detail="invalid briefing id")
        base = briefings_dir()
        if base is None or not base.is_dir():
            raise HTTPException(status_code=404, detail="briefings are not configured")
        try:
            base_resolved = base.resolve()
        except OSError:
            raise HTTPException(status_code=404, detail="briefing not found") from None
        audio: Path | None = None
        for suffix, media in AUDIO_SUFFIXES.items():
            candidate = base_resolved / f"{briefing_id}{suffix}"
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.parent == base_resolved and resolved.is_file():
                audio, media_type = resolved, media
                break
        if audio is None:
            raise HTTPException(status_code=404, detail="briefing not found")
        return FileResponse(audio, media_type=media_type)

    return router
