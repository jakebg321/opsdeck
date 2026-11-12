"""Optional operator-owned model boxes behind one small subprocess contract.

OPSDECK_MODEL_ADAPTERS_FILE is a JSON file outside the repository. Each box
names an absolute argv prefix. Opsdeck appends a fixed action and passes one
JSON object on stdin; the adapter returns one JSON object on stdout. No phone
field chooses a command or URL. See README for the action contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from fastapi import HTTPException

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
ACTIONS = {"status", "run", "unload", "worker-start", "worker-stop"}
TIMEOUTS = {"status": 15, "run": 600, "unload": 30, "worker-start": 130, "worker-stop": 30}


def configured_adapters() -> list[dict[str, Any]]:
    source = os.environ.get("OPSDECK_MODEL_ADAPTERS_FILE", "").strip()
    if not source:
        return []
    try:
        path = Path(source).expanduser()
        if path.stat().st_size > 64_000:
            return []
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    if not isinstance(config, dict) or not isinstance(config.get("boxes"), list):
        return []
    boxes = []
    seen = set()
    for item in config["boxes"][:16]:
        if not isinstance(item, dict):
            continue
        ident, command = item.get("id"), item.get("command")
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or ident in seen:
            continue
        if not isinstance(command, list) or not (1 <= len(command) <= 12):
            continue
        if not all(isinstance(part, str) and 0 < len(part) <= 500 for part in command):
            continue
        if not Path(command[0]).is_absolute():
            continue
        seen.add(ident)
        boxes.append({
            "id": ident,
            "label": str(item.get("label") or ident)[:80],
            "kind": "adapter",
            "command": command,
            "image": item.get("image") is True,
            "think": item.get("think") is True,
            "unload": item.get("unload") is True,
            "worker_start": item.get("worker_start") is True,
            "worker_stop": item.get("worker_stop") is True,
            "note": str(item.get("note") or "")[:300],
        })
        if len(boxes) >= 8:
            break
    return boxes


def call_adapter(box: dict[str, Any], action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise HTTPException(status_code=400, detail="unknown model action")
    try:
        proc = subprocess.run(
            [*box["command"], action], input=json.dumps(body or {}).encode("utf-8"),
            capture_output=True, timeout=TIMEOUTS[action], check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=502, detail=f"model adapter unavailable: {type(exc).__name__}") from exc
    if proc.returncode:
        raise HTTPException(status_code=502, detail="model adapter failed")
    if len(proc.stdout) > 256_000:
        raise HTTPException(status_code=502, detail="model adapter response too large")
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="model adapter returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="model adapter must return an object")
    return result
