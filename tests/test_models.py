"""Exercise the Models tab backend: config parsing, auth, and a stub endpoint.

Follows the repository's existing integration-test style: a stub
Ollama-compatible HTTP server plus the real uvicorn app, driven over
urllib. No new test dependencies.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def _free_port():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


class _StubOllama(BaseHTTPRequestHandler):
    seen_unload = None
    seen_generate = None

    def log_message(self, *args):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            body = {}
        if self.path == "/api/generate" and body.get("keep_alive") == 0:
            type(self).seen_unload = body
            self._send({})
        elif self.path == "/api/generate":
            type(self).seen_generate = body
            self._send({"response": "hello there",
                        "prompt_eval_count": 3, "eval_count": 2})
        else:
            self.send_error(404)


class _BlockingOllama(_StubOllama):
    """Holds every /api/generate call until the test releases the gate."""

    gate = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            body = {}
        if self.path == "/api/generate":
            type(self).gate.wait(timeout=20)
            self._send({"response": "slow answer",
                        "prompt_eval_count": 1, "eval_count": 1})
        else:
            self.send_error(404)


class ModelsConfigTest(unittest.TestCase):
    def _providers(self, value):
        from unittest.mock import patch
        with patch.dict(os.environ, {"OPSDECK_MODELS": value}):
            import server.models as models
            return models.configured_providers()

    def test_empty_config_means_unconfigured(self):
        self.assertEqual(self._providers(""), [])

    def test_bad_entries_are_skipped_not_fatal(self):
        providers = self._providers(
            "ok=http://127.0.0.1:11434,bad-no-equals,evil=ftp://x,"
            "cred=http://user@host:11434,dup=http://a,dup=http://b")
        self.assertEqual([p["id"] for p in providers], ["ok", "dup"])

    def test_request_models_carry_no_url_field(self):
        import server.models as models
        for name in ("SubmitRequest", "UnloadRequest"):
            fields = set(getattr(models, name).model_fields)
            self.assertNotIn("url", fields)
            self.assertNotIn("endpoint", fields)
            self.assertNotIn("base_url", fields)


class ModelsIntegrationTest(unittest.TestCase):
    def test_providers_status_submit_history_unload(self):
        stub = HTTPServer(("127.0.0.1", 0), _StubOllama)
        stub_port = stub.server_address[1]
        stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
        stub_thread.start()

        port = _free_port()
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
        token = "models-integration-test-token-12345678"
        environment = {**os.environ, "OPSDECK_TOKEN": token,
                       "OPSDECK_SSH_HOSTS": "",
                       "OPSDECK_DOCS_DIR": str(home / "docs"),
                       "OPSDECK_MODELS": f"desk=http://127.0.0.1:{stub_port}",
                       "HOME": str(home)}
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/hello", timeout=0.2).read()
                    break
                except Exception:
                    if server.poll() is not None:
                        self.fail("test server exited before it was ready")
                    time.sleep(0.1)
            else:
                self.fail("test server did not become ready")

            def api(path, body=None, authorized=True):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={"Content-Type": "application/json", **(
                        {"Authorization": f"Bearer {token}"} if authorized else {})},
                    method="POST" if body is not None else "GET")
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode())

            catalog = api("/api/models/providers")[1]
            self.assertTrue(catalog["configured"])
            self.assertEqual([p["id"] for p in catalog["providers"]], ["desk"])
            self.assertEqual(catalog["providers"][0]["kind"], "ollama")
            self.assertTrue(catalog["providers"][0]["unload"])
            self.assertNotIn("base_url", catalog["providers"][0])
            self.assertNotIn("command", catalog["providers"][0])

            status_code, status = api("/api/models/status?provider=desk")
            self.assertEqual(status_code, 200)
            self.assertTrue(status["ok"])
            self.assertEqual(status["models"], ["llama3.1:8b", "qwen2.5:7b"])

            try:
                api("/api/models/status?provider=desk", authorized=False)
                self.fail("unauthenticated request was allowed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401)

            try:
                api("/api/models/status?provider=nope")
                self.fail("unknown provider was allowed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

            _, submit = api("/api/models/submit", {
                "provider": "desk", "model": "llama3.1:8b", "prompt": "Say hi"})
            job_id = submit["job_id"]
            result = None
            for _ in range(100):
                _, result = api(f"/api/models/jobs/{job_id}")
                if result.get("state") == "done":
                    break
                time.sleep(0.1)
            self.assertEqual(result["state"], "done")
            self.assertEqual(result["answer"], "hello there")
            self.assertEqual(result["prompt_tokens"], 3)

            try:
                api("/api/models/submit", {
                    "provider": "desk", "model": "evil; rm -rf ~", "prompt": "hi"})
                self.fail("bad model name was allowed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

            import base64 as _base64
            tiny_png = _base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
            _, image_submit = api("/api/models/submit", {
                "provider": "desk", "model": "llama3.1:8b", "prompt": "Look at this",
                "image_base64": "data:image/png;base64," + tiny_png})
            image_job = image_submit["job_id"]
            for _ in range(100):
                _, image_result = api(f"/api/models/jobs/{image_job}")
                if image_result.get("state") == "done":
                    break
                time.sleep(0.1)
            self.assertEqual(image_result["state"], "done")
            self.assertEqual(_StubOllama.seen_generate["images"], [tiny_png])

            try:
                api("/api/models/submit", {
                    "provider": "desk", "model": "llama3.1:8b", "prompt": "hi",
                    "image_base64": _base64.b64encode(b"GIF89a" + b"\x00" * 64).decode()})
                self.fail("non-PNG/JPEG image was allowed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

            import server.models as _models
            try:
                api("/api/models/submit", {
                    "provider": "desk", "model": "llama3.1:8b", "prompt": "hi",
                    "image_base64": "A" * (_models.MAX_IMAGE_B64_CHARS + 65)})
                self.fail("oversized image was allowed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 422)

            try:
                api("/api/models/jobs/%2E%2E")
                self.fail("bad job id was allowed")
            except urllib.error.HTTPError as exc:
                self.assertIn(exc.code, (400, 404))

            _, history = api("/api/models/history")
            self.assertTrue(any(run["prompt"] == "Say hi"
                               for run in history["runs"]))
            self.assertTrue(any(run["prompt"] == "Look at this" and run["has_image"]
                               for run in history["runs"]))

            _, unload = api("/api/models/unload", {
                "provider": "desk", "model": "llama3.1:8b"})
            self.assertEqual(unload, {"ok": True})
            self.assertEqual(_StubOllama.seen_unload,
                             {"model": "llama3.1:8b", "prompt": "", "keep_alive": 0})
        finally:
            server.terminate()
            server.wait(timeout=10)
            stub.shutdown()
            stub.server_close()
            temporary.cleanup()

    def test_concurrent_jobs_bounded_and_store_private(self):
        import stat as _stat

        import server.models as _models

        _BlockingOllama.gate.clear()
        stub = HTTPServer(("127.0.0.1", 0), _BlockingOllama)
        stub_port = stub.server_address[1]
        threading.Thread(target=stub.serve_forever, daemon=True).start()

        port = _free_port()
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
        token = "models-bound-test-token-1234567890"
        environment = {**os.environ, "OPSDECK_TOKEN": token,
                       "OPSDECK_SSH_HOSTS": "",
                       "OPSDECK_DOCS_DIR": str(home / "docs"),
                       "OPSDECK_MODELS": f"desk=http://127.0.0.1:{stub_port}",
                       "HOME": str(home)}
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/hello", timeout=0.2).read()
                    break
                except Exception:
                    if server.poll() is not None:
                        self.fail("test server exited before it was ready")
                    time.sleep(0.1)
            else:
                self.fail("test server did not become ready")

            def submit(prompt):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/models/submit",
                    data=json.dumps({"provider": "desk", "model": "llama3.1:8b",
                                     "prompt": prompt}).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {token}"},
                    method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode())

            job_ids = [submit(f"slow {n}")[1]["job_id"]
                       for n in range(_models.MAX_CONCURRENT_JOBS)]
            try:
                submit("one too many")
                self.fail("job over the concurrency bound was accepted")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 429)
            finally:
                _BlockingOllama.gate.set()

            first = None
            for _ in range(100):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/models/jobs/{job_ids[0]}",
                    headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    first = json.loads(response.read().decode())
                if first.get("state") == "done":
                    break
                time.sleep(0.1)
            self.assertEqual(first["answer"], "slow answer")

            store = home / ".opsdeck-community" / "model-runs"
            self.assertEqual(_stat.S_IMODE(os.stat(store).st_mode), 0o700)
            job_dir = store / job_ids[0]
            self.assertEqual(_stat.S_IMODE(os.stat(job_dir).st_mode), 0o700)
            for name in ("result.json", "meta.json", "prompt.txt"):
                self.assertEqual(_stat.S_IMODE(os.stat(job_dir / name).st_mode), 0o600)
            self.assertEqual(list(job_dir.glob(".*.tmp")), [])
            # The polled result is complete JSON, never a partial write.
            self.assertEqual(first["job_id"], job_ids[0])
        finally:
            _BlockingOllama.gate.set()
            server.terminate()
            server.wait(timeout=10)
            stub.shutdown()
            stub.server_close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
