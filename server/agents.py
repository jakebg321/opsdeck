"""Configurable agent sessions for the showcase copy.

Operators enable their own agent CLIs and project folders with environment
variables; the phone only ever sends ids, names, and prompt text. No file
paths, executable paths, or endpoint URLs come from the client.

Operator configuration:

  OPSDECK_AGENTS="claude,codex"             enabled agent ids (empty = feature off)
  OPSDECK_AGENT_COMMANDS="claude=/usr/local/bin/claude,claude@lab=/home/op/bin/claude"
                                            agent=/abs/path default, with
                                            optional agent@machine= overrides
                                            for hosts with other layouts
  OPSDECK_AGENT_MODELS="claude=opus,sonnet" optional model choices per agent
  OPSDECK_PROJECT_ROOTS="/srv/projects,lab=/data/projects"
                                            bare path = local machine root,
                                            machine=path = that host's root
  OPSDECK_AGENT_SESSIONS_DIR="/srv/agent-sessions,lab=/data/agent-sessions"
                                            optional JSONL transcript store
                                            per machine; layout
                                            <dir>/<agent>/*.jsonl or flat
  OPSDECK_SKILLS_DIR="/srv/agent-skills"    optional Markdown skill catalog

Machines come from the shared OPSDECK_SSH_HOSTS setting: "local" always
exists, every alias is an ssh machine. Remote execution reuses the same
fixed-argv discipline as the terminal: no shell=True anywhere, prompts
travel as argv elements (local spawn) or inside a JSON stdin payload
(remote launcher). Command execution stays disabled with an explicit
message until its prerequisites (enabled agent, configured command,
configured project root) are present.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._/:@-]{1,100}$")
AGENT_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")

SSH_TIMEOUT = 20
WALK_TIMEOUT = 40
RECENT_SCAN_TIMEOUT = 15
RECENT_TOTAL_TIMEOUT = 20
RECENT_MAX_SESSIONS = 200
SESSIONS_MAX_FILES = 2000
MAX_MESSAGE_CHARS = 8000
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_LINES = 200000

# Agent catalog. models[]/efforts[] are only UI hints; the operator's
# OPSDECK_AGENT_MODELS setting overrides models. transcript=False agents
# keep no retrievable plain-text log, so transcript/send answer 409 and the
# phone shows the run log instead. Resume argv shapes mirror each CLI's
# documented session-continue flags; the prompt stays the last element.
AGENT_SPECS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude",
        "efforts": ["low", "medium", "high", "max"],
        "transcript": True,
        "launch_extra": [],
        "resume": ["-p", "--resume", "{session}"],
    },
    "codex": {
        "label": "Codex",
        "efforts": ["low", "medium", "high", "xhigh"],
        "transcript": True,
        "launch_extra": ["exec"],
        "resume": ["exec", "resume", "{session}"],
    },
    "kimi": {
        "label": "Kimi",
        "efforts": [],
        "transcript": True,
        "launch_extra": ["-p"],
        "resume": ["-S", "{session}", "-p"],
    },
    "qwen": {
        "label": "Qwen",
        "efforts": [],
        "transcript": True,
        "launch_extra": ["-p"],
        "resume": ["-r", "{session}", "-p"],
    },
    "muse": {
        "label": "Muse",
        "efforts": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        "transcript": False,
        "launch_extra": ["exec"],
        "resume": [],
    },
    "glm": {
        "label": "GLM adapter",
        "efforts": [],
        "transcript": False,
        "launch_extra": ["exec"],
        "resume": [],
    },
}


def enabled_agents() -> list[str]:
    raw = os.environ.get("OPSDECK_AGENTS", "")
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and AGENT_ID_RE.fullmatch(name) and name in AGENT_SPECS and name not in seen:
            seen.append(name)
    return seen


def _parse_commands() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Parse OPSDECK_AGENT_COMMANDS.

    Entries are ``agent=/abs/path`` (default for every machine) or
    ``agent@machine=/abs/path`` (override for one machine). Server and
    remote hosts often keep CLIs in different places, so the override
    form exists for exactly that. Relative or blank paths are ignored.
    """
    defaults: dict[str, str] = {}
    per_machine: dict[tuple[str, str], str] = {}
    for entry in os.environ.get("OPSDECK_AGENT_COMMANDS", "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, path = entry.partition("=")
        name = name.strip().lower()
        path = path.strip()
        machine = "local"
        if "@" in name:
            name, _, machine = name.partition("@")
            name = name.strip()
            machine = machine.strip()
        if (name not in AGENT_SPECS or not HOST_RE.fullmatch(machine)
                or not path.startswith("/") or "\x00" in path or "\n" in path):
            continue
        if machine == "local":
            if name not in defaults:
                defaults[name] = path
        elif (name, machine) not in per_machine:
            per_machine[(name, machine)] = path
    return defaults, per_machine


def agent_commands() -> dict[str, str]:
    """agent -> default absolute CLI path. Relative or blank values are ignored."""
    defaults, _ = _parse_commands()
    return defaults


def agent_command_for(agent: str, machine: str) -> str | None:
    """Effective CLI path for one agent on one machine.

    A per-machine ``agent@machine=`` entry wins; otherwise the ``agent=``
    default applies. Returns None when neither is configured.
    """
    defaults, per_machine = _parse_commands()
    return per_machine.get((agent, machine), defaults.get(agent))


def agent_models_cfg() -> dict[str, list[str]]:
    """agent -> operator-configured model choices. ';' separates agents."""
    out: dict[str, list[str]] = {}
    for group in os.environ.get("OPSDECK_AGENT_MODELS", "").split(";"):
        group = group.strip()
        if not group or "=" not in group:
            continue
        name, _, models = group.partition("=")
        name = name.strip().lower()
        if name not in AGENT_SPECS or name in out:
            continue
        choices = [m.strip() for m in models.split(",") if m.strip()]
        choices = [m for m in choices if MODEL_RE.fullmatch(m)][:50]
        if choices:
            out[name] = choices
    return out


def project_roots() -> dict[str, Path]:
    """machine -> resolved project root. A bare path configures local."""
    return _machine_path_map(os.environ.get("OPSDECK_PROJECT_ROOTS", ""))


def _machine_path_map(raw_value: str) -> dict[str, Path]:
    """Parse comma-separated ``path`` / ``machine=path`` directory settings.

    A bare path configures the server host ("local"); ``machine=path``
    entries configure one ssh host each. Values must be absolute.
    """
    out: dict[str, Path] = {}
    for entry in raw_value.split(","):
        entry = entry.strip()
        if not entry or "\x00" in entry:
            continue
        if "=" in entry:
            machine, _, path = entry.partition("=")
            machine = machine.strip()
            path = path.strip()
            if not HOST_RE.fullmatch(machine):
                continue
        else:
            machine, path = "local", entry
        if not path.startswith("/") or machine in out:
            continue
        try:
            out[machine] = Path(path).expanduser().resolve()
        except OSError:
            continue
    return out


def sessions_bases() -> dict[str, Path]:
    """machine -> transcript store. Same ``path``/``machine=path`` syntax."""
    return _machine_path_map(os.environ.get("OPSDECK_AGENT_SESSIONS_DIR", ""))


def sessions_base() -> Path | None:
    return sessions_bases().get("local")


def skills_base() -> Path | None:
    raw = os.environ.get("OPSDECK_SKILLS_DIR", "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def runs_base(create: bool = False) -> Path:
    d = Path.home() / ".opsdeck-community" / "agent-runs"
    if create:
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d.parent, 0o700)
        except OSError:
            pass
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def configured_hosts() -> list[str]:
    """Local plus every alias from the shared OPSDECK_SSH_HOSTS setting."""
    from server.status import configured_hosts as _hosts

    return _hosts()


def _machine_entry(machine: str) -> dict[str, str]:
    if machine == "local":
        return {"id": "local", "kind": "local"}
    for alias in configured_hosts():
        if alias == machine:
            return {"id": alias, "kind": "ssh", "ssh": alias}
    raise HTTPException(status_code=400, detail=f"unknown machine: {machine}")


def _agent_spec(agent: str) -> dict[str, Any]:
    try:
        return AGENT_SPECS[agent]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"unknown agent: {agent}") from None


