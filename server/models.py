"""User-configurable local-model access for the showcase copy.

Operators point the server at their own Ollama-compatible endpoint(s) with
the OPSDECK_MODELS environment variable; the phone only ever sends a
provider id, model name, prompt, and an optional photo. Endpoint URLs never
come from the client, so a token holder cannot make the server fetch
arbitrary URLs.

Supported provider API (Ollama-compatible, non-streaming):
  GET  {base}/api/tags      -> model list
  POST {base}/api/generate  -> {"model","prompt","stream":false,
                                "images":[...], "options":{"num_predict":N}}
  POST {base}/api/generate  -> {"model","prompt":"","keep_alive":0} unloads that model

Runs execute in bounded daemon threads per job (at most
MAX_CONCURRENT_JOBS at once); the phone polls GET /api/models/jobs/{job_id}.
Run history is the on-disk job store, kept private to the server user, with
atomic result writes so polling never reads half-written JSON.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from server.model_adapters import call_adapter, configured_adapters
from pydantic import BaseModel, Field

PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DATA_URL_RE = re.compile(r"^data:image/(png|jpe?g);base64,", re.IGNORECASE)
MAX_PROVIDERS = 8
MAX_CONCURRENT_JOBS = 3
MAX_PROMPT_CHARS = 8000
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_B64_CHARS = MAX_IMAGE_BYTES * 4 // 3 + 1024
MAX_REQUEST_BYTES = MAX_IMAGE_B64_CHARS + MAX_PROMPT_CHARS + 4096
MAX_RESPONSE_BYTES = 256 * 1024
STATUS_TIMEOUT = 10
MODELS_TIMEOUT = 10
INFER_TIMEOUT = 300
HISTORY_LIMIT = 20
HISTORY_SNIPPET = 400

_job_slots = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)


def configured_providers() -> list[dict[str, str]]:
    """Parse OPSDECK_MODELS="label=url,label2=url2" into server-side endpoints.

    Only http(s) URLs without embedded credentials are accepted; anything
    else is ignored so one bad entry cannot break the rest.
    """
    raw = os.environ.get("OPSDECK_MODELS", "").strip()
    if not raw:
        return []
    providers: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        label, _, url = entry.partition("=")
        label = label.strip()
        url = url.strip().rstrip("/")
        if not PROVIDER_RE.fullmatch(label) or label in seen:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        # Reject embedded credentials and missing hosts (no userinfo in config).
        without_scheme = url.split("://", 1)[1]
        if not without_scheme or "@" in without_scheme.split("/")[0]:
            continue
        host = without_scheme.split("/")[0].split(":")[0]
        if not host:
            continue
        seen.add(label)
        providers.append({"id": label, "label": label, "base_url": url})
        if len(providers) >= MAX_PROVIDERS:
            break
    return providers


def configured_boxes() -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = [{
        **provider, "kind": "ollama", "image": True, "think": False,
        "unload": True, "worker_start": False, "worker_stop": False,
        "note": "Ollama-compatible endpoint",
    } for provider in configured_providers()]
    used = {box["id"] for box in boxes}
    boxes.extend(box for box in configured_adapters() if box["id"] not in used)
    return boxes


def _provider_or_404(provider_id: str) -> dict[str, Any]:
    if not PROVIDER_RE.fullmatch(provider_id or ""):
        raise HTTPException(status_code=400, detail="unknown provider")
    for provider in configured_boxes():
        if provider["id"] == provider_id:
            return provider
    raise HTTPException(status_code=400, detail="unknown provider")


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _jobs_dir(create: bool = False) -> Path:
    d = Path.home() / ".opsdeck-community" / "model-runs"
    if create:
        _ensure_private_dir(Path.home() / ".opsdeck-community")
        _ensure_private_dir(d)
    return d


def _job_dir(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise HTTPException(status_code=400, detail="invalid job id")
    return _jobs_dir() / job_id


def _write_text_private(path: Path, text: str) -> None:
    """Write a small text file atomically with owner-only permissions.

    The payload lands in a temp file first and is renamed over the target,
    so readers of result.json never observe half-written JSON.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_result(job_dir: Path, payload: dict[str, Any]) -> None:
    _write_text_private(job_dir / "result.json", json.dumps(payload))


def _decode_image(image_base64: str) -> str:
    """Validate an optional photo and return normalized base64 (no data-URL prefix).

    Accepts a PNG/JPEG data URL or raw base64. Rejects anything else, so a
    text-only endpoint never receives a malformed images payload.
    """
    payload = image_base64.strip()
    match = DATA_URL_RE.match(payload)
    if match:
        payload = payload[match.end():]
    elif payload.startswith("data:"):
        raise HTTPException(status_code=400, detail="image must be PNG or JPEG")
    if len(payload) > MAX_IMAGE_B64_CHARS:
        raise HTTPException(status_code=400, detail="image exceeds 4 MB")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="image is not valid base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image exceeds 4 MB")
    if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:2] == b"\xff\xd8":
        return base64.b64encode(raw).decode("ascii")
    raise HTTPException(status_code=400, detail="image must be PNG or JPEG")


