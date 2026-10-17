from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.gitenv import CHECK_TIMEOUT_SECONDS, safe_environment


class CLICompatibilityBoundaryTests(unittest.TestCase):
    def _invoke(self, executable: str) -> subprocess.CompletedProcess:
        environment = safe_environment()
        environment["STATERIPPLE"] = executable
        return subprocess.run(
            [sys.executable, "scripts/check_cli_compat.py"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=CHECK_TIMEOUT_SECONDS,
        )

    def test_missing_cli_is_blocked(self) -> None:
        # Given: an explicit executable path that does not exist.
        missing = str(Path(tempfile.gettempdir()) / "stateripple-definitely-missing")

        # When: compatibility evaluation starts.
        process = self._invoke(missing)

        # Then: it is BLOCKED rather than reported as a test failure or pass.
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stdout)["status"], "BLOCKED")

    def test_version_mismatch_is_blocked(self) -> None:
        # Given: an executable claiming an unsupported version.
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "stateripple"
            fake.write_text("#!/bin/sh\nprintf 'stateripple v0.2.0\\n'\n", encoding="utf-8")
            fake.chmod(0o755)

            # When: compatibility evaluation starts.
            process = self._invoke(str(fake))

            # Then: strict version gating stops before compatibility cases run.
            self.assertEqual(process.returncode, 2)
            report = json.loads(process.stdout)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["cases"], [])


if __name__ == "__main__":
    unittest.main()
