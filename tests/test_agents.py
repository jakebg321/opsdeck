"""Exercise the Agents tab and Briefings backends over a real uvicorn app.

Follows the repository's existing integration-test style: the real app is
driven over urllib with temp directories, a stub agent CLI (a shell script
that records its argv, never a paid CLI), and mocked session/skill/briefing
stores. Remote ssh hosts are never contacted: OPSDECK_SSH_HOSTS is empty in
every server environment, so only the local machine is exercised.
"""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.agents as agents_mod  # noqa: E402


def _free_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


class _Server:
    """A real uvicorn app with an isolated HOME and caller-supplied env."""

    def __init__(self, env_extra):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.port = _free_port()
        self.token = "agents-test-token-0123456789abcdef"
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "OPSDECK_TOKEN": self.token,
            "OPSDECK_SSH_HOSTS": "",
            **env_extra,
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--no-access-log"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/hello", timeout=0.2).read()
                    break
                except Exception:
                    if self.process.poll() is not None:
                        self.fail("test server exited before it was ready")
                    time.sleep(0.1)
            else:
                self.fail("test server did not become ready")
        except Exception:
            self.close()
            raise

    def fail(self, message):
        raise AssertionError(message)

    def request(self, method, path, body=None, token="__default__"):
        headers = {}
        if token == "__default__":
            headers["Authorization"] = f"Bearer {self.token}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                         data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                ctype = response.headers.get("Content-Type", "")
                return response.status, (json.loads(raw) if "json" in ctype else raw), \
                    dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw), dict(exc.headers or {})
            except ValueError:
                return exc.code, {"raw": raw.decode(errors="replace")}, dict(exc.headers or {})

    def close(self):
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.temporary.cleanup()


