"""Portable local status collector for the showcase copy."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HOST_PROBE_SECONDS = 30
MAX_REMOTE_HOSTS = 8
_host_probe_cache: dict[str, tuple[float, bool]] = {}
_host_probe_lock = threading.Lock()


def configured_hosts() -> list[str]:
    aliases = [part.strip() for part in os.environ.get("OPSDECK_SSH_HOSTS", "").split(",")]
    hosts = ["local"]
    for alias in aliases:
        if alias and not alias.startswith("-") and HOST_RE.fullmatch(alias) and alias not in hosts:
            hosts.append(alias)
            if len(hosts) - 1 >= MAX_REMOTE_HOSTS:
                break
    return hosts


def _probe_ssh(alias: str) -> bool:
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
             "-o", "ConnectionAttempts=1", alias, "hostname"],
            capture_output=True, check=False, timeout=4,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _host_states(hosts: list[str]) -> dict[str, bool]:
    now = time.monotonic()
    aliases = hosts[1:]
    with _host_probe_lock:
        pending = [alias for alias in aliases
                   if alias not in _host_probe_cache
                   or now - _host_probe_cache[alias][0] >= HOST_PROBE_SECONDS]
    if pending:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
            results = dict(zip(pending, pool.map(_probe_ssh, pending)))
        with _host_probe_lock:
            for alias, online in results.items():
                _host_probe_cache[alias] = (now, online)
    with _host_probe_lock:
        return {alias: _host_probe_cache[alias][1] for alias in aliases}


def _gpu_rows() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    gpus = []
    by_uuid = {}
    for index, row in enumerate(csv.reader(io.StringIO(result.stdout), skipinitialspace=True)):
        if len(row) != 6:
            continue
        try:
            uuid, name, used, total, util, temp = [field.strip() for field in row]
            gpu = {
                "label": f"GPU {index} · local", "name": name,
                "used_mb": int(used), "total_mb": int(total),
                "util_pct": int(util), "temp_c": int(temp),
                "history": [], "procs": [],
            }
            gpus.append(gpu)
            by_uuid[uuid] = gpu
        except ValueError:
            continue

    if not gpus:
        return []
    try:
        processes = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return gpus
    if processes.returncode:
        return gpus
    for row in csv.reader(io.StringIO(processes.stdout), skipinitialspace=True):
        if len(row) != 4:
            continue
        uuid, pid, name, memory = [field.strip() for field in row]
        gpu = by_uuid.get(uuid)
        if gpu is None:
            continue
        try:
            process_id = int(pid)
            memory_mb = int(memory) if memory not in {"N/A", "[N/A]"} else 0
        except ValueError:
            continue
        gpu["procs"].append({
            "pid": process_id, "name": os.path.basename(name) or name,
            "mem_mb": memory_mb,
        })
    return gpus


def _local_metrics() -> dict[str, Any]:
    """Small, read-only server snapshot without a monitoring dependency."""
    metrics: dict[str, Any] = {}
    try:
        metrics["load_1m"] = round(os.getloadavg()[0], 2)
        metrics["cpu_cores"] = os.cpu_count()
    except OSError:
        pass
    try:
        values = {}
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                key, _, value = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(value.strip().split()[0]) // 1024
        if "MemTotal" in values and "MemAvailable" in values:
            metrics["memory_total_mb"] = values["MemTotal"]
            metrics["memory_used_mb"] = max(0, values["MemTotal"] - values["MemAvailable"])
    except (OSError, ValueError, IndexError):
        pass
    try:
        disk = shutil.disk_usage(Path.home())
        metrics["disk_total_mb"] = disk.total // (1024 * 1024)
        metrics["disk_used_mb"] = disk.used // (1024 * 1024)
    except OSError:
        pass
    return metrics


def build_payload() -> dict[str, Any]:
    hosts = configured_hosts()
    remote = _host_states(hosts)
    payload = {
        "ts": int(time.time()),
        "local_metrics": _local_metrics(),
        "gpus": _gpu_rows(),
        "hosts": [{
            "label": host,
            "state": "online" if host == "local" or remote.get(host) else "offline",
            "detail": "server host" if host == "local" else
                      ("SSH available" if remote.get(host) else "SSH unavailable"),
        } for host in hosts],
        "agents": [],
        "ollama": [],
        "sample": False,
    }
    # An operator can supply the same rich Fleet payload as a private install
    # without publishing their collector, hostnames, sessions, or telemetry.
    source = os.environ.get("OPSDECK_STATUS_FILE", "").strip()
    if not source:
        return payload
    try:
        path = Path(source).expanduser()
        if path.stat().st_size > 2_000_000:
            raise ValueError("status file is too large")
        extra = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(extra, dict):
            raise ValueError("status file must contain an object")
        for field in ("gpus", "hosts", "agents", "ollama"):
            if field in extra and not isinstance(extra[field], list):
                raise ValueError(f"{field} must be a list")
        for field in ("gpus", "hosts", "agents", "ollama", "local_metrics", "ts", "sample"):
            if field in extra:
                payload[field] = extra[field]
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        payload["refresh_error"] = f"Configured status file unavailable: {type(exc).__name__}"
    if payload["sample"]:
        payload["ts"] = int(time.time())
    return payload
