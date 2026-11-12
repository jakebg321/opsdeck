import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import model_adapters, models


class ModelAdapterTest(unittest.TestCase):
    def test_private_command_stays_server_side_and_actions_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "adapter.py"
            script.write_text('''import json, sys
body = json.load(sys.stdin)
action = sys.argv[-1]
if action == "status":
    print(json.dumps({"ok": True, "models": ["demo-model"], "holder": "example", "gpu": {"util_pct": 24}}))
elif action == "run":
    if body["prompt"] == "busy":
        print(json.dumps({"state": "refused", "held_by": "another-job"}))
    else:
        print(json.dumps({"answer": "echo: " + body["prompt"], "prompt_tokens": 2}))
else:
    print(json.dumps({"ok": True}))
''')
            config = root / "boxes.json"
            config.write_text(json.dumps({"boxes": [{
                "id": "lab", "label": "Lab GPU", "command": [sys.executable, str(script)],
                "image": True, "think": True, "unload": True,
                "worker_start": True, "worker_stop": True,
            }]}))
            with patch.dict(os.environ, {"OPSDECK_MODEL_ADAPTERS_FILE": str(config),
                                      "OPSDECK_MODELS": ""}):
                boxes = models.configured_boxes()
                self.assertEqual([box["id"] for box in boxes], ["lab"])
                router = models.make_models_router(lambda: None)
                route = lambda path: next(r.endpoint for r in router.routes if r.path == path)
                public = route("/api/models/providers")()["providers"][0]
                self.assertEqual(public["label"], "Lab GPU")
                self.assertTrue(public["worker_stop"])
                self.assertNotIn("command", public)
                self.assertNotIn("base_url", public)
                status = route("/api/models/status")("lab")
                self.assertEqual(status["models"], ["demo-model"])
                self.assertEqual(status["holder"], "example")
                self.assertEqual(status["gpu"]["util_pct"], 24)
                request = models.WorkerRequest(provider="lab")
                self.assertEqual(route("/api/models/worker-start")(request), {"ok": True})
                self.assertEqual(route("/api/models/worker-stop")(request), {"ok": True})
                with patch.object(models, "_jobs_dir", return_value=root):
                    submitted = route("/api/models/submit")(models.SubmitRequest(
                        provider="lab", model="demo-model", prompt="hello", think=True))
                    job_path = root / submitted["job_id"] / "result.json"
                    for _ in range(100):
                        if job_path.exists() and json.loads(job_path.read_text()).get("state") == "done":
                            break
                        time.sleep(0.02)
                    result = json.loads(job_path.read_text())
                    self.assertEqual(result["answer"], "echo: hello")
                    self.assertEqual(result["prompt_tokens"], 2)
                    submitted = route("/api/models/submit")(models.SubmitRequest(
                        provider="lab", model="demo-model", prompt="busy"))
                    job_path = root / submitted["job_id"] / "result.json"
                    for _ in range(100):
                        if job_path.exists() and json.loads(job_path.read_text()).get("state") == "refused":
                            break
                        time.sleep(0.02)
                    result = json.loads(job_path.read_text())
                    self.assertEqual(result["held_by"], "another-job")
