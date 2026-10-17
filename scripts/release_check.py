#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
# --- How to run ---
# python3 scripts/release_check.py
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Sequence


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    command: str
    exit_code: int


if not __package__:
    from gitenv import CHECK_TIMEOUT_SECONDS, GIT_TIMEOUT_SECONDS, run_bounded, safe_environment
else:
    from .gitenv import CHECK_TIMEOUT_SECONDS, GIT_TIMEOUT_SECONDS, run_bounded, safe_environment

# Settings passed to release-check's own git calls. They override the same settings in the repository's configuration;
# repository settings this list does not name (filters, textconv drivers, aliases, includes) still apply.
GIT_SAFE_OPTIONS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "core.pager=cat",
                    "-c", "diff.external=", "-c", "credential.helper=", "--no-pager")


def isolated_git_environment() -> dict:
    """The shared safe environment: every child of release-check, not only its direct git calls, receives it."""
    return safe_environment()


def _execute(root: Path, arguments: Sequence[str], timeout: int, env=None) -> subprocess.CompletedProcess:
    """Run one bounded command; a timeout or a launch failure is an ordinary failed result."""
    return run_bounded(arguments, root, env, timeout)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return _execute(root, ("git",) + GIT_SAFE_OPTIONS + arguments, GIT_TIMEOUT_SECONDS, isolated_git_environment())


def _run(root: Path, name: str, arguments: Sequence[str]) -> CheckResult:
    process = _execute(root, arguments, CHECK_TIMEOUT_SECONDS)
    return CheckResult(name, "PASS" if process.returncode == 0 else "FAIL", " ".join(arguments), process.returncode)


def _git_check(root: Path, name: str, *arguments: str) -> CheckResult:
    process = _git(root, *arguments)
    return CheckResult(name, "PASS" if process.returncode == 0 else "FAIL", "git " + " ".join(arguments), process.returncode)


def _build(root: Path, name: str, python: str, previous: List[str]) -> CheckResult:
    """Build once; the second call also requires the same archive and manifest hashes as the first."""
    process = _execute(root, [python, "scripts/build.py"], CHECK_TIMEOUT_SECONDS)
    passed = process.returncode == 0
    try:
        result = json.loads(process.stdout.strip().splitlines()[-1]) if passed else {}
    except (ValueError, IndexError):
        result, passed = {}, False
    passed = passed and isinstance(result, dict) and result.get("status") == "PASS" and all(
        isinstance(result.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", result[key]) for key in ("archive", "manifest"))
    if not passed:
        return CheckResult(name, "FAIL", "%s scripts/build.py" % python, process.returncode or 1)
    hashes = "%s %s" % (result.get("archive"), result.get("manifest"))
    if passed and previous and previous[0] != hashes:
        passed = False
    previous.append(hashes)
    return CheckResult(name, "PASS" if passed else "FAIL", "%s scripts/build.py" % python, process.returncode if not passed or process.returncode else 0)


def _git_hygiene(root: Path) -> CheckResult:
    tracked = _git(root, "ls-files")
    ignored = _git(root, "check-ignore", "-q", "dist/manifest.json")
    generated = [path for path in tracked.stdout.splitlines() if any(part in {'dist','.omx','.mandala','artifacts','__pycache__'} for part in Path(path).parts) or path.endswith(('.pyc','.pyo','.DS_Store','.Rhistory'))]
    passed = tracked.returncode == 0 and not generated and ignored.returncode == 0
    return CheckResult("git-hygiene", "PASS" if passed else "FAIL", "git ls-files/check-ignore", 0 if passed else 1)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    python = sys.executable
    checks: List[CheckResult] = []
    hashes: List[str] = []
    checks.append(_build(root, "build", python, hashes))
    checks.append(_run(root, "static-validation", [python, "scripts/validate.py"]))
    checks.append(_run(root, "unit-tests", [python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]))
    checks.append(_build(root, "reproducible-build", python, hashes))
    checks.append(_git_check(root, 'working-diff-whitespace', 'diff', '--check'))
    checks.append(_git_check(root, 'staged-diff-whitespace', 'diff', '--cached', '--check'))
    checks.append(_git_hygiene(root))
    passed = all(check.status == "PASS" for check in checks)
    print(json.dumps({"status": "PASS" if passed else "FAIL", "checks": [asdict(check) for check in checks]}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
