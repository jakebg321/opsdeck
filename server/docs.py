"""Read-only Markdown library rooted at an operator-chosen directory."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

DOCS_SUFFIXES = {".md", ".markdown"}
DOCS_MAX_FILES = 500
DOCS_MAX_VISITED = 5000
DOCS_MAX_CHARS = 2 * 1024 * 1024


def docs_dir() -> Path | None:
    value = os.environ.get("OPSDECK_DOCS_DIR", "").strip()
    return Path(value).expanduser().resolve() if value else None


def _title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream.read(8192).splitlines():
                if line.startswith("# "):
                    return line[2:].strip()[:120]
    except OSError:
        pass
    return path.stem


def _candidate(base: Path, name: str) -> Path:
    parts = name.split("/")
    if (not name or name.startswith("/") or "\\" in name or "\x00" in name
            or any(not part or part in {".", ".."} or part.startswith(".") for part in parts)):
        raise HTTPException(status_code=400, detail="invalid doc path")
    path = (base / name).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(status_code=400, detail="path escapes docs directory")
    return path


def make_docs_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_token)])

    @router.get("/api/docs")
    def docs_list():
        base = docs_dir()
        if base is None or not base.is_dir():
            return {"configured": base is not None, "docs": []}
        entries = []
        for visited, path in enumerate(base.rglob("*")):
            if visited >= DOCS_MAX_VISITED or len(entries) >= DOCS_MAX_FILES:
                break
            if path.suffix.lower() not in DOCS_SUFFIXES or not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            try:
                _candidate(base, rel)
                modified = path.stat().st_mtime
            except (HTTPException, OSError):
                continue
            entries.append((modified, {
                "path": rel,
                "title": _title(path),
                "updated_at": datetime.fromtimestamp(modified, tz=timezone.utc).isoformat(),
            }))
        entries.sort(key=lambda entry: entry[0], reverse=True)
        return {"configured": True, "docs": [item for _, item in entries]}

    @router.get("/api/docs/content")
    def docs_content(path: str = Query(...)):
        base = docs_dir()
        if base is None or not base.is_dir():
            raise HTTPException(status_code=404, detail="docs directory is not configured")
        candidate = _candidate(base, path)
        if candidate.suffix.lower() not in DOCS_SUFFIXES or not candidate.is_file():
            raise HTTPException(status_code=404, detail="no such doc")
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as stream:
                text = stream.read(DOCS_MAX_CHARS)
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"cannot read doc: {exc}") from exc
        return {"path": path, "title": _title(candidate), "text": text}

    return router
