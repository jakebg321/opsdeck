import subprocess
import unittest
from unittest.mock import patch

from server import status
from server.status import _gpu_rows, configured_hosts


class StatusTest(unittest.TestCase):
    @patch("server.status.subprocess.run")
    def test_compute_processes_follow_gpu_uuid(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "GPU-A, Example A, 1024, 8192, 7, 40\nGPU-B, Example B, 2048, 8192, 9, 42\n", ""),
            subprocess.CompletedProcess([], 0, 'GPU-B, 42, "/tmp/my app, worker", 2048\n', ""),
        ]
        gpus = _gpu_rows()
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["procs"], [])
        self.assertEqual(gpus[1]["procs"], [{"pid": 42, "name": "my app, worker", "mem_mb": 2048}])

    @patch.dict("os.environ", {"OPSDECK_SSH_HOSTS": "build, -oProxyCommand, build, lab.example"})
    def test_host_aliases_are_unique_and_cannot_be_ssh_options(self):
        self.assertEqual(configured_hosts(), ["local", "build", "lab.example"])

    @patch.dict("os.environ", {"OPSDECK_SSH_HOSTS": "build"})
    @patch("server.status._gpu_rows", return_value=[])
    @patch("server.status._probe_ssh", return_value=True)
    def test_remote_host_state_is_probed_and_cached(self, probe, _gpus):
        with status._host_probe_lock:
            status._host_probe_cache.clear()
        try:
            self.assertEqual(status.build_payload()["hosts"][1]["state"], "online")
            self.assertEqual(status.build_payload()["hosts"][1]["state"], "online")
            probe.assert_called_once_with("build")
        finally:
            with status._host_probe_lock:
                status._host_probe_cache.clear()