def _check_enabled(agent: str) -> dict[str, Any]:
    spec = _agent_spec(agent)
    if agent not in enabled_agents():
        raise HTTPException(
            status_code=400,
            detail=f"agent {agent} is not enabled: set OPSDECK_AGENTS on the server",
        )
    return spec


def _check_session(session: str) -> str:
    if not SESSION_ID_RE.fullmatch(session or ""):
        raise HTTPException(status_code=400, detail="invalid session id")
    return session


def _check_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id or ""):
        raise HTTPException(status_code=400, detail="invalid run id")
    return run_id


def _check_model_effort(spec: dict[str, Any], agent: str, model: str, effort: str) -> tuple[str, str]:
    model = (model or "").strip()
    effort = (effort or "").strip()
    if model.lower() == "default":
        model = ""
    if model and not MODEL_RE.fullmatch(model):
        raise HTTPException(status_code=400, detail=f"invalid model name: {model}")
    if effort and effort not in spec["efforts"]:
        raise HTTPException(status_code=400, detail=f"unsupported effort for {agent}: {effort}")
    return model, effort


def _command_or_503(agent: str, machine: str) -> str:
    command = agent_command_for(agent, machine)
    if not command:
        raise HTTPException(
            status_code=503,
            detail=(f"agent {agent} is not installed for machine {machine}: set "
                    "OPSDECK_AGENT_COMMANDS on the server (agent=/abs/path, "
                    "optionally agent@machine=/abs/path)"),
        )
    return command