def _stub_cli(directory, log_name="argv.log"):
    """An executable stand-in for a paid agent CLI: records argv, prints a line."""
    stub = directory / "fake-claude"
    log = directory / log_name
    stub.write_text(f"#!/bin/sh\necho \"stub-output $@\"\necho \"$@\" >> {log}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub, log


class AgentsUnconfiguredTest(unittest.TestCase):
    def test_setup_state_and_auth(self):
        server = _Server({})
        try:
            status, _, _ = server.request("GET", "/api/agents", token=None)
            self.assertEqual(status, 401)
            status, _, _ = server.request("GET", "/api/agents", token="wrong-token")
            self.assertEqual(status, 401)
            status, payload, _ = server.request("GET", "/api/agents")
            self.assertEqual(status, 200)
            self.assertFalse(payload["configured"])
            self.assertEqual(len(payload["agents"]), len(agents_mod.AGENT_SPECS))
            self.assertTrue(all(entry["available"] is False for entry in payload["agents"]))
            status, payload, _ = server.request("GET", "/api/projects?machine=local")
            self.assertEqual(status, 200)
            self.assertFalse(payload["configured"])
            status, payload, _ = server.request("GET", "/api/machines")
            self.assertEqual(status, 200)
            self.assertEqual(payload["machines"], [{"id": "local", "kind": "local",
                                                    "label": "This server"}])
            status, payload, _ = server.request("GET", "/api/briefings")
            self.assertEqual(status, 200)
            self.assertFalse(payload["configured"])
            # Launch is disabled before any agent is enabled: explicit 400.
            status, payload, _ = server.request(
                "POST", "/api/agent/launch",
                {"agent": "claude", "machine": "local", "project": "demo",
                 "message": "hello"})
            self.assertEqual(status, 400)
            self.assertIn("OPSDECK_AGENTS", json.dumps(payload))
        finally:
            server.close()


class AgentsConfinementTest(unittest.TestCase):
    def test_project_and_session_confinement(self):
        base = Path(tempfile.mkdtemp())
        roots = base / "roots"
        (roots / "proj").mkdir(parents=True)
        stub, _log = _stub_cli(base)
        server = _Server({"OPSDECK_AGENTS": "claude",
                          "OPSDECK_AGENT_COMMANDS": f"claude={stub}",
                          "OPSDECK_PROJECT_ROOTS": str(roots)})
        try:
            good = {"agent": "claude", "machine": "local", "project": "proj",
                    "message": "hello"}
            status, payload, _ = server.request("POST", "/api/agent/launch", good)
            self.assertEqual(status, 200)
            self.assertTrue(payload["run_id"])
            for bad_project in ("..", "/abs", "a/../../b", "a\\b", ".hidden", "a/./../b"):
                status, payload, _ = server.request(
                    "POST", "/api/agent/launch",
                    {"agent": "claude", "machine": "local", "project": bad_project,
                     "message": "hello"})
                self.assertIn(status, (400, 404), bad_project)
            status, _, _ = server.request("POST", "/api/agent/launch",
                                          {"agent": "claude", "machine": "local",
                                           "project": "missing", "message": "hello"})
            self.assertEqual(status, 404)
            for bad in ("../x", "/abs", "a/b", ""):
                status, _, _ = server.request(
                    "GET", f"/api/agent/transcript?agent=claude&machine=local&session={bad or ' '}")
                self.assertIn(status, (400, 404), repr(bad))
            status, _, _ = server.request("GET", "/api/agent/run?run_id=../x")
            self.assertEqual(status, 400)
            status, _, _ = server.request(
                "POST", "/api/agent/send",
                {"agent": "claude", "machine": "local", "session": "../x",
                 "message": "hi"})
            self.assertEqual(status, 400)
            # Unknown machines are rejected, never passed to ssh.
            status, _, _ = server.request("GET", "/api/projects?machine=nope")
            self.assertEqual(status, 400)
        finally:
            server.close()


class AgentsLaunchTranscriptTest(unittest.TestCase):
    def test_launch_run_transcript_and_resume(self):
        base = Path(tempfile.mkdtemp())
        roots = base / "roots"
        project = roots / "projA"
        project.mkdir(parents=True)
        sessions = base / "sessions" / "claude"
        sessions.mkdir(parents=True)
        (sessions / "sess-1.jsonl").write_text(
            json.dumps({"role": "user", "content": "first question",
                        "cwd": str(project)}) + "\n"
            + json.dumps({"message": {"role": "assistant",
                                      "content": [{"type": "text", "text": "first answer"}]},
                        "cwd": str(project)}) + "\n")
        skills = base / "skills"
        skills.mkdir()
        (skills / "review.md").write_text("# Review\nLook at the diff.\n")
        stub, log = _stub_cli(base)
        server = _Server({"OPSDECK_AGENTS": "claude",
                          "OPSDECK_AGENT_COMMANDS": f"claude={stub}",
                          "OPSDECK_AGENT_MODELS": "claude=opus,sonnet",
                          "OPSDECK_PROJECT_ROOTS": str(roots),
                          "OPSDECK_AGENT_SESSIONS_DIR": str(base / "sessions"),
                          "OPSDECK_SKILLS_DIR": str(skills)})
        try:
            status, payload, _ = server.request(
                "GET", "/api/agent/sessions?agent=claude&machine=local")
            self.assertEqual(status, 200)
            self.assertTrue(payload["configured"])
            self.assertEqual(len(payload["sessions"]), 1)
            self.assertEqual(payload["sessions"][0]["preview"], "first question")
            status, payload, _ = server.request(
                "GET", "/api/agent/transcript?agent=claude&machine=local&session=sess-1")
            self.assertEqual(status, 200)
            self.assertFalse(payload["running"])
            self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant"])
            self.assertEqual(payload["messages"][1]["text"], "first answer")
            status, payload, _ = server.request("GET", "/api/skills?agent=claude&machine=local")
            self.assertEqual(status, 200)
            self.assertTrue(payload["configured"])
            self.assertEqual(payload["skills"][0]["name"], "review")
            status, _, _ = server.request("GET", "/api/skills?agent=claude&machine=remote-x")
            self.assertEqual(status, 400)
            status, payload, _ = server.request(
                "GET", "/api/models?agent=claude&machine=local")
            self.assertEqual(status, 200)
            self.assertTrue(payload["configured"])
            self.assertEqual(payload["models"], ["opus", "sonnet"])
            # Launch through the stub CLI: argv recorded, output in the run log.
            status, payload, _ = server.request(
                "POST", "/api/agent/launch",
                {"agent": "claude", "machine": "local", "project": "projA",
                 "message": "hello stub"})
            self.assertEqual(status, 200)
            run_id = payload["run_id"]
            tail = []
            for _ in range(50):
                status, payload, _ = server.request("GET", f"/api/agent/run?run_id={run_id}")
                self.assertEqual(status, 200)
                tail = payload["log_tail"]
                if not payload["running"]:
                    break
                time.sleep(0.1)
            self.assertTrue(any("stub-output" in line for line in tail))
            self.assertIn("hello stub", log.read_text())
            # Resume reuses the session's project and passes the resume flags.
            status, payload, _ = server.request(
                "POST", "/api/agent/send",
                {"agent": "claude", "machine": "local", "session": "sess-1",
                 "message": "follow up"})
            self.assertEqual(status, 200)
            self.assertTrue(payload["run_id"])
            last = log.read_text().strip().splitlines()[-1]
            self.assertIn("--resume", last)
            self.assertIn("sess-1", last)
            self.assertIn("follow up", last)
        finally:
            server.close()


class AgentsConfigParsingTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_per_machine_commands_and_dirs(self):
        os.environ["OPSDECK_AGENT_COMMANDS"] = (
            "claude=/usr/local/bin/claude,claude@lab=/home/op/bin/claude,"
            "codex=relative/path,unknown=/bin/x,bad name@lab=/bin/y")
        self.assertEqual(agents_mod.agent_commands(), {"claude": "/usr/local/bin/claude"})
        self.assertEqual(agents_mod.agent_command_for("claude", "local"),
                         "/usr/local/bin/claude")
        self.assertEqual(agents_mod.agent_command_for("claude", "lab"),
                         "/home/op/bin/claude")
        self.assertIsNone(agents_mod.agent_command_for("codex", "local"))
        # The default applies to every machine without an override.
        self.assertEqual(agents_mod.agent_command_for("claude", "other"),
                         "/usr/local/bin/claude")
        os.environ["OPSDECK_AGENT_SESSIONS_DIR"] = "/srv/sessions,lab=/data/sessions"
        bases = agents_mod.sessions_bases()
        self.assertEqual(bases["local"], Path("/srv/sessions"))
        self.assertEqual(bases["lab"], Path("/data/sessions"))
        os.environ["OPSDECK_PROJECT_ROOTS"] = "/srv/projects,lab=/data/projects"
        roots = agents_mod.project_roots()
        self.assertEqual(roots["local"], Path("/srv/projects"))
        self.assertEqual(roots["lab"], Path("/data/projects"))
        os.environ["OPSDECK_AGENTS"] = "claude, nosuchagent, codex, claude"
        self.assertEqual(agents_mod.enabled_agents(), ["claude", "codex"])

    def test_normalize_transcript_shapes(self):
        lines = [
            json.dumps({"message": {"role": "user", "content": "q"},
                        "cwd": "/srv/projects/demo"}),
            json.dumps({"message": {"role": "assistant",
                                    "content": [{"type": "text", "text": "a"}]}}),
            json.dumps({"type": "function_call", "name": "bash",
                        "arguments": "{\"cmd\":\"ls\"}"}),
            "not json at all",
        ]
        messages = agents_mod._normalize_lines(lines)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool"])
        self.assertEqual(messages[1]["text"], "a")
        self.assertIn("bash", messages[2]["text"])
        self.assertEqual(agents_mod._head_project(
            [{"message": {"role": "user"}, "cwd": "/srv/projects/demo"}]),
            "/srv/projects/demo")


class AgentArgvShapeTest(unittest.TestCase):
    def test_verified_cli_argument_shapes(self):
        launch_cases = {
            "claude": ["/bin/claude", "--model", "sonnet", "--effort", "high", "-p", "hello"],
            "codex": ["/bin/codex", "exec", "-m", "model-id", "-c",
                      "model_reasoning_effort=high", "hello"],
            "kimi": ["/bin/kimi", "-m", "model-id", "-p", "hello"],
            "qwen": ["/bin/qwen", "-m", "model-id", "-p", "hello"],
            "muse": ["/bin/muse", "exec", "--model", "model-id",
                     "--reasoning-effort", "medium", "hello"],
            "glm": ["/bin/glm-adapter", "hello"],
        }
        for agent, expected in launch_cases.items():
            with self.subTest(agent=agent):
                model = "" if agent == "glm" else ("sonnet" if agent == "claude" else "model-id")
                effort = "high" if agent in ("claude", "codex") else (
                    "medium" if agent == "muse" else "")
                command = "/bin/glm-adapter" if agent == "glm" else f"/bin/{agent}"
                self.assertEqual(agents_mod._launch_argv(agent, command, model, effort,
                                                         "hello"), expected)
        self.assertEqual(
            agents_mod._resume_argv("claude", "/bin/claude", "", "", "session-1", "reply"),
            ["/bin/claude", "--resume", "session-1", "-p", "reply"])
        self.assertEqual(
            agents_mod._resume_argv("codex", "/bin/codex", "", "high", "session-1", "reply"),
            ["/bin/codex", "exec", "resume", "session-1", "-c",
             "model_reasoning_effort=high", "reply"])
        self.assertEqual(
            agents_mod._resume_argv("kimi", "/bin/kimi", "", "", "session-1", "reply"),
            ["/bin/kimi", "-S", "session-1", "-p", "reply"])
        self.assertEqual(
            agents_mod._resume_argv("qwen", "/bin/qwen", "", "", "session-1", "reply"),
            ["/bin/qwen", "-r", "session-1", "-p", "reply"])
        with self.assertRaises(Exception):
            agents_mod._launch_argv("glm", "/bin/glm-adapter", "model-id", "", "hello")


class BriefingsTest(unittest.TestCase):
    def test_list_audio_auth_and_path_confinement(self):
        base = Path(tempfile.mkdtemp())
        store = base / "briefings"
        store.mkdir()
        outside = base / "secret.txt"
        outside.write_text("do not leak")
        (store / "brief-1.json").write_text(json.dumps(
            {"id": "brief-1", "title": "Morning", "agent": "claude"}))
        (store / "brief-1.mp3").write_bytes(b"ID3fake-audio-bytes")
        (store / "brief-1.txt").write_text("  spoken words  ")
        (store / "mismatch.json").write_text(json.dumps({"id": "other"}))
        (store / "noaudio.json").write_text(json.dumps({"id": "noaudio"}))
        (store / "evil.json").write_text(json.dumps({"id": "evil"}))
        (store / "evil.mp3").write_bytes(b"ID3evil")
        (store / "evil.txt").symlink_to(outside)
        server = _Server({"OPSDECK_BRIEFINGS_DIR": str(store)})
        try:
            status, payload, _ = server.request("GET", "/api/briefings")
            self.assertEqual(status, 200)
            self.assertTrue(payload["configured"])
            by_id = {item["id"]: item for item in payload["briefings"]}
            # Mismatched sidecars and entries without audio are skipped.
            self.assertEqual(sorted(by_id), ["brief-1", "evil"])
            self.assertEqual(by_id["brief-1"]["text"], "spoken words")
            # A transcript symlink pointing outside the store is never read.
            self.assertEqual(by_id["evil"].get("text", ""), "")
            audio = "/api/briefings/brief-1/audio"
            status, _, _ = server.request("GET", audio, token=None)
            self.assertEqual(status, 401)
            status, _, _ = server.request("GET", audio, token="wrong")
            self.assertEqual(status, 401)
            status, body, headers = server.request("GET", audio)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"ID3fake-audio-bytes")
            lowered = {key.lower(): value for key, value in headers.items()}
            self.assertIn("audio/mpeg", lowered.get("content-type", ""))
            status, _, _ = server.request("GET", "/api/briefings/noaudio/audio")
            self.assertEqual(status, 404)
            status, _, _ = server.request("GET", "/api/briefings/a%20b/audio")
            self.assertEqual(status, 400)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
