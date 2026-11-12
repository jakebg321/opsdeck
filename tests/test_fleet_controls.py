import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from server import app, status


TOKEN = "t" * 32


class FleetControlsTest(unittest.IsolatedAsyncioTestCase):
    def _snapshot(self, *, sample=False, timestamp=None, pid=2468):
        return {
            "ts": time.time() if timestamp is None else timestamp,
            "sample": sample,
            "gpus": [{"label": "GPU 0 · local", "procs": [{"pid": pid}]}],
        }

    async def test_stop_only_a_listed_process_in_a_fresh_real_snapshot(self):
        with patch.dict(os.environ, {"OPSDECK_TOKEN": TOKEN}), \
             patch.object(app.status_cache, "current", return_value=self._snapshot()), \
             patch.object(app.asyncio, "to_thread", new_callable=AsyncMock) as to_thread:
            result = await app.api_kill(app.KillRequest(host="local", pid=2468), f"Bearer {TOKEN}")
            self.assertEqual(result, {"ok": True})
            to_thread.assert_awaited_once_with(app._stop_gpu_process, "local", 2468)

    async def test_sample_unlisted_and_stale_snapshots_never_stop(self):
        with patch.dict(os.environ, {"OPSDECK_TOKEN": TOKEN}), \
             patch.object(app, "_stop_gpu_process") as stop:
            for payload, pid, expected in (
                (self._snapshot(sample=True), 2468, None),
                (self._snapshot(), 9999, 409),
                (self._snapshot(timestamp=time.time() - 60), 2468, 409),
            ):
                with patch.object(app.status_cache, "current", return_value=payload):
                    if expected is None:
                        result = await app.api_kill(app.KillRequest(host="local", pid=pid), f"Bearer {TOKEN}")
                        self.assertEqual(result, {"ok": False, "sample": True})
                    else:
                        with self.assertRaises(HTTPException) as raised:
                            await app.api_kill(app.KillRequest(host="local", pid=pid), f"Bearer {TOKEN}")
                        self.assertEqual(raised.exception.status_code, expected)
            stop.assert_not_called()

    async def test_token_and_host_are_checked(self):
        with patch.dict(os.environ, {"OPSDECK_TOKEN": TOKEN}), \
             patch.object(app.status_cache, "current", return_value=self._snapshot()), \
             patch.object(app, "_stop_gpu_process") as stop:
            for request, auth, code in (
                (app.KillRequest(host="local", pid=2468), "Bearer wrong", 401),
                (app.KillRequest(host="unconfigured", pid=2468), f"Bearer {TOKEN}", 400),
            ):
                with self.assertRaises(HTTPException) as raised:
                    await app.api_kill(request, auth)
                self.assertEqual(raised.exception.status_code, code)
            stop.assert_not_called()


class RichStatusTest(unittest.TestCase):
    def test_configured_status_file_can_add_live_style_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({
                "ts": 123,
                "gpus": [{"label": "GPU 0 · local", "procs": []}],
                "agents": [{"tool": "codex", "project": "example"}],
                "ollama": [{"name": "example-model", "size_gb": 4}],
            }))
            with patch.dict(os.environ, {"OPSDECK_STATUS_FILE": str(path)}), \
                 patch.object(status, "_gpu_rows", return_value=[]), \
                 patch.object(status, "_host_states", return_value={}):
                payload = status.build_payload()
            self.assertEqual(payload["ts"], 123)
            self.assertEqual(payload["agents"][0]["tool"], "codex")
            self.assertEqual(payload["ollama"][0]["name"], "example-model")
            self.assertEqual(payload["hosts"][0]["label"], "local")