def _write_text_private(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _tail_lines(path: Path, n: int = 30) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 4096
            data = b""
            while len(data.splitlines()) <= n and size > 0:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(block) + data
                if size == 0:
                    break
        return data.decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _pid_alive_local(pid: int) -> bool:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # A reaped zombie still answers kill(0); confirm via waitpid result.
        try:
            _reaped, _status = os.waitpid(pid, os.WNOHANG)
            return _reaped != pid
        except (ChildProcessError, OSError):
            return False
    except PermissionError:
        return True
    return True


def _pid_alive(entry: dict[str, str], pid: int) -> bool:
    if entry["kind"] == "local":
        return _pid_alive_local(pid)
    try:
        proc = _run_bytes(["ssh", entry["ssh"], "kill", "-0", str(pid)], SSH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _run_bytes(argv: list[str], timeout: int, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    # Fixed argv only; never shell=True. ssh joins everything after the host
    # into one remote-shell string, so quote each piece; free-text prompts
    # travel as a single quoted element, never interpreted remotely.
    if argv and argv[0] == "ssh" and len(argv) > 2:
        argv = argv[:2] + [shlex.join(argv[2:])]
    return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)


def _check_project_name(project: str) -> list[str]:
    if not project or project.startswith("/") or "\\" in project or "\x00" in project:
        raise HTTPException(status_code=400, detail="invalid project name")
    parts = [p for p in project.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise HTTPException(status_code=400, detail="invalid project name")
    return parts


def _resolve_project_local(machine: str, project: str) -> Path:
    parts = _check_project_name(project)
    roots = project_roots()
    root = roots.get(machine)
    if root is None or not root.is_dir():
        raise HTTPException(
            status_code=503,
            detail=f"project folders are not configured for machine {machine}: "
            "set OPSDECK_PROJECT_ROOTS on the server",
        )
    try:
        base = root.resolve()
        resolved = (base / Path(*parts)).resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="invalid project name") from None
    if resolved != base and not str(resolved).startswith(str(base) + os.sep):
        raise HTTPException(status_code=400, detail="project escapes its root")
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown project: {project}")
    return resolved


def _model_effort_argv(agent: str, model: str, effort: str) -> list[str]:
    """Build only flags verified for the selected CLI; prompts never enter a shell."""
    argv: list[str] = []
    if model:
        if agent in ("codex", "kimi", "qwen"):
            argv += ["-m", model]
        elif agent in ("claude", "muse"):
            argv += ["--model", model]
        else:
            raise HTTPException(status_code=400, detail=f"model flag is unsupported for {agent}")
    if effort:
        if agent == "codex":
            argv += ["-c", f"model_reasoning_effort={effort}"]
        elif agent == "claude":
            argv += ["--effort", effort]
        elif agent == "muse":
            argv += ["--reasoning-effort", effort]
        else:
            raise HTTPException(status_code=400, detail=f"effort flag is unsupported for {agent}")
    return argv


def _launch_argv(agent: str, command: str, model: str, effort: str,
                 message: str) -> list[str]:
    argv = [command]
    if agent in ("codex", "muse"):
        argv.append("exec")
    argv += _model_effort_argv(agent, model, effort)
    if agent in ("claude", "kimi", "qwen"):
        argv += ["-p", message]
    else:
        # GLM is an operator-supplied adapter accepting a positional prompt.
        argv.append(message)
    return argv


def _resume_argv(agent: str, command: str, model: str, effort: str,
                 session: str, message: str) -> list[str]:
    if agent == "claude":
        argv = [command, "--resume", session]
    elif agent == "codex":
        argv = [command, "exec", "resume", session]
    elif agent == "kimi":
        argv = [command, "-S", session]
    elif agent == "qwen":
        argv = [command, "-r", session]
    else:
        raise HTTPException(status_code=409, detail=(
            "resume is unavailable for this agent; start a new run instead"))
    argv += _model_effort_argv(agent, model, effort)
    if agent in ("claude", "kimi", "qwen"):
        argv += ["-p", message]
    else:
        argv.append(message)
    return argv


# Remote launcher: runs detached on the target host via `ssh <t> python3 -c`.
# Payload (JSON on stdin): {run_id, agent, machine, project, root, argv, env,
# message unused}. The prompt travels inside argv JSON, never through a
# shell. Prints {"run_id","pid"}.
_REMOTE_LAUNCH_PY = r"""
import json, os, subprocess, sys
payload = json.load(sys.stdin)
run_id = payload["run_id"]
root = os.path.realpath(payload["root"])
name = payload["name"]
parts = [p for p in name.split("/") if p not in ("", ".")]
if not parts or any(p == ".." or p.startswith(".") for p in parts) or name.startswith("/"):
    sys.exit("invalid project name")
proj = os.path.realpath(os.path.join(root, *[p for p in parts]))
if proj != root and not proj.startswith(root + os.sep):
    sys.exit("project escapes its root")
if not os.path.isdir(proj):
    sys.exit("unknown project: " + name)
argv = payload["argv"]
if not argv or not isinstance(argv[0], str) or not argv[0].startswith("/"):
    sys.exit("agent command is not configured on this host")
rundir = os.path.join(os.path.expanduser("~/.opsdeck-community/agent-runs"), run_id)
os.makedirs(rundir, exist_ok=True)
log = os.path.join(rundir, "output.log")
env = dict(os.environ)
env.update({k: str(v) for k, v in (payload.get("env") or {}).items()})
with open(log, "ab") as fh:
    proc = subprocess.Popen(argv, cwd=proj, stdin=subprocess.DEVNULL,
                            stdout=fh, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
meta = {"run_id": run_id, "agent": payload["agent"], "machine": payload["machine"],
        "project": name, "session": payload.get("session"),
        "model": payload.get("model") or "", "effort": payload.get("effort") or "",
        "pid": proc.pid, "started_at": int(__import__("time").time())}
with open(os.path.join(rundir, "meta.json"), "w") as fh:
    json.dump(meta, fh)
print(json.dumps({"run_id": run_id, "pid": proc.pid}))
"""


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _record_run(run_id: str, payload: dict[str, Any]) -> None:
    rundir = runs_base(create=True) / run_id
    rundir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(rundir, 0o700)
    except OSError:
        pass
    _write_text_private(rundir / "meta.json", json.dumps(payload))


def _spawn(agent: str, entry: dict[str, str], project: str, argv: list[str],
           model: str, effort: str, session: str | None) -> str:
    run_id = _new_run_id()
    if entry["kind"] == "local":
        command = argv[0]
        if not (os.path.isfile(command) and os.access(command, os.X_OK)):
            raise HTTPException(
                status_code=503,
                detail=f"agent {agent} is not installed: {command} is not executable",
            )
        proj = _resolve_project_local(entry["id"], project)
        rundir = runs_base(create=True) / run_id
        rundir.mkdir(parents=True, exist_ok=True)
        log = open(rundir / "output.log", "ab")
        try:
            proc = subprocess.Popen(argv, cwd=str(proj), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    env=dict(os.environ), start_new_session=True)
        finally:
            log.close()
        _record_run(run_id, {"run_id": run_id, "agent": agent, "machine": entry["id"],
                             "project": project, "session": session,
                             "model": model or "", "effort": effort or "",
                             "pid": proc.pid, "started_at": int(time.time())})
        return run_id
    parts = _check_project_name(project)
    roots = project_roots()
    root = roots.get(entry["id"])
    if root is None:
        raise HTTPException(
            status_code=503,
            detail=f"project folders are not configured for machine {entry['id']}: "
            "set OPSDECK_PROJECT_ROOTS on the server",
        )
    target = entry.get("ssh") or entry["id"]
    payload = {"run_id": run_id, "agent": agent, "machine": entry["id"],
               "name": "/".join(parts), "root": str(root), "argv": argv, "env": {},
               "session": session, "model": model or "", "effort": effort or ""}
    try:
        proc = _run_bytes(["ssh", target, "python3", "-c", _REMOTE_LAUNCH_PY],
                          SSH_TIMEOUT, stdin=json.dumps(payload).encode())
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=502, detail=f"ssh to {target} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.decode(errors="replace") or proc.stdout.decode(errors="replace")).strip()
        raise HTTPException(status_code=502, detail=f"remote launch on {target} failed: {detail[-300:]}")
    try:
        remote = json.loads(proc.stdout.decode())
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"bad remote response from {target}") from exc
    _record_run(run_id, {"run_id": run_id, "agent": agent, "machine": entry["id"],
                         "project": project, "session": session,
                         "model": model or "", "effort": effort or "",
                         "pid": remote.get("pid", 0), "started_at": int(time.time()),
                         "remote": True})
    return run_id


def _read_meta(run_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((runs_base() / run_id / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _linked_run(machine_id: str, agent: str, session: str) -> dict[str, Any] | None:
    """Newest local run record linking this agent+machine+session."""
    best: dict[str, Any] | None = None
    best_mtime = -1.0
    try:
        entries = sorted(
            (p for p in runs_base().iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:200]
    except OSError:
        return None
    for rundir in entries:
        try:
            meta = json.loads((rundir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("agent") == agent and meta.get("machine") == machine_id \
                and meta.get("session") == session:
            try:
                mtime = (rundir / "meta.json").stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best = {**meta, "run_dir": str(rundir)}
    return best


def _peek_json_objects(path: Path, max_lines: int = 12, max_bytes: int = 65536) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes)
    except OSError:
        return out
    for raw in data.decode(errors="replace").splitlines()[:max_lines]:
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _head_project(objs: list[dict[str, Any]]) -> str:
    for obj in objs:
        wrappers = [obj]
        payload = obj.get("payload")
        if isinstance(payload, dict):
            wrappers.append(payload)
        message = obj.get("message")
        if isinstance(message, dict):
            wrappers.append(message)
        for item in wrappers:
            for key in ("cwd", "directory", "project", "workdir", "workspace"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith("/"):
                    return value
    return ""


def _head_user_text(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(262144)
    except OSError:
        return ""
    for raw in data.decode(errors="replace").splitlines():
        if '"role"' not in raw and '"role":' not in raw.replace(" ", ""):
            continue
        try:
            obj = json.loads(raw) if raw.strip().startswith("{") else None
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        text = _extract_text(obj)
        role = _extract_role(obj)
        if role == "user" and text and not text.lstrip().startswith(("<", "# AGENTS.md")):
            return text[:160].replace("\n", " ")
    return ""


def _extract_role(obj: dict[str, Any]) -> str:
    for key in ("role",):
        value = obj.get(key)
        if isinstance(value, str):
            low = value.lower()
            if low in ("user", "human"):
                return "user"
            if low in ("tool", "system", "function"):
                return "tool"
            return "assistant"
    payload = obj.get("payload")
    if isinstance(payload, dict):
        return _extract_role(payload)
    message = obj.get("message")
    if isinstance(message, dict):
        return _extract_role(message)
    otype = obj.get("type")
    if isinstance(otype, str) and otype in ("function_call", "custom_tool_call", "function_call_output",
                                            "custom_tool_call_output", "tool_result", "functionCall",
                                            "functionResponse"):
        return "tool"
    return "assistant"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str) and block.get("type", "text") in (
                        "text", "input_text", "output_text"):
                    texts.append(block["text"])
                elif isinstance(block.get("text"), str) and "function" not in str(block.get("type", "")):
                    texts.append(block["text"])
        return "\n".join(texts)
    return ""


def _extract_text(obj: dict[str, Any]) -> str:
    message = obj.get("message")
    if isinstance(message, dict):
        text = _extract_text(message)
        if text:
            return text
    payload = obj.get("payload")
    if isinstance(payload, dict):
        text = _extract_text(payload)
        if text:
            return text
    for key in ("content", "text", "message", "value", "response", "output"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            text = _content_text(value)
            if text.strip():
                return text
    parts = obj.get("parts")
    if isinstance(parts, list):
        texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return ""


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _normalize_lines(lines: list[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        role = _extract_role(obj)
        otype = str(obj.get("type") or "")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None
        item = payload if payload is not None else obj
        itype = str(item.get("type") or otype)
        if itype in ("function_call", "custom_tool_call", "tool_use", "functionCall"):
            name = item.get("name") or item.get("call_id") or "tool"
            args = item.get("arguments") or item.get("input") or item.get("args") or ""
            summary = f"{name}({_short(str(args), 120)})"
            messages.append({"role": "tool", "kind": itype, "text": summary,
                             "summary": summary, "detail": {"name": name, "arguments": str(args)}})
            continue
        if itype in ("function_call_output", "custom_tool_call_output", "tool_result",
                     "functionResponse", "reasoning"):
            output = (item.get("output") or item.get("content")
                      or item.get("response") or item.get("text") or "")
            text = _content_text(output) if not isinstance(output, str) else output
            if itype == "reasoning" and not text.strip():
                continue
            summary = f"tool_result: {_short(str(text), 160)}"
            messages.append({"role": "tool" if itype != "reasoning" else "assistant",
                             "kind": itype, "text": str(text), "summary": summary})
            continue
        text = _extract_text(obj)
        if not text.strip():
            continue
        messages.append({"role": role, "kind": "message", "text": text, "summary": _short(text)})
        if len(messages) >= 1000:
            break
    return messages


def _scan_store(base: Path, assume_agent: str = "") -> list[dict[str, Any]]:
    """Scan a JSONL transcript store: <base>/<agent>/*.jsonl or flat files."""
    sessions: list[dict[str, Any]] = []
    if not base.is_dir():
        return sessions
    try:
        files = sorted(
            (p for p in base.rglob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:SESSIONS_MAX_FILES]
    except OSError:
        return sessions
    try:
        resolved_base = base.resolve()
    except OSError:
        return sessions
    for path in files:
        try:
            resolved = path.resolve()
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if resolved != resolved_base and not str(resolved).startswith(str(resolved_base) + os.sep):
            continue
        try:
            rel = resolved.relative_to(resolved_base)
        except ValueError:
            continue
        first = rel.parts[0] if rel.parts else ""
        agent = first if first in AGENT_SPECS else assume_agent
        objs = _peek_json_objects(path, max_lines=6)
        project = _head_project(objs)
        sessions.append({
            "session_id": path.stem,
            "agent": agent,
            "project": project,
            "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "_mtime": mtime,
            "preview": _head_user_text(path),
            "_path": str(resolved),
        })
    return sessions


def _find_local_transcript(agent: str, session: str) -> Path | None:
    base = sessions_base()
    if base is None or not base.is_dir():
        return None
    try:
        resolved_base = base.resolve()
    except OSError:
        return None
    agent_dir = base / agent
    roots = [agent_dir] if agent_dir.is_dir() else [base]
    candidates: list[Path] = []
    for root in roots:
        try:
            files = [p for p in root.rglob("*.jsonl") if p.is_file()]
        except OSError:
            continue
        for path in files[:SESSIONS_MAX_FILES]:
            if path.stem == session:
                candidates.append(path)
    if not candidates and roots:
        for root in roots:
            try:
                files = [p for p in root.rglob("*.jsonl") if p.is_file()]
            except OSError:
                continue
            for path in files[:SESSIONS_MAX_FILES]:
                for obj in _peek_json_objects(path, max_lines=6):
                    blob = json.dumps(obj)
                    if session in blob:
                        candidates.append(path)
                        break
                if candidates:
                    break
            if candidates:
                break
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved != resolved_base and not str(resolved).startswith(str(resolved_base) + os.sep):
            continue
        return resolved
    return None


def _read_local_lines(path: Path, max_bytes: int = MAX_TRANSCRIPT_BYTES,
                      max_lines: int = MAX_TRANSCRIPT_LINES) -> list[str]:
    lines: list[str] = []
    size = 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                size += len(raw)
                if size > max_bytes or len(lines) >= max_lines:
                    break
                lines.append(raw.decode(errors="replace"))
    except OSError:
        return []
    return lines


# Remote walker: same JSONL scan on the target host. argv: <base>.
# Prints {"sessions": [...]} capped at the newest 200 files.
_REMOTE_WALK_PY = r"""
import glob, json, os, sys
from datetime import datetime, timezone
base = os.path.realpath(os.path.expanduser(sys.argv[1]) if len(sys.argv) > 1 else "")
out = []
if base and os.path.isdir(base):
    files = []
    for path in glob.glob(os.path.join(base, "**/*.jsonl"), recursive=True):
        try:
            files.append((os.path.getmtime(path), path))
        except OSError:
            continue
    files.sort(reverse=True)
    for mtime, path in files[:200]:
        if os.path.realpath(path) != base and not os.path.realpath(path).startswith(base + os.sep):
            continue
        preview, project, agent = "", "", ""
        rel = os.path.relpath(path, base).split(os.sep)
        if rel and rel[0]:
            agent = rel[0]
        try:
            with open(path, "rb") as fh:
                data = fh.read(262144).decode(errors="replace")
        except OSError:
            data = ""
        for raw in data.splitlines():
            s = raw.strip()
            if not s.startswith("{"):
                continue
            try:
                obj = json.loads(s)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            for wrap in (obj, obj.get("payload") if isinstance(obj.get("payload"), dict) else {},
                         obj.get("message") if isinstance(obj.get("message"), dict) else {}):
                for key in ("cwd", "directory", "project", "workdir", "workspace"):
                    if isinstance(wrap.get(key), str) and wrap[key].startswith("/"):
                        project = wrap[key]
                        break
                if project:
                    break
            role = obj.get("role")
            if role != "user" and not (isinstance(obj.get("message"), dict)
                                       and obj["message"].get("role") == "user"):
                continue
            text = ""
            content = obj.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("text"))
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if not text and isinstance(msg.get("content"), str):
                text = msg["content"]
            if text.strip() and not text.lstrip().startswith(("<", "# AGENTS.md")):
                preview = text.strip()[:160].replace("\n", " ")
                break
        out.append({"session_id": os.path.splitext(os.path.basename(path))[0],
                    "agent": agent, "project": project,
                    "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "_mtime": mtime, "preview": preview, "_path": path})
print(json.dumps({"sessions": out}))
"""

# Remote cat: prints {"lines": [...]} capped at 200k lines / 8MB.
# abspath must resolve strictly under store (checked remotely too).
_REMOTE_CAT_PY = r"""
import json, os, sys
store = os.path.realpath(os.path.expanduser(sys.argv[1]))
path = os.path.realpath(sys.argv[2])
if path != store and not path.startswith(store + os.sep):
    sys.exit("path escapes store")
if not os.path.isfile(path):
    sys.exit("no such transcript")
lines = []
size = 0
with open(path, "rb") as fh:
    for raw in fh:
        size += len(raw)
        if size > 8 * 1024 * 1024 or len(lines) >= 200000:
            break
        lines.append(raw.decode(errors="replace"))
print(json.dumps({"lines": lines}))
"""


def _scan_remote(entry: dict[str, str], base: Path) -> list[dict[str, Any]] | None:
    target = entry.get("ssh") or entry["id"]
    try:
        proc = _run_bytes(["ssh", target, "python3", "-c", _REMOTE_WALK_PY, str(base)],
                          WALK_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode())["sessions"]
    except (ValueError, KeyError):
        return None


def _finalize(sessions: list[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    sessions.sort(key=lambda s: s.get("_mtime", 0), reverse=True)
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in sessions[:limit]]


def _run_log_tail(meta: dict[str, Any]) -> tuple[list[str], bool]:
    """Tail the run's output log plus its liveness, for local and remote runs."""
    run_dir = runs_base() / meta.get("run_id", "")
    if meta.get("remote"):
        target = meta.get("machine", "")
        try:
            proc = _run_bytes(["ssh", target, "tail", "-c", "12000",
                               f".opsdeck-community/agent-runs/{meta['run_id']}/output.log"],
                              SSH_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return [], False
        tail = proc.stdout.decode(errors="replace").splitlines()[-60:] if proc.returncode == 0 else []
        try:
            alive = _run_bytes(["ssh", target, "kill", "-0", str(meta.get("pid", 0))],
                               SSH_TIMEOUT).returncode == 0
        except (OSError, subprocess.SubprocessError):
            alive = False
        return tail, alive
    tail = _tail_lines(run_dir / "output.log", 60)
    try:
        alive = _pid_alive_local(int(meta.get("pid", 0)))
    except (TypeError, ValueError):
        alive = False
    return tail, alive


class LaunchRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=32)
    machine: str = Field(min_length=1, max_length=64)
    project: str = Field(min_length=1, max_length=512)
    model: str = Field(default="", max_length=100)
    effort: str = Field(default="", max_length=32)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class SendRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=32)
    machine: str = Field(min_length=1, max_length=64)
    session: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    model: str = Field(default="", max_length=100)
    effort: str = Field(default="", max_length=32)


def make_agents_router(require_token: Callable[..., None]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_token)])

    @router.get("/api/agents")
    def agents() -> dict[str, Any]:
        enabled = enabled_agents()
        defaults, per_machine = _parse_commands()
        models_cfg = agent_models_cfg()

        def _available(aid: str) -> bool:
            if aid not in enabled:
                return False
            if aid in defaults:
                return True
            return any(agent == aid for agent, _machine in per_machine)

        return {
            "configured": bool(enabled),
            "agents": [
                {
                    "id": aid,
                    "label": spec["label"],
                    "models": models_cfg.get(aid, []),
                    "efforts": spec["efforts"],
                    "machines": None,
                    "transcript": spec["transcript"],
                    "available": _available(aid),
                    "disabled_reason": (
                        None if _available(aid)
                        else (f"agent {aid} is not installed: set OPSDECK_AGENT_COMMANDS on the server"
                              if aid in enabled
                              else f"agent {aid} is not enabled: set OPSDECK_AGENTS on the server")
                    ),
                }
                for aid, spec in AGENT_SPECS.items()
            ],
        }

    @router.get("/api/machines")
    def machines() -> dict[str, Any]:
        return {
            "machines": [
                {"id": "local", "kind": "local", "label": "This server"},
                *[{"id": alias, "kind": "ssh", "label": alias}
                  for alias in configured_hosts() if alias != "local"],
            ]
        }

    @router.get("/api/projects")
    def projects(machine: str = Query(...)) -> dict[str, Any]:
        entry = _machine_entry(machine)
        if entry["kind"] == "local":
            roots = project_roots()
            root = roots.get(entry["id"])
            if root is None or not root.is_dir():
                return {"configured": False, "machine": entry["id"], "projects": []}
            try:
                names = sorted(p.name for p in root.iterdir()
                               if p.is_dir() and not p.name.startswith("."))
            except OSError as exc:
                raise HTTPException(status_code=502, detail=f"cannot list projects: {exc}") from exc
            return {"configured": True, "machine": entry["id"], "projects": names}
        roots = project_roots()
        if roots.get(entry["id"]) is None:
            return {"configured": False, "machine": entry["id"], "projects": []}
        target = entry.get("ssh") or entry["id"]
        try:
            proc = _run_bytes(["ssh", target, "python3", "-c",
                               "import json,os,sys;print(json.dumps(sorted("
                               "os.listdir(os.path.realpath(os.path.expanduser(sys.argv[1]))))))",
                               str(roots[entry["id"]])], SSH_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=502, detail=f"ssh to {target} failed: {exc}") from exc
        if proc.returncode != 0:
            return {"configured": False, "machine": entry["id"], "projects": []}
        try:
            names = [n for n in json.loads(proc.stdout.decode())
                     if isinstance(n, str) and n and not n.startswith(".")]
        except ValueError:
            raise HTTPException(status_code=502, detail=f"bad remote response from {target}") from None
        return {"configured": True, "machine": entry["id"], "projects": sorted(names)}

    @router.get("/api/models")
    def models(agent: str = Query(...), machine: str = Query(...)) -> dict[str, Any]:
        spec = _check_enabled(agent)
        entry = _machine_entry(machine)
        choices = agent_models_cfg().get(agent, [])
        command = agent_command_for(agent, entry["id"])
        return {"agent": agent, "machine": entry["id"],
                "configured": command is not None,
                "models": choices, "default": choices[0] if choices else "",
                "disabled_reason": (None if command else
                                    f"agent {agent} is not installed for machine {entry['id']}: "
                                    "set OPSDECK_AGENT_COMMANDS")}

    @router.get("/api/skills")
    def skills(agent: str = Query(...), machine: str = Query(...)) -> dict[str, Any]:
        _agent_spec(agent)
        entry = _machine_entry(machine)
        if entry["kind"] != "local":
            raise HTTPException(status_code=400, detail="skills are served from the server host only")
        base = skills_base()
        if base is None or not base.is_dir():
            return {"configured": False, "skills": []}
        found: list[dict[str, str]] = []
        try:
            files = sorted(p for p in base.rglob("*.md") if p.is_file())[:200]
        except OSError:
            files = []
        try:
            resolved_base = base.resolve()
        except OSError:
            return {"configured": False, "skills": []}
        for path in files:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved != resolved_base and not str(resolved).startswith(str(resolved_base) + os.sep):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
            first = next((ln.strip().lstrip("# ") for ln in text.splitlines() if ln.strip()), "")
            found.append({"name": path.stem, "kind": "skill", "description": first[:200], "hint": ""})
        return {"configured": True, "skills": found}

    @router.post("/api/agent/launch")
    def launch(req: LaunchRequest) -> dict[str, Any]:
        spec = _check_enabled(req.agent)
        entry = _machine_entry(req.machine)
        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message must not be empty")
        model, effort = _check_model_effort(spec, req.agent, req.model, req.effort)
        command = _command_or_503(req.agent, entry["id"])
        argv = _launch_argv(req.agent, command, model, effort, message)
        run_id = _spawn(req.agent, entry, req.project.strip(), argv, model, effort, None)
        return {"run_id": run_id, "session_id": None}

    @router.get("/api/agent/sessions")
    def sessions(agent: str = Query(...), machine: str = Query(...)) -> dict[str, Any]:
        spec = _check_enabled(agent)
        del spec
        entry = _machine_entry(machine)
        base = sessions_bases().get(entry["id"])
        if base is None or not base.is_dir():
            return {"configured": False, "sessions": []}
        if entry["kind"] == "local":
            return {"configured": True, "sessions": _finalize(_scan_store(base, agent))}
        found = _scan_remote(entry, base)
        if found is None:
            raise HTTPException(status_code=502, detail=f"machine {entry['id']} is unreachable")
        tagged = [{**{k: v for k, v in item.items() if not k.startswith("_")},
                   "agent": item.get("agent") or agent} for item in found]
        return {"configured": True, "sessions": _finalize(
            [{**t, "_mtime": next((i.get("_mtime", 0) for i in found
                                   if i.get("session_id") == t.get("session_id")), 0)}
             for t in tagged])}

    @router.get("/api/agent/recent")
    def recent(days: int = Query(5, ge=1, le=30),
               agents: str = Query(""), machines: str = Query("")) -> dict[str, Any]:
        agent_ids = [a.strip().lower() for a in agents.split(",") if a.strip()]
        machine_ids = [m.strip() for m in machines.split(",") if m.strip()]
        enabled = enabled_agents()
        if agent_ids:
            for aid in agent_ids:
                _agent_spec(aid)
        else:
            agent_ids = list(enabled)
        if machine_ids:
            entries = {mid: _machine_entry(mid) for mid in machine_ids}
        else:
            machine_ids = ["local", *(a for a in configured_hosts() if a != "local")]
            entries = {"local": {"id": "local", "kind": "local"},
                       **{a: {"id": a, "kind": "ssh", "ssh": a}
                          for a in configured_hosts() if a != "local"}}
        bases = sessions_bases()
        pairs = [(aid, entries[mid]) for aid in agent_ids for mid in machine_ids
                 if mid in entries and aid in enabled]

        def scan_pair(aid: str, entry: dict[str, str]) -> list[dict[str, Any]] | None:
            base = bases.get(entry["id"])
            if base is None or not base.is_dir():
                return []
            if entry["kind"] == "local":
                return [s for s in _scan_store(base, aid) if not s.get("agent") or s.get("agent") == aid]
            found = _scan_remote(entry, base)
            if found is None:
                return None
            return [s for s in found if not s.get("agent") or s.get("agent") == aid or s.get("agent") == ""]

        found: dict[tuple[str, str], list[dict[str, Any]]] = {}
        machine_ok: dict[str, bool] = {e["id"]: False for _aid, e in pairs}
        if pairs:
            pool = ThreadPoolExecutor(max_workers=min(8, len(pairs)), thread_name_prefix="recent-scan")
            futures = {pool.submit(scan_pair, aid, entry): (aid, entry) for aid, entry in pairs}
            done, pending = futures_wait(futures, timeout=RECENT_TOTAL_TIMEOUT)
            for fut in done:
                aid, entry = futures[fut]
                try:
                    items = fut.result()
                except Exception:
                    items = None
                if items is not None:
                    found[(aid, entry["id"])] = items
                    machine_ok[entry["id"]] = True
            for fut in pending:
                fut.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
        # Phone-launched runs live in the local run store for every machine.
        run_items: list[dict[str, Any]] = []
        try:
            metas = sorted((p for p in runs_base().iterdir() if p.is_dir()),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:200]
        except OSError:
            metas = []
        for rundir in metas:
            try:
                meta = json.loads((rundir / "meta.json").read_text(encoding="utf-8"))
                mtime = (rundir / "meta.json").stat().st_mtime
            except (OSError, ValueError):
                continue
            if meta.get("agent") not in agent_ids or meta.get("machine") not in machine_ids:
                continue
            run_items.append({
                "session_id": meta.get("session") or meta.get("run_id", ""),
                "run_id": meta.get("run_id", ""),
                "agent": meta.get("agent", ""),
                "machine": meta.get("machine", ""),
                "project": meta.get("project", ""),
                "preview": "",
                "updated_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                "_mtime": mtime,
            })
            machine_ok.setdefault(meta.get("machine", ""), True)
        unreachable = sorted(mid for mid, ok in machine_ok.items() if not ok)
        cutoff = time.time() - days * 86400

        def _mtime_of(item: dict[str, Any]) -> float:
            raw = item.get("_mtime")
            if isinstance(raw, (int, float)) and raw > 0:
                return float(raw)
            stamp = item.get("updated_at")
            if isinstance(stamp, str) and stamp:
                try:
                    return datetime.fromisoformat(stamp).timestamp()
                except ValueError:
                    return 0.0
            return 0.0

        merged: list[tuple[float, dict[str, Any]]] = []
        for (aid, mid), items in found.items():
            for item in items:
                mtime = _mtime_of(item)
                if mtime < cutoff:
                    continue
                clean = {k: v for k, v in item.items() if not k.startswith("_")}
                clean["agent"] = aid
                clean["machine"] = mid
                merged.append((mtime, clean))
        for item in run_items:
            # Phone-launched runs stay visible while fresh; a run with no
            # session id yet (still starting) always shows.
            if _mtime_of(item) >= cutoff or not item.get("session_id"):
                clean = {k: v for k, v in item.items() if not k.startswith("_")}
                merged.append((_mtime_of(item), clean))
        merged.sort(key=lambda pair: pair[0], reverse=True)
        return {"days": days, "agents": agent_ids, "machines": machine_ids,
                "configured": any(b.is_dir() for b in bases.values()),
                "sessions": [item for _mtime, item in merged[:RECENT_MAX_SESSIONS]],
                "unreachable": unreachable}

    @router.get("/api/agent/transcript")
    def transcript(agent: str = Query(...), machine: str = Query(...),
                   session: str = Query(...)) -> dict[str, Any]:
        spec = _check_enabled(agent)
        entry = _machine_entry(machine)
        _check_session(session)
        if not spec["transcript"]:
            payload: dict[str, Any] = {
                "message": (f"no transcript: {agent} keeps no retrievable plain-text log; "
                            "the run output log is shown instead"),
            }
            linked = _linked_run(entry["id"], agent, session)
            if linked is not None:
                tail, alive = _run_log_tail(linked)
                payload["log_tail"] = tail
                payload["running"] = alive
            raise HTTPException(status_code=409, detail=payload)
        if entry["kind"] == "local":
            path = _find_local_transcript(agent, session)
            if path is None:
                raise HTTPException(status_code=404, detail="session not found")
            lines = _read_local_lines(path)
        else:
            base = sessions_bases().get(entry["id"])
            if base is None:
                raise HTTPException(status_code=404, detail="session not found")
            target = entry.get("ssh") or entry["id"]
            try:
                proc = _run_bytes(["ssh", target, "python3", "-c", _REMOTE_WALK_PY, str(base)],
                                  WALK_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as exc:
                raise HTTPException(status_code=502, detail=f"ssh to {target} failed: {exc}") from exc
            if proc.returncode != 0:
                raise HTTPException(status_code=502, detail=f"remote scan failed on {target}")
            try:
                items = json.loads(proc.stdout.decode())["sessions"]
            except (ValueError, KeyError) as exc:
                raise HTTPException(status_code=502, detail=f"bad remote response from {target}") from exc
            rel = ""
            for item in items:
                if item.get("session_id") == session and isinstance(item.get("_path"), str):
                    rel = item["_path"]
                    break
            if not rel:
                raise HTTPException(status_code=404, detail="session not found")
            try:
                proc = _run_bytes(["ssh", target, "python3", "-c", _REMOTE_CAT_PY,
                                   str(base), rel], WALK_TIMEOUT)
            except (OSError, subprocess.SubprocessError) as exc:
                raise HTTPException(status_code=502, detail=f"ssh to {target} failed: {exc}") from exc
            if proc.returncode != 0:
                raise HTTPException(status_code=404, detail="session not found")
            try:
                lines = json.loads(proc.stdout.decode())["lines"]
            except (ValueError, KeyError) as exc:
                raise HTTPException(status_code=502, detail=f"bad remote response from {target}") from exc
        messages = _normalize_lines(lines)
        linked = _linked_run(entry["id"], agent, session)
        running = False
        log_tail: list[str] = []
        if linked is not None:
            log_tail, running = _run_log_tail(linked)
        return {"messages": messages, "running": running, "log_tail": log_tail}

    @router.get("/api/agent/run")
    def run(run_id: str = Query(...)) -> dict[str, Any]:
        _check_run_id(run_id)
        meta = _read_meta(run_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="run not found")
        tail, alive = _run_log_tail(meta)
        return {"run_id": run_id, "agent": meta.get("agent", ""),
                "machine": meta.get("machine", ""), "project": meta.get("project", ""),
                "session": meta.get("session") or "", "running": alive, "log_tail": tail}

    @router.post("/api/agent/send")
    def send(req: SendRequest) -> dict[str, Any]:
        spec = _check_enabled(req.agent)
        entry = _machine_entry(req.machine)
        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message must not be empty")
        session = _check_session(req.session.strip())
        model, effort = _check_model_effort(spec, req.agent, req.model, req.effort)
        if not spec["resume"]:
            raise HTTPException(status_code=409, detail=(
                f"no resume: {req.agent} keeps no retrievable transcript; "
                "start a new run with POST /api/agent/launch instead"))
        command = _command_or_503(req.agent, entry["id"])
        project = ""
        roots = project_roots()
        root = roots.get(entry["id"])
        if entry["kind"] == "local":
            path = _find_local_transcript(req.agent, session)
            if path is not None:
                head = _head_project(_peek_json_objects(path, max_lines=10))
                if head and root is not None:
                    try:
                        project = Path(head).resolve().relative_to(root.resolve()).as_posix()
                    except (OSError, ValueError):
                        project = ""
        else:
            base = sessions_bases().get(entry["id"])
            if base is not None:
                found = _scan_remote(entry, base) or []
                for item in found:
                    proj = item.get("project") or ""
                    if item.get("session_id") != session or not proj.startswith("/"):
                        continue
                    if root is not None:
                        try:
                            rel = Path(proj).resolve().relative_to(root.resolve()).as_posix()
                        except (OSError, ValueError):
                            continue
                        project = rel
                        break
                    project = proj.strip("/").split("/")[0] or ""
                    break
        if not project:
            linked = _linked_run(entry["id"], req.agent, session)
            if linked and isinstance(linked.get("project"), str):
                project = linked["project"]
        if not project:
            raise HTTPException(status_code=409, detail=(
                "cannot determine the session's project directory; "
                "start a new run with POST /api/agent/launch instead"))
        argv = _resume_argv(req.agent, command, model, effort, session, message)
        run_id = _spawn(req.agent, entry, project, argv, model, effort, session)
        return {"run_id": run_id}

    return router
