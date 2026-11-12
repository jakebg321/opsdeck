from __future__ import annotations

import asyncio
from collections import deque
import errno
import fcntl
import hmac
import json
import os
import pty
import signal
import subprocess
import struct
import termios
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, StrictInt, StrictStr
from fastapi.staticfiles import StaticFiles

from server.status import build_payload, configured_hosts
from server.ask import console_origin, make_ask_router
from server.docs import make_docs_router
from server.models import make_models_router
from server.agents import make_agents_router
from server.briefings import make_briefings_router

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
HOSTS = frozenset(configured_hosts())
TERMINAL_ATTEMPTS_PER_MINUTE = 60
TERMINAL_HANDSHAKE_LIMIT = 16
_terminal_attempts: dict[str, deque[float]] = {}
_terminal_attempts_lock = threading.Lock()
_terminal_handshake_slots = asyncio.Semaphore(TERMINAL_HANDSHAKE_LIMIT)


class StatusCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._payload = build_payload()
        self._last_poll = 0.0

    def poll(self) -> dict[str, Any]:
        with self._lock:
            if time.monotonic() - self._last_poll >= 3:
                self._payload = build_payload()
                self._last_poll = time.monotonic()
            return self._payload

    def current(self) -> dict[str, Any]:
        with self._lock:
            return self._payload


status_cache = StatusCache()
app = FastAPI(title="opsdeck", docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # The optional full-console view embeds the operator's own console URL
    # in an iframe; allow exactly that origin (or none when unconfigured).
    # The relay itself stays same-origin: the phone never fetches the Ask
    # service directly, so connect-src needs no extra origin.
    policy = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' ws: wss:"
    )
    origin = console_origin()
    if origin:
        policy += f"; frame-src 'self' {origin}"
    response.headers["Content-Security-Policy"] = policy
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def expected_token() -> str:
    token = os.environ.get("OPSDECK_TOKEN")
    if not token or len(token) < 32:
        raise HTTPException(status_code=503, detail="OPSDECK_TOKEN must have at least 32 characters")
    return token


def require_http_token(authorization: str | None = Header(default=None)) -> None:
    expected = expected_token()
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Unauthorized")


app.include_router(make_docs_router(require_http_token))
app.include_router(make_ask_router(require_http_token))
app.include_router(make_models_router(require_http_token))
app.include_router(make_agents_router(require_http_token))
app.include_router(make_briefings_router(require_http_token))


@app.get("/api/config")
async def api_config(authorization: str | None = Header(default=None)):
    require_http_token(authorization)
    return {"hosts": list(configured_hosts())}


@app.get("/api/hello")
async def api_hello():
    # Unauthenticated identity check for clients before they send a token.
    return {"app": "opsdeck"}


@app.get("/api/status", dependencies=[])
async def api_status(authorization: str | None = Header(default=None)):
    require_http_token(authorization)
    return await asyncio.to_thread(status_cache.poll)


class KillRequest(BaseModel):
    host: StrictStr
    pid: StrictInt


def _gpu_host(label: Any) -> str:
    return label.rsplit("·", 1)[-1].strip() if isinstance(label, str) else ""


def _pid_is_listed(payload: dict[str, Any], host: str, pid: int) -> bool:
    if pid <= 0:
        return False
    return any(
        _gpu_host(gpu.get("label")) == host
        and any(proc.get("pid") == pid for proc in (gpu.get("procs") or []))
        for gpu in (payload.get("gpus") or [])
        if isinstance(gpu, dict)
    )


def _stop_gpu_process(host: str, pid: int) -> None:
    if host == "local":
        os.kill(pid, signal.SIGTERM)
        return
    result = subprocess.run(["ssh", host, "kill", str(pid)],
                            capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "remote stop failed").strip()[:500])


@app.post("/api/kill")
async def api_kill(request: KillRequest, authorization: str | None = Header(default=None)):
    require_http_token(authorization)
    payload = status_cache.current()
    if payload.get("sample"):
        return {"ok": False, "sample": True}
    if request.host not in HOSTS:
        raise HTTPException(status_code=400, detail="Host is not configured")
    if not _pid_is_listed(payload, request.host, request.pid):
        raise HTTPException(status_code=409, detail="PID is not in the current GPU snapshot")
    try:
        age = time.time() - float(payload.get("ts", 0))
    except (TypeError, ValueError):
        age = float("inf")
    if age < 0 or age > 30:
        raise HTTPException(status_code=409, detail="GPU snapshot is stale")
    try:
        await asyncio.to_thread(_stop_gpu_process, request.host, request.pid)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