def _http_json(base_url: str, path: str, body: Any, timeout: int) -> Any:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=400, detail="request too large")
    request = urllib.request.Request(
        base_url + path, data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"model endpoint unreachable: {type(exc).__name__}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=502, detail="model endpoint response too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="model endpoint returned invalid JSON") from exc


def _model_names(tags: Any) -> list[str]:
    models: list[str] = []
    items = tags.get("models") if isinstance(tags, dict) else None
    if not isinstance(items, list):
        return []
    for entry in items:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip() and MODEL_RE.fullmatch(name.strip()):
            models.append(name.strip())
            if len(models) >= 100:
                break
    return models


def _run_job(job_id: str, box: dict[str, Any], provider_id: str, model: str,
             prompt: str, max_tokens: int, image_b64: str | None, think: bool = False) -> None:
    job_dir = _jobs_dir() / job_id
    started = time.time()
    try:
        try:
            if box["kind"] == "adapter":
                payload = call_adapter(box, "run", {
                    "model": model, "prompt": prompt, "max_tokens": max_tokens,
                    "image_base64": image_b64, "think": think,
                })
                state = payload.get("state")
                if state in ("refused", "error"):
                    _write_result(job_dir, {
                        "state": state, "job_id": job_id, "provider": provider_id,
                        "model": model, "error": str(payload.get("error") or "")[:2000],
                        "held_by": str(payload.get("held_by") or "")[:200],
                        "finished_at": int(time.time()),
                    })
                    return
                answer = payload.get("answer")
            else:
                body: dict[str, Any] = {
                    "model": model, "prompt": prompt, "stream": False,
                    "options": {"num_predict": max_tokens},
                }
                if image_b64 is not None:
                    body["images"] = [image_b64]
                payload = _http_json(box["base_url"], "/api/generate", body, INFER_TIMEOUT)
                answer = payload.get("response") if isinstance(payload, dict) else ""
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "model run failed"
            _write_result(job_dir, {"state": "error", "job_id": job_id,
                                   "provider": provider_id, "model": model,
                                   "error": detail[-2000:], "finished_at": int(time.time())})
            return
        except (AttributeError, KeyError, TypeError, ValueError):
            _write_result(job_dir, {"state": "error", "job_id": job_id,
                                   "provider": provider_id, "model": model,
                                   "error": "model returned an invalid response",
                                   "finished_at": int(time.time())})
            return
        if not isinstance(answer, str) or not answer.strip():
            _write_result(job_dir, {"state": "error", "job_id": job_id,
                                   "provider": provider_id, "model": model,
                                   "error": "model returned no text",
                                   "duration_s": round(time.time() - started, 1),
                                   "finished_at": int(time.time())})
            return
        _write_result(job_dir, {
            "state": "done", "job_id": job_id, "provider": provider_id,
            "model": model, "answer": answer[:20000],
            "duration_s": round(time.time() - started, 1),
            "prompt_tokens": payload.get("prompt_eval_count", payload.get("prompt_tokens")),
            "completion_tokens": payload.get("eval_count", payload.get("completion_tokens")),
            "finished_at": int(time.time()),
        })
    finally:
        _job_slots.release()


class SubmitRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    image_base64: str | None = Field(default=None, max_length=MAX_IMAGE_B64_CHARS + 64)
    max_tokens: int = Field(default=256, ge=1, le=4096)
    think: bool = False


class UnloadRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model: str = Field(min_length=1, max_length=200)


class WorkerRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)


