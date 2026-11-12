import unittest
from unittest.mock import patch

from server import app


class TerminalAttemptLimitTest(unittest.TestCase):
    def test_attempts_expire_after_one_minute(self):
        with app._terminal_attempts_lock:
            app._terminal_attempts.clear()
        try:
            with patch("server.app.time.monotonic", return_value=1000):
                for _ in range(app.TERMINAL_ATTEMPTS_PER_MINUTE):
                    self.assertTrue(app._allow_terminal_attempt("example-client"))
                self.assertFalse(app._allow_terminal_attempt("example-client"))
            with patch("server.app.time.monotonic", return_value=1061):
                self.assertTrue(app._allow_terminal_attempt("example-client"))
        finally:
            with app._terminal_attempts_lock:
                app._terminal_attempts.clear()