def _resize_pty(fd: int, cols: int, rows: int) -> None:
    cols = max(1, min(cols, 500))
    rows = max(1, min(rows, 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _spawn_terminal(host: str) -> tuple[int, int]:
    pid, fd = pty.fork()
    if pid == 0:
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        if host == "local":
            os.chdir(env.get("HOME") or str(Path.home()))
            os.execvpe("bash", ["bash", "-l"], env)
        os.execvpe("ssh", ["ssh", "-t", host], env)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return pid, fd


def _reap_terminal(pid: int) -> None:
    """End and reap a terminal child so ssh/shell sessions don't pile up as zombies."""
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        return
    except OSError:
        return
    # Give the child ~1s to hang up cleanly, then force it.
    for _ in range(20):
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        pass


async def _pty_to_websocket(websocket: WebSocket, fd: int) -> None:
    while True:
        try:
            data = os.read(fd, 65536)
            if not data:
                return
            await websocket.send_bytes(data)
        except BlockingIOError:
            await asyncio.sleep(0.01)
        except OSError as exc:
            if exc.errno == errno.EIO:
                return
            raise


async def _wait_until_writable(fd: int) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def mark_ready() -> None:
        if not ready.done():
            ready.set_result(None)

    loop.add_writer(fd, mark_ready)
    try:
        await ready
    finally:
        loop.remove_writer(fd)


async def _write_pty(fd: int, data: bytes) -> None:
    """Write all bytes to a nonblocking PTY without losing partial-write suffixes."""
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except BlockingIOError:
            await _wait_until_writable(fd)
            continue
        except InterruptedError:
            continue
        if written == 0:
            raise OSError(errno.EIO, "PTY write returned zero bytes")
        remaining = remaining[written:]


async def _websocket_to_pty(websocket: WebSocket, fd: int) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        text = message.get("text")
        if data is not None:
            await _write_pty(fd, data)
            continue
        if text is None:
            continue
        try:
            control = json.loads(text)
        except json.JSONDecodeError:
            control = None
        if isinstance(control, dict) and control.get("ping"):
            # keepalive heartbeat from the client — never write it to the PTY
            continue
        if (
            isinstance(control, dict)
            and isinstance(control.get("resize"), list)
            and len(control["resize"]) == 2
            and all(isinstance(value, int) for value in control["resize"])
        ):
            _resize_pty(fd, control["resize"][0], control["resize"][1])
        else:
            await _write_pty(fd, text.encode())


def _allowed_websocket_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    allowed = {f"{scheme}://{host}"}
    allowed.update(
        value.strip() for value in os.environ.get("OPSDECK_ALLOWED_ORIGINS", "").split(",")
        if value.strip()
    )
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and origin in allowed
    )


def _allow_terminal_attempt(address: str) -> bool:
    now = time.monotonic()
    cutoff = now - 60
    with _terminal_attempts_lock:
        if len(_terminal_attempts) >= 1024:
            for key, history in list(_terminal_attempts.items()):
                while history and history[0] < cutoff:
                    history.popleft()
                if not history:
                    del _terminal_attempts[key]
            if len(_terminal_attempts) >= 1024 and address not in _terminal_attempts:
                return False
        history = _terminal_attempts.setdefault(address, deque())
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= TERMINAL_ATTEMPTS_PER_MINUTE:
            return False
        history.append(now)
        return True


@app.websocket("/api/term")
async def terminal(websocket: WebSocket, host: str):
    if not _allowed_websocket_origin(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return
    if host not in HOSTS:
        await websocket.close(code=4400, reason="Unknown host")
        return
    address = websocket.client.host if websocket.client else "unknown"
    if not _allow_terminal_attempt(address):
        await websocket.accept()
        await websocket.close(code=4429, reason="Too many connections")
        return

    try:
        await asyncio.wait_for(_terminal_handshake_slots.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        await websocket.accept()
        await websocket.close(code=4429, reason="Too many connections")
        return

    try:
        await websocket.accept()
        expected = os.environ.get("OPSDECK_TOKEN")
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            auth = json.loads(first) if isinstance(first, str) and len(first) <= 2048 else None
        except WebSocketDisconnect:
            return
        except (asyncio.TimeoutError, ValueError, TypeError):
            auth = None
        supplied = auth.get("auth") if isinstance(auth, dict) else None
        if not expected or len(expected) < 32 or not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
            await websocket.close(code=4401, reason="Unauthorized")
            return
    finally:
        _terminal_handshake_slots.release()

    await websocket.send_json({"type": "ready"})
    pid, fd = _spawn_terminal(host)
    try:
        outgoing = asyncio.create_task(_pty_to_websocket(websocket, fd))
        incoming = asyncio.create_task(_websocket_to_pty(websocket, fd))
        done, pending = await asyncio.wait(
            {outgoing, incoming}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            try:
                task.result()
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        await asyncio.to_thread(_reap_terminal, pid)


if DIST.exists():
    assets = DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    # index.html must revalidate every load: with no Cache-Control the iOS web view
    # heuristically caches it and keeps loading the old bundle after a deploy.
    # The hashed /assets files can cache normally.
    NO_CACHE = {"Cache-Control": "no-cache"}

    @app.get("/")
    async def index():
        return FileResponse(DIST / "index.html", headers=NO_CACHE)

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        candidate = (DIST / path).resolve()
        if candidate.is_relative_to(DIST.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html", headers=NO_CACHE)
else:
    @app.get("/")
    async def missing_frontend():
        raise HTTPException(status_code=503, detail="Build the frontend in web/")
