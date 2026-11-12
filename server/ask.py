"""Configurable Ask-chats relay for the showcase copy.

The external Ask service is NOT included in this repository. Operators who
run a compatible service point this server at it with ``OPSDECK_ASK_URL``;
the phone only ever sends project names, sizes, questions, and job ids.
The upstream endpoint URL always comes from server-side configuration, so a
token holder cannot make the server fetch arbitrary URLs.

Required upstream API (JSON over HTTP, owned and served by the operator):

  GET  {base}/api/chats/projects          -> {"projects": [{"name", "lastActive"}]}
  GET  {base}/api/local/{box}/status      -> {"current"|"loadedModel"|"ok"...}
  POST {base}/api/chats/fit               <- {"mode","project","size","box"}
  POST {base}/api/chats/ask               <- {"box",...fit params,"thread","question","maxTokens"}
  GET  {base}/api/chats/job?id={id}       -> {"status": "done"|"error"|...,
                                              "result": {"text",...}}

``box`` is always the server-configured ``OPSDECK_ASK_BOX`` value; any
client-supplied box is ignored. Upstream 401/403 responses are reported as
502 so they can never be mistaken for an Opsdeck token rejection (the phone
treats Opsdeck 401 as "re-authenticate").
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

BOX_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9._\- /]{1,200}$")

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROJECTS_TIMEOUT = 15
READY_TIMEOUT = 10
FIT_TIMEOUT = 30
ASK_TIMEOUT = 120
JOB_TIMEOUT = 15
MAX_QUESTION_CHARS = 8000


def _valid_base_url(raw: str) -> str | None:
    value = (raw or "").strip().rstrip("/")
    if not value:
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        return None
    authority = value.split("://", 1)[1].split("/")[0]
    # Reject embedded credentials and missing hosts (no userinfo in config).
    if not authority or "@" in authority:
        return None
    if not authority.split(":")[0]:
        return None
    return value


def ask_base_url() -> str | None:
    """Operator-configured Ask service base URL, or None when unconfigured."""
    return _valid_base_url(os.environ.get("OPSDECK_ASK_URL", ""))


def ask_box() -> str:
    """Operator-configured box/model id; neutral default, no hardware names."""
    raw = os.environ.get("OPSDECK_ASK_BOX", "").strip() or "local"
    if not BOX_RE.fullmatch(raw):
        return "local"
    return raw


def console_origin() -> str | None:
    """Origin of the operator-configured console URL for CSP frame-src.

    Returns e.g. "https://gpu.example.com:8443", or None when the console
    URL is unset or invalid. Only http(s) URLs without credentials qualify.
    """
    raw = (os.environ.get("OPSDECK_ASK_CONSOLE_URL", "") or "").strip()
    if not raw:
        return None
    if not (raw.startswith("http://") or raw.startswith("https://")):
        return None
    parts = urllib.parse.urlsplit(raw)
    if not parts.hostname or "@" in (parts.netloc or ""):
        return None
    if parts.username or parts.password:
        return None
    origin = f"{parts.scheme}://{parts.hostname.lower()}"
    if parts.port:
        origin += f":{parts.port}"
    return origin


def console_url() -> str | None:
    """Full operator-configured console URL, or None when unset/invalid."""
    raw = (os.environ.get("OPSDECK_ASK_CONSOLE_URL", "") or "").strip().rstrip("/")
    if not raw:
        return None
    if console_origin() is None:
        return None
    if len(raw) > 2048:
        return None
    return raw


def _require_configured() -> tuple[str, str]:
    base = ask_base_url()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="Ask service is not configured: set OPSDECK_ASK_URL "
            "to your compatible Ask service base URL and restart the server.",
        )
    return base, ask_box()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them.

    The upstream URL is operator-configured, but a compromised or
    misbehaving service must not be able to bounce this relay onto
    another host via a 302 (redirect-based SSRF).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, 502, "Ask service redirected the request", headers, fp)


def _call(path: str, body: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    base, _ = _require_configured()
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        if len(data) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=400, detail="request too large")
    target = base + path
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("OPSDECK_ASK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(target, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
    try:
        with opener.open(request, timeout=timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except HTTPException:
        raise
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(MAX_RESPONSE_BYTES) or b"{}")
            detail = detail.get("error") or detail.get("detail") or str(error)
        except (ValueError, OSError):
            detail = str(error)
        if not isinstance(detail, str) or not detail.strip():
            detail = f"Ask service error {error.code}"
        # Never surface an upstream auth failure as Opsdeck 401: the phone
        # treats Opsdeck 401 as "token rejected, re-authenticate".
        status = error.code if error.code not in (401, 403) else 502
        raise HTTPException(status_code=status, detail=str(detail)[:2000])
    except (urllib.error.URLError, OSError) as error:
        raise HTTPException(status_code=502,
                            detail="Ask service unreachable") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=502,
                            detail="Ask service response too large")
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except ValueError as error:
        raise HTTPException(status_code=502,
                            detail="Ask service returned invalid JSON") from error


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=400, detail="request too large")
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="invalid JSON body") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return body


def make_ask_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix="/api/ask", dependencies=[Depends(require_token)])

    @router.get("/config")
    def config() -> dict[str, Any]:
        """Tell the phone which Ask views are usable; URLs stay server-side
        except the operator-configured console URL the iframe needs."""
        base = ask_base_url()
        return {"configured": bool(base), "box": ask_box(),
                "console_url": console_url()}

    @router.get("/projects")
    def projects() -> Any:
        return _call("/api/chats/projects", timeout=PROJECTS_TIMEOUT)

    @router.get("/ready")
    def ready() -> Any:
        _, box = _require_configured()
        return _call(f"/api/local/{urllib.parse.quote(box, safe='')}/status",
                     timeout=READY_TIMEOUT)

    @router.post("/fit")
    async def fit(request: Request) -> Any:
        _, box = _require_configured()
        body = await _read_json_body(request)
        project = body.get("project", "")
        size = body.get("size", 0)
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
            raise HTTPException(status_code=400, detail="invalid project")
        if not isinstance(size, int) or not 1024 <= size <= 262144:
            raise HTTPException(status_code=400, detail="invalid size")
        return _call("/api/chats/fit",
                     {"mode": "chats", "project": project,
                      "size": size, "box": box},
                     timeout=FIT_TIMEOUT)

    @router.post("/ask")
    async def ask(request: Request) -> Any:
        _, box = _require_configured()
        body = await _read_json_body(request)
        question = body.get("question", "")
        if not isinstance(question, str) or not question.strip():
            raise HTTPException(status_code=400, detail="question is empty")
        if len(question) > MAX_QUESTION_CHARS:
            raise HTTPException(status_code=400, detail="question too long")
        params = body.get("params")
        if not isinstance(params, dict):
            params = {}
        thread = body.get("thread", [])
        if not isinstance(thread, list) or len(thread) > 50:
            raise HTTPException(status_code=400, detail="invalid thread")
        try:
            max_tokens = int(body.get("maxTokens", 4096))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400,
                                detail="invalid maxTokens") from error
        max_tokens = max(1, min(max_tokens, 8192))
        forwarded: dict[str, Any] = {"box": box, "thread": thread,
                                     "question": question.strip(),
                                     "maxTokens": max_tokens}
        for key in ("project", "size", "params", "reach", "files",
                    "since", "earliest", "mode"):
            if key in body and key not in forwarded:
                forwarded[key] = body[key]
        if isinstance(params, dict):
            for key, value in params.items():
                if isinstance(key, str) and len(key) <= 64 and key not in forwarded:
                    forwarded[key] = value
        return _call("/api/chats/ask", forwarded, timeout=ASK_TIMEOUT)

    @router.get("/job")
    def job(id: str) -> Any:
        if not JOB_ID_RE.fullmatch(id or ""):
            raise HTTPException(status_code=400, detail="invalid job id")
        return _call("/api/chats/job?" + urllib.parse.urlencode({"id": id}),
                     timeout=JOB_TIMEOUT)

    return router
