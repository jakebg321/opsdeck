"""Exercise the real WebSocket handshake and PTY on a local test server."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus


class TerminalIntegrationTest(unittest.TestCase):
    def test_origin_auth_and_terminal(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()

        temporary = tempfile.TemporaryDirectory()
        base = Path(temporary.name)
        docs = base / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide\nA visible document.\n")
        (base / "outside.md").write_text("# Outside\nKeep this out.\n")
        (docs / "link.md").symlink_to(base / "outside.md")
        token = "local-integration-test-token-12345678"
        environment = {**os.environ, "OPSDECK_TOKEN": token, "OPSDECK_SSH_HOSTS": "",
                       "OPSDECK_DOCS_DIR": str(docs)}
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1",
             "--port", str(port), "--no-access-log"],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=0.2).read()
                    break
                except Exception:
                    if server.poll() is not None:
                        self.fail("test server exited before it was ready")
                    time.sleep(0.1)
            else:
                self.fail("test server did not become ready")

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=3) as response:
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")

            def docs_request(path, authorized=True):
                headers = {"Authorization": f"Bearer {token}"} if authorized else {}
                request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
                with urllib.request.urlopen(request, timeout=3) as response:
                    return json.load(response)

            with self.assertRaises(urllib.error.HTTPError) as rejected:
                docs_request("/api/docs", authorized=False)
            self.assertEqual(rejected.exception.code, 401)
            listing = docs_request("/api/docs")
            self.assertEqual([item["path"] for item in listing["docs"]], ["guide.md"])
            self.assertIn("A visible document.", docs_request("/api/docs/content?path=guide.md")["text"])
            metrics = docs_request("/api/status")["local_metrics"]
            self.assertGreaterEqual(metrics["load_1m"], 0)
            self.assertGreater(metrics["memory_total_mb"], 0)
            self.assertGreater(metrics["disk_total_mb"], 0)
            self.assertLessEqual(metrics["memory_used_mb"], metrics["memory_total_mb"])
            self.assertLessEqual(metrics["disk_used_mb"], metrics["disk_total_mb"])
            for forbidden in ("../outside.md", "link.md"):
                query = urllib.parse.quote(forbidden, safe="")
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    docs_request(f"/api/docs/content?path={query}")
                self.assertEqual(rejected.exception.code, 400)

            async def check():
                uri = f"ws://127.0.0.1:{port}/api/term?host=local"
                origin = f"http://127.0.0.1:{port}"
                with self.assertRaises(InvalidStatus) as rejected:
                    async with websockets.connect(uri, origin="http://foreign.example"):
                        pass
                self.assertEqual(rejected.exception.response.status_code, 403)

                async with websockets.connect(uri, origin=origin) as ws:
                    await ws.send(json.dumps({"auth": "wrong"}))
                    with self.assertRaises(ConnectionClosed) as rejected:
                        await ws.recv()
                    self.assertEqual(rejected.exception.rcvd.code, 4401)

                async with websockets.connect(uri, origin=origin) as ws:
                    await ws.send(json.dumps({"auth": token}))
                    self.assertEqual(json.loads(await ws.recv()), {"type": "ready"})
                    # Encode the marker so terminal echo alone cannot satisfy this check.
                    await ws.send("printf '\\x4f\\x50\\x53\\x44\\x45\\x43\\x4b\\n'\r")
                    output = ""
                    for _ in range(20):
                        part = await asyncio.wait_for(ws.recv(), timeout=3)
                        output += part if isinstance(part, str) else part.decode(errors="replace")
                        if "OPSDECK" in output:
                            break
                    self.assertIn("OPSDECK", output)

                pending = []
                try:
                    for _ in range(16):
                        pending.append(await websockets.connect(uri, origin=origin))
                    async with websockets.connect(uri, origin=origin) as ws:
                        with self.assertRaises(ConnectionClosed) as rejected:
                            await ws.recv()
                        self.assertEqual(rejected.exception.rcvd.code, 4429)
                finally:
                    await asyncio.gather(*(ws.close() for ws in pending))

            asyncio.run(check())
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            temporary.cleanup()
