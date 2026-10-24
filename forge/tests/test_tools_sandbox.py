"""Security regression tests for workspace boundaries and shell policy."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools


class WorkspaceSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = tools.ROOT.set(Path(self.temp.name).resolve())

    def tearDown(self):
        tools.ROOT.reset(self.old_root)
        self.temp.cleanup()

    def test_safe_accepts_workspace_file(self):
        path = tools.safe("nested/example.txt")
        self.assertEqual(path, Path(self.temp.name).resolve() / "nested" / "example.txt")

    def test_safe_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            tools.safe("../outside.txt")

    def test_shell_is_disabled_by_default(self):
        with patch.dict(os.environ, {"ALLOW_SHELL": "0", "SANDBOX_DOCKER": "0", "SANDBOX_ALLOW_HOST_EXECUTION": "0"}):
            result = tools.sh("echo should-not-run")
        self.assertIn("disabled by policy", result)

    def test_no_runner_never_falls_back_to_host_shell(self):
        with patch.dict(os.environ, {"ALLOW_SHELL": "1", "SANDBOX_DOCKER": "0", "SANDBOX_ALLOW_HOST_EXECUTION": "0"}):
            result = tools.sh("echo should-not-run")
        self.assertIn("no isolated command runner", result)

    def test_docker_missing_fails_closed(self):
        with patch.dict(os.environ, {"ALLOW_SHELL": "1", "SANDBOX_DOCKER": "1", "SANDBOX_ALLOW_HOST_EXECUTION": "0"}):
            with patch("sandbox_core.subprocess.run", side_effect=FileNotFoundError):
                result = tools.sh("echo should-not-run")
        self.assertIn("Docker is unavailable", result)


if __name__ == "__main__":
    unittest.main()
