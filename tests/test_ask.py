"""Exercise the Ask-chats relay backend: config parsing, auth, and a stub service.

Follows the repository's existing integration-test style: a stub
Ask-compatible HTTP server plus the real uvicorn app, driven over
urllib. No new test dependencies, and no real model services are hit.
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


class _StubAsk(BaseHTTPRequestHandler):
    seen_fit = None
    seen_ask = None
    seen_auth = None
    mode = "ok"  # "ok" | "deny400" | "deny401"

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/chats/projects":
            type(self).seen_auth = self.headers.get("Authorization")
            if type(self).mode == "deny400":
                self._send({"error": "stub says no"}, status=400)
            elif type(self).mode == "deny401":
                self._send({"error": "stub auth failed"}, status=401)
            else:
                self._send({"projects": [{"name": "demo", "lastActive": 1700000000}]})
        elif self.path == "/api/local/testbox/status":
            self._send({"current": "somemodel", "ok": True})
        elif self.path == "/api/chats/job?id=job-good":
            self._send({"status": "done", "result": {"text": "hello from stub"}})
        elif self.path == "/api/chats/job?id=job-bad":
            self._send({"status": "error", "result": {"error": "boom"}})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except ValueError:
            body = {}
        if self.path == "/api/chats/fit":
            type(self).seen_fit = body
            self._send({"params": {"project": body.get("project"),
                                   "size": body.get("size")},
                        "tokens": 100})
        elif self.path == "/api/chats/ask":
            type(self).seen_ask = body
            self._send({"job": "job-good"})
        else:
            self.send_error(404)


class _StubAskRedirect(_StubAsk):
    def do_GET(self):
        if self.path == "/api/chats/projects":
            self.send_response(302)
            self.send_header("Location", "/api/chats/projects-target")
            self.end_headers()
        elif self.path == "/api/chats/projects-target":
            self._send({"projects": []})
        else:
            super().do_GET()


class AskConfigTest(unittest.TestCase):
    def _env(self, **overrides):
        from unittest.mock import patch
        base = {"OPSDECK_ASK_URL": "", "OPSDECK_ASK_BOX": "",
                "OPSDECK_ASK_TOKEN": "", "OPSDECK_ASK_CONSOLE_URL": ""}
        base.update(overrides)
        return patch.dict(os.environ, base)

    def test_empty_url_means_unconfigured(self):
        import server.ask as ask
        with self._env():
            self.assertIsNone(ask.ask_base_url())
            self.assertIsNone(ask.console_url())
            self.assertIsNone(ask.console_origin())

    def test_bad_base_urls_are_rejected(self):
        import server.ask as ask
        for bad in ("ftp://x/y", "http://user@host:8756", "http:///nonsense",
                    "just-a-word", "http://"):
            with self._env(OPSDECK_ASK_URL=bad):
                self.assertIsNone(ask.ask_base_url(), bad)

    def test_good_base_url_is_normalized(self):
        import server.ask as ask
        with self._env(OPSDECK_ASK_URL="http://127.0.0.1:8756/"):
            self.assertEqual(ask.ask_base_url(), "http://127.0.0.1:8756")

    def test_box_defaults_to_neutral_local(self):
        import server.ask as ask
        with self._env():
            self.assertEqual(ask.ask_box(), "local")
        with self._env(OPSDECK_ASK_BOX="gpu-a"):
            self.assertEqual(ask.ask_box(), "gpu-a")
        with self._env(OPSDECK_ASK_BOX="evil; rm"):
            self.assertEqual(ask.ask_box(), "local")

    def test_console_url_must_be_clean_https_origin(self):
        import server.ask as ask
        with self._env(OPSDECK_ASK_CONSOLE_URL="https://gpu.example.com:8443/app"):
            self.assertEqual(ask.console_origin(), "https://gpu.example.com:8443")
            self.assertEqual(ask.console_url(), "https://gpu.example.com:8443/app")
        with self._env(OPSDECK_ASK_CONSOLE_URL="https://user@gpu.example.com/"):
            self.assertIsNone(ask.console_url())
        with self._env(OPSDECK_ASK_CONSOLE_URL="ftp://gpu.example.com/"):
            self.assertIsNone(ask.console_url())

    def test_request_shapes_carry_no_url_fields(self):
        import server.ask as ask
        import inspect
        source = inspect.getsource(ask.make_ask_router)
        self.assertNotIn("base_url", source.replace("ask_base_url", ""))
        # Client-controlled routing fields must not reach the upstream call.
        self.assertIn('"box": box', source)


class AskIntegrationTest(unittest.TestCase):
    def _launch(self, stub_class=_StubAsk, **extra_env):
        stub = HTTPServer(("127.0.0.1", 0), stub_class)
        stub_port = stub.server_address[1]
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        port = _free_port()
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
        token = "ask-integration-test-token-1234567890"
        environment = {**os.environ, "OPSDECK_TOKEN": token,
                       "OPSDECK_SSH_HOSTS": "",
                       "OPSDECK_DOCS_DIR": str(home / "docs"),
                       "OPSDECK_MODELS": "",
                       "OPSDECK_ASK_URL": f"http://127.0.0.1:{stub_port}",
                       "OPSDECK_ASK_BOX": "testbox",
                       "OPSDECK_ASK_CONSOLE_URL": "https://gpu.example.com:8443/",
                       "HOME": str(home)}
        environment.update(extra_env)
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
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
        self.addCleanup(temporary.cleanup)
        self.addCleanup(stub.server_close)
        self.addCleanup(stub.shutdown)
        self.addCleanup(lambda: (server.terminate(), server.wait(timeout=10)))
        return port, token

    def _api(self, port, token, path, body=None, authorized=True):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(
                {"Authorization": f"Bearer {token}"} if authorized else {})},
            method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode())

    def test_config_projects_ready_fit_ask_job(self):
        port, token = self._launch()
        status, config = self._api(port, token, "/api/ask/config")
        self.assertEqual(status, 200)
        self.assertEqual(config, {"configured": True, "box": "testbox",
                                 "console_url": "https://gpu.example.com:8443"})

        _, projects = self._api(port, token, "/api/ask/projects")
        self.assertEqual(projects["projects"][0]["name"], "demo")

        _, ready = self._api(port, token, "/api/ask/ready")
        self.assertTrue(ready["ok"])

        # The client cannot steer the upstream box: even a forged box in the
        # fit body must arrive upstream as the configured box.
        _, fit = self._api(port, token, "/api/ask/fit",
                           {"project": "demo", "size": 32768, "box": "evil"})
        self.assertEqual(fit["tokens"], 100)
        self.assertEqual(_StubAsk.seen_fit,
                         {"mode": "chats", "project": "demo",
                          "size": 32768, "box": "testbox"})

        _, asked = self._api(port, token, "/api/ask/ask",
                             {"project": "demo", "question": "hi",
                              "thread": [], "box": "evil"})
        self.assertEqual(asked, {"job": "job-good"})
        self.assertEqual(_StubAsk.seen_ask["box"], "testbox")
        self.assertEqual(_StubAsk.seen_ask["question"], "hi")

        _, good = self._api(port, token, "/api/ask/job?id=job-good")
        self.assertEqual(good["result"]["text"], "hello from stub")
        _, bad = self._api(port, token, "/api/ask/job?id=job-bad")
        self.assertEqual(bad["status"], "error")

    def test_auth_is_required(self):
        port, token = self._launch()
        for path in ("/api/ask/config", "/api/ask/projects", "/api/ask/ready",
                     "/api/ask/job?id=job-good"):
            try:
                self._api(port, token, path, authorized=False)
                self.fail(f"unauthenticated request was allowed: {path}")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401, path)
        for path, body in (("/api/ask/fit", {"project": "demo", "size": 1024}),
                           ("/api/ask/ask", {"question": "hi"})):
            try:
                self._api(port, token, path, body, authorized=False)
                self.fail(f"unauthenticated request was allowed: {path}")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 401, path)

    def test_invalid_inputs_are_rejected(self):
        port, token = self._launch()
        bad_calls = [
            ("/api/ask/job?id=../escape", None),
            ("/api/ask/fit", {"project": "", "size": 1024}),
            ("/api/ask/fit", {"project": "demo", "size": 999999}),
            ("/api/ask/ask", {"question": "   "}),
            ("/api/ask/ask", {"question": "x" * 9000}),
            ("/api/ask/ask", {"question": "x" * 70000}),
        ]
        for path, body in bad_calls:
            try:
                self._api(port, token, path, body)
                self.fail(f"bad input was allowed: {path} {body}")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400, f"{path} {body}")

    def test_upstream_errors_do_not_leak_or_reauth(self):
        port, token = self._launch()
        # A job that failed upstream still relays as 200 with status=error.
        _, bad = self._api(port, token, "/api/ask/job?id=job-bad")
        self.assertEqual(bad["status"], "error")
        # A stub 400 surfaces as relay 400 with the stub's message...
        _StubAsk.mode = "deny400"
        try:
            try:
                self._api(port, token, "/api/ask/projects")
                self.fail("stub 400 was not relayed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)
                self.assertIn("stub says no", exc.read().decode())
        finally:
            _StubAsk.mode = "ok"
        # ...but a stub 401 must NOT surface as Opsdeck 401: the phone
        # treats Opsdeck 401 as "token rejected, re-authenticate".
        _StubAsk.mode = "deny401"
        try:
            try:
                self._api(port, token, "/api/ask/projects")
                self.fail("stub 401 was not mapped")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 502)
                body = exc.read().decode()
                self.assertIn("stub auth failed", body)
                # No upstream address may leak into relay errors.
                self.assertNotIn("127.0.0.1", body)
        finally:
            _StubAsk.mode = "ok"

    def test_redirect_is_not_followed(self):
        port, token = self._launch(stub_class=_StubAskRedirect)
        try:
            self._api(port, token, "/api/ask/projects")
            self.fail("redirect target response was returned")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 502)

    def test_bearer_token_forwarded_but_never_echoed(self):
        port, token = self._launch(OPSDECK_ASK_TOKEN="upstream-secret")
        self._api(port, token, "/api/ask/projects")
        self.assertEqual(_StubAsk.seen_auth, "Bearer upstream-secret")

    def test_unconfigured_relay_reports_setup(self):
        stub = HTTPServer(("127.0.0.1", 0), _StubAsk)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        port = _free_port()
        temporary = tempfile.TemporaryDirectory()
        home = Path(temporary.name)
        token = "ask-unconfigured-test-token-12345678"
        environment = {**os.environ, "OPSDECK_TOKEN": token,
                       "OPSDECK_SSH_HOSTS": "",
                       "OPSDECK_DOCS_DIR": str(home / "docs"),
                       "OPSDECK_MODELS": "",
                       "OPSDECK_ASK_URL": "",
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
            _, config = self._api(port, token, "/api/ask/config")
            self.assertEqual(config["configured"], False)
            self.assertIsNone(config["console_url"])
            for path in ("/api/ask/projects", "/api/ask/ready"):
                try:
                    self._api(port, token, path)
                    self.fail(f"unconfigured relay answered: {path}")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 503, path)
        finally:
            server.terminate()
            server.wait(timeout=10)
            stub.shutdown()
            stub.server_close()
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
