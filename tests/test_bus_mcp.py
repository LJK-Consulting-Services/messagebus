import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_bus_mcp():
    spec = importlib.util.spec_from_file_location("bus_mcp", ROOT / "scripts" / "bus-mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BusMcpTests(unittest.TestCase):
    def test_redacts_redis_url_credentials(self):
        bus_mcp = load_bus_mcp()
        text = (
            "redis://user:secret@example.com:6379/0 "
            "redis://127.0.0.1:6379/0?password=pw&token=tok"
        )

        redacted = bus_mcp._redact_secrets(text)

        self.assertNotIn("secret", redacted)
        self.assertNotIn("password=pw", redacted)
        self.assertNotIn("token=tok", redacted)
        self.assertIn("redis://***@example.com:6379/0", redacted)
        self.assertIn("password=***", redacted)
        self.assertIn("token=***", redacted)

    def test_nonzero_bus_exit_is_mcp_error_even_with_stdout(self):
        bus_mcp = load_bus_mcp()
        completed = subprocess.CompletedProcess(
            args=["bus", "doctor"],
            returncode=1,
            stdout="redis: FAIL (redis://:secret@localhost:6379/0)\n",
            stderr="",
        )

        with patch.object(bus_mcp.subprocess, "run", return_value=completed):
            text, is_error = bus_mcp.run_bus("bus_doctor", {})

        self.assertTrue(is_error)
        self.assertNotIn("secret", text)
        self.assertIn("redis://***@localhost:6379/0", text)


if __name__ == "__main__":
    unittest.main()