def make_models_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix="/api/models", dependencies=[Depends(require_token)])

    @router.get("/providers")
    def providers() -> dict[str, Any]:
        items = configured_boxes()
        public = [{key: p[key] for key in (
            "id", "label", "kind", "image", "think", "unload",
            "worker_start", "worker_stop", "note",
        )} for p in items]
        return {"configured": bool(items), "providers": public}

    @router.get("/boxes")
    def boxes() -> dict[str, Any]:
        return {"boxes": {p["id"]: p for p in providers()["providers"]}}

    @router.get("/status")
    def status(provider: str) -> dict[str, Any]:
        resolved = _provider_or_404(provider)
        try:
            if resolved["kind"] == "adapter":
                payload = call_adapter(resolved, "status")
                models = payload.get("models", [])
                if not isinstance(models, list):
                    models = []
                return {"provider": provider, "ok": payload.get("ok") is not False,
                        "models": [name for name in models if isinstance(name, str)][:100],
                        "holder": str(payload.get("holder") or "")[:200],
                        "gpu": payload.get("gpu") if isinstance(payload.get("gpu"), dict) else {},
                        "disabled": payload.get("disabled") if isinstance(payload.get("disabled"), list) else [],
                        "error": str(payload.get("error") or "")[:500]}
            payload = _http_json(resolved["base_url"], "/api/tags", None, STATUS_TIMEOUT)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "endpoint unreachable"
            return {"provider": provider, "ok": False, "models": [], "error": detail[-500:]}
        return {"provider": provider, "ok": True, "models": _model_names(payload)}

    @router.get("/models")
    def models(provider: str) -> dict[str, Any]:
        return status(provider)

    @router.post("/submit")
    def submit(request: SubmitRequest) -> dict[str, Any]:
        resolved = _provider_or_404(request.provider)
        model = request.model.strip()
        prompt = request.prompt.strip()
        if not MODEL_RE.fullmatch(model):
            raise HTTPException(status_code=400, detail="invalid model name")
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is empty")
        if request.image_base64 is not None and not resolved["image"]:
            raise HTTPException(status_code=400, detail="images are not supported on this box")
        image_b64 = (_decode_image(request.image_base64)
                     if request.image_base64 is not None else None)
        if not _job_slots.acquire(blocking=False):
            raise HTTPException(status_code=429,
                                detail="too many model jobs running, try again shortly")
        try:
            import uuid

            job_id = uuid.uuid4().hex[:12]
            job_dir = _ensure_private_dir(_jobs_dir(create=True) / job_id)
            _write_text_private(job_dir / "prompt.txt", prompt[:MAX_PROMPT_CHARS])
            _write_text_private(job_dir / "meta.json", json.dumps({
                "job_id": job_id, "provider": request.provider, "model": model,
                "max_tokens": request.max_tokens, "has_image": image_b64 is not None,
                "started_at": int(time.time()),
            }))
            _write_result(job_dir, {"state": "queued", "job_id": job_id,
                                   "provider": request.provider, "model": model})
            thread = threading.Thread(
                target=_run_job,
                args=(job_id, resolved, request.provider, model,
                      prompt, request.max_tokens, image_b64, bool(request.think and resolved["think"])),
                name=f"models-{job_id}", daemon=True,
            )
            thread.start()
        except BaseException:
            _job_slots.release()
            raise
        return {"job_id": job_id}

    @router.get("/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        job_dir = _job_dir(job_id)
        try:
            return json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="job not found") from None

    @router.get("/history")
    def history() -> dict[str, Any]:
        base = _jobs_dir()
        entries: list[dict[str, Any]] = []
        if base.is_dir():
            dirs = sorted((p for p in base.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:HISTORY_LIMIT]
            for job_dir in dirs:
                try:
                    result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
                    meta = json.loads((job_dir / "meta.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                prompt = ""
                try:
                    prompt = (job_dir / "prompt.txt").read_text(encoding="utf-8")[:HISTORY_SNIPPET]
                except OSError:
                    pass
                answer = result.get("answer", "") if isinstance(result, dict) else ""
                entries.append({
                    "job_id": meta.get("job_id", job_dir.name),
                    "provider": meta.get("provider", result.get("provider", "")),
                    "model": meta.get("model", result.get("model", "")),
                    "state": result.get("state", "unknown"),
                    "prompt": prompt,
                    "has_image": bool(meta.get("has_image")),
                    "answer": answer[:HISTORY_SNIPPET] if isinstance(answer, str) else "",
                    "duration_s": result.get("duration_s"),
                    "started_at": meta.get("started_at"),
                    "finished_at": result.get("finished_at"),
                })
        return {"runs": entries}

    @router.post("/unload")
    def unload(request: UnloadRequest) -> dict[str, Any]:
        resolved = _provider_or_404(request.provider)
        model = request.model.strip()
        if not resolved["unload"]:
            raise HTTPException(status_code=400, detail="unload is not supported on this box")
        if not MODEL_RE.fullmatch(model):
            raise HTTPException(status_code=400, detail="invalid model name")
        try:
            if resolved["kind"] == "adapter":
                result = call_adapter(resolved, "unload", {"model": model})
                if result.get("ok") is False:
                    raise HTTPException(status_code=502, detail="model unload failed")
            else:
                _http_json(resolved["base_url"], "/api/generate",
                           {"model": model, "prompt": "", "keep_alive": 0}, STATUS_TIMEOUT)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else "unload failed"
            raise HTTPException(status_code=502, detail=detail[-500:]) from exc
        return {"ok": True}

    @router.post("/worker-start")
    def worker_start(request: WorkerRequest) -> dict[str, Any]:
        box = _provider_or_404(request.provider)
        if not box["worker_start"]:
            raise HTTPException(status_code=400, detail="worker start is not configured")
        result = call_adapter(box, "worker-start")
        if result.get("ok") is False:
            raise HTTPException(status_code=502, detail="worker start failed")
        return {"ok": True}

    @router.post("/worker-stop")
    def worker_stop(request: WorkerRequest) -> dict[str, Any]:
        box = _provider_or_404(request.provider)
        if not box["worker_stop"]:
            raise HTTPException(status_code=400, detail="worker stop is not configured")
        result = call_adapter(box, "worker-stop")
        if result.get("ok") is False:
            raise HTTPException(status_code=502, detail="worker stop failed")
        return {"ok": True}

    return router
