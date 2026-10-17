#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
# --- How to run ---
# STATERIPPLE=/absolute/path/to/stateripple python3 scripts/check_cli_compat.py
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

if not __package__:
    from gitenv import GIT_TIMEOUT_SECONDS, run_bounded, safe_environment
else:
    from .gitenv import GIT_TIMEOUT_SECONDS, run_bounded, safe_environment

EXPECTED_VERSION = "stateripple v0.1.0"


@dataclass(frozen=True)
class RunSpec:
    executable: str
    arguments: Sequence[str]
    directory: Optional[Path] = None
    environment: Optional[Mapping[str, str]] = None


@dataclass(frozen=True)
class CaseResult:
    id: str
    status: str
    expected: str
    actual: str
    stdout_sha256: str
    executions: List[Dict[str, object]]


def _run(spec: RunSpec) -> subprocess.CompletedProcess:
    """One bounded CLI or fixture command; a timeout is an ordinary failed result, and its process group is ended."""
    return run_bounded([spec.executable, *spec.arguments], spec.directory,
                       spec.environment if spec.environment else None, GIT_TIMEOUT_SECONDS)


def _json(stdout: str) -> Dict[str, object]:
    value = json.loads(stdout)
    if not isinstance(value, dict):
        raise ValueError("CLI JSON output is not an object")
    return value


def _case(case_id: str, passed: bool, expected: str, actual: str, stdout: str = "", executions: Sequence[subprocess.CompletedProcess] = ()) -> CaseResult:
    return CaseResult(
        id=case_id,
        status="PASS" if passed else "FAIL",
        expected=expected,
        actual=actual,
        stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest() if stdout else "",
        executions=[_execution_evidence(process) for process in executions],
    )


def _execution_evidence(process: subprocess.CompletedProcess) -> Dict[str, object]:
    def sanitize(value):
        value = value.replace(str(Path(__file__).resolve().parents[1]), '<PROJECT>')
        value = value.replace(str(Path.home()), '<HOME>')
        return re.sub(r'(?:/private)?/var/folders/[^\\s\"\']+?/T/stateripple-[^/\\s\"\']+|(?:/private)?/tmp/stateripple-[^/\\s\"\']+', '<COMPAT_TMP>', value)
    try:
        report = json.loads(process.stdout) if process.stdout else None
    except json.JSONDecodeError:
        report = None
    return {'argv': [sanitize(str(arg)) for arg in process.args], 'exit_code': process.returncode,
            'stdout': sanitize(process.stdout), 'stderr': sanitize(process.stderr),
            'stdout_sha256': hashlib.sha256(process.stdout.encode('utf-8')).hexdigest(),
            'stderr_sha256': hashlib.sha256(process.stderr.encode('utf-8')).hexdigest(),
            'status': report.get('status') if isinstance(report, dict) else None,
            'stop_reason': report.get('stop_reason') if isinstance(report, dict) else None,
            'schema_version': report.get('schema_version') if isinstance(report, dict) else None,
            'warnings': report.get('warnings') if isinstance(report, dict) else None}


def _git(repository: Path, arguments: Sequence[str]) -> None:
    environment = safe_environment(identity=("StateRipple Compatibility", "compat@example.invalid"))
    result = _run(RunSpec("git", arguments, repository, environment))
    if result.returncode != 0:
        raise RuntimeError("isolated git fixture setup failed: %s" % result.stderr.strip())


def _repository(parent: Path, change: str, dirty: bool = False) -> Path:
    repository = parent / ("repo-" + change + ("-dirty" if dirty else ""))
    repository.mkdir()
    _git(repository, ["init", "-q"])
    (repository / "go.mod").write_text("module example.invalid/compat\n\ngo 1.23\n", encoding="utf-8")
    (repository / "calc.go").write_text("package compat\n\nfunc Value() int { return 1 }\n", encoding="utf-8")
    _git(repository, ["add", "go.mod", "calc.go"])
    _git(repository, ["commit", "-q", "-m", "fixture base"])
    if change == "source":
        (repository / "calc.go").write_text("package compat\n\nfunc Value() int { return 2 }\n", encoding="utf-8")
        _git(repository, ["add", "calc.go"])
    else:
        (repository / "opaque.asset").write_bytes(b"unclassified\n")
        _git(repository, ["add", "opaque.asset"])
    _git(repository, ["commit", "-q", "-m", "fixture head"])
    if dirty:
        (repository / "calc.go").write_text("package compat\n\nfunc Value() int { return 3 }\n", encoding="utf-8")
    return repository


def _simulate(cli: str, fixture: Path, environment: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory(prefix='stateripple-simulation-') as temporary:
        return _run(RunSpec(cli, ["simulate", "--input", str(fixture), "--format", "json"], Path(temporary), environment))


def _blocked(reason: str) -> int:
    print(json.dumps({"status": "BLOCKED", "reason": reason, "cases": []}, sort_keys=True))
    return 2


def run_compatibility(root: Path, executable: Optional[str]) -> int:
    cli = executable or shutil.which("stateripple")
    if not cli or not Path(cli).is_file():
        return _blocked("StateRipple CLI is unavailable; set STATERIPPLE to a v0.1.0 executable")
    version = _run(RunSpec(cli, ["version"]))
    if version.returncode != 0 or version.stdout.strip() != EXPECTED_VERSION:
        return _blocked("required %s, observed %s" % (EXPECTED_VERSION, version.stdout.strip() or "execution failure"))
    fixtures = root / "tests" / "fixtures"
    upstream = fixtures / "upstream"
    results: List[CaseResult] = [_case("C01", True, EXPECTED_VERSION, version.stdout.strip(), version.stdout, [version])]
    empty = upstream / "cli-empty.json"
    normal = _simulate(cli, empty)
    golden = (upstream / "cli-empty.golden.json").read_text(encoding="utf-8")
    results.append(_case("C02", normal.returncode == 0 and normal.stdout == golden, "exit 0 and byte-exact upstream golden", "exit %d" % normal.returncode, normal.stdout, [normal]))
    isolated_environment = safe_environment()
    isolated_environment["PATH"] = ''
    nongit = _simulate(cli, empty, isolated_environment)
    results.append(_case("C03", nongit.returncode == 0 and _json(nongit.stdout).get("status") == "complete", "simulate works without Git/Go/PATH", "exit %d" % nongit.returncode, nongit.stdout, [nongit]))
    limited = _simulate(cli, fixtures / "generation-limit.json")
    limited_report = _json(limited.stdout)
    recorded = fixtures / "recorded"
    limited_ok = limited.returncode == 2 and limited_report.get("status") == "incomplete" and limited_report.get("stop_reason") == "generation_limit"
    limited_ok = limited_ok and limited.stdout == (recorded / "generation-limit.report.json").read_text(encoding="utf-8")
    results.append(_case("C04", limited_ok, "exit 2 incomplete generation_limit", "exit %d %s/%s" % (limited.returncode, limited_report.get("status"), limited_report.get("stop_reason")), limited.stdout, [limited]))
    invalid = _simulate(cli, fixtures / "invalid.json")
    results.append(_case("C05", invalid.returncode == 1 and not invalid.stdout, "exit 1 without a report", "exit %d stdout-bytes %d" % (invalid.returncode, len(invalid.stdout)), invalid.stdout, [invalid]))
    zero = _simulate(cli, fixtures / "risk-zero.json")
    zero_report = _json(zero.stdout)
    zero_ok = zero.returncode == 0 and zero_report.get("status") == "complete" and any(cell.get("risk") == 0 for cell in zero_report.get("cells", []))
    zero_ok = zero_ok and zero.stdout == (recorded / "risk-zero.report.json").read_text(encoding="utf-8")
    results.append(_case("C06", zero_ok, "complete report retains Risk 0 cells", "exit %d" % zero.returncode, zero.stdout, [zero]))
    trace = _simulate(cli, fixtures / "tied-cycle.json")
    trace_report = _json(trace.stdout)
    trace_ok = trace.returncode == 0 and bool(trace_report.get("waves")) and any(len(cell.get("causes", [])) > 1 for cell in trace_report.get("cells", []))
    wave_causes = {change.get("id"): change.get("causes") for wave in trace_report.get("waves", []) for change in wave.get("changes", [])}
    # Final support must differ from the wave history for at least one cell (cyclic support added later).
    trace_ok = trace_ok and any(cell.get("id") in wave_causes and cell.get("causes") != wave_causes[cell.get("id")] for cell in trace_report.get("cells", []))
    trace_ok = trace_ok and trace.stdout == (recorded / "tied-cycle.report.json").read_text(encoding="utf-8")
    results.append(_case("C07", trace_ok, "wave history distinct from tied final causes", "exit %d" % trace.returncode, trace.stdout, [trace]))
    targets = _simulate(cli, upstream / "example.json")
    targets_report = _json(targets.stdout)
    results.append(_case("C08", targets.returncode == 0 and targets.stdout == (upstream / "example.report.json").read_text(encoding="utf-8") and bool(targets_report.get("test_targets")), "positive test_targets projection", "exit %d targets %d" % (targets.returncode, len(targets_report.get("test_targets", []))), targets.stdout, [targets]))
    with tempfile.TemporaryDirectory(prefix="stateripple-compat-") as temporary:
        temp = Path(temporary)
        existing = temp / "existing.json"
        existing.write_text("sentinel\n", encoding="utf-8")
        protected = _run(RunSpec(cli, ["simulate", "--input", str(empty), "--format", "json", "--out", str(existing)]))
        results.append(_case("C09", protected.returncode == 1 and existing.read_text(encoding="utf-8") == "sentinel\n", "exit 1 and existing bytes preserved", "exit %d" % protected.returncode, executions=[protected]))
        created = temp / "created.json"
        output = _run(RunSpec(cli, ["simulate", "--input", str(empty), "--format", "json", "--out", str(created)]))
        created_ok = output.returncode == 0 and not output.stdout and created.is_file() and created.read_text(encoding='utf-8') == golden
        if os.name == 'posix' and created.is_file():
            created_ok = created_ok and created.stat().st_mode & 0o777 == 0o600
        results.append(_case("C10", created_ok, "new private JSON output matches golden", "exit %d" % output.returncode, executions=[output]))
        source_repo = _repository(temp, "source")
        analyzed = _run(RunSpec(cli, ["analyze", "--base", "HEAD~1", "--format", "json"], source_repo))
        analyzed_report = _json(analyzed.stdout) if analyzed.stdout else {}
        results.append(_case("C11", analyzed.returncode == 0 and analyzed_report.get("status") == "complete" and analyzed_report.get("build_context") is not None, "clean committed Go analysis completes", "exit %d status %s" % (analyzed.returncode, analyzed_report.get("status")), analyzed.stdout, [analyzed]))
        dirty_repo = _repository(temp, "source", True)
        dirty = _run(RunSpec(cli, ["analyze", "--base", "HEAD~1", "--format", "json"], dirty_repo))
        results.append(_case("C12", dirty.returncode == 1 and not dirty.stdout, "dirty worktree fails closed with exit 1", "exit %d" % dirty.returncode, dirty.stdout, [dirty]))
        unknown_repo = _repository(temp, "unknown")
        unknown = _run(RunSpec(cli, ["analyze", "--base", "HEAD~1", "--format", "json"], unknown_repo))
        unknown_report = _json(unknown.stdout) if unknown.stdout else {}
        results.append(_case("C13", unknown.returncode == 2 and unknown_report.get("status") == "incomplete" and unknown_report.get("stop_reason") == "converged" and bool(unknown_report.get("unknown_changes")), "unknown adapter input is incomplete exit 2 despite convergence", "exit %d status %s/%s" % (unknown.returncode, unknown_report.get("status"), unknown_report.get("stop_reason")), unknown.stdout, [unknown]))
        shim = temp / "shim"
        shim.mkdir()
        fake_git = shim / "git"
        fake_git.write_text("#!/bin/sh\nprintf 'git version 2.44.0\\n'\n", encoding="utf-8")
        fake_git.chmod(0o755)
        old_git_environment = safe_environment()
        old_git_environment["PATH"] = str(shim) + os.pathsep + old_git_environment.get("PATH", "")
        unsupported = _run(RunSpec(cli, ["analyze", "--base", "HEAD~1", "--format", "json"], source_repo, old_git_environment))
        results.append(_case("C14", unsupported.returncode == 1 and not unsupported.stdout, "injected Git 2.44 test double is rejected", "exit %d" % unsupported.returncode, unsupported.stdout, [unsupported]))
    repeat = _simulate(cli, upstream / "generic-supply-chain.json")
    repeat_again = _simulate(cli, upstream / "generic-supply-chain.json")
    deterministic = repeat.returncode == repeat_again.returncode == 0 and repeat.stdout == repeat_again.stdout == (upstream / "generic-supply-chain.report.json").read_text(encoding="utf-8")
    results.append(_case("C15", deterministic, "identical conditions produce byte-identical reports", "exit %d/%d" % (repeat.returncode, repeat_again.returncode), repeat.stdout, [repeat, repeat_again]))
    passed = all(result.status == "PASS" for result in results)
    print(json.dumps({"schema_version": 1, "evaluation_type": "real-cli-compatibility", "status": "PASS" if passed else "FAIL", "cli_version": EXPECTED_VERSION, "environment": {"platform": sys.platform, "python": sys.version.split()[0]}, "supplements": {"C14": "Real StateRipple CLI with injected Git 2.44 test double"}, "cases": [asdict(result) for result in results]}, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


def main() -> int:
    return run_compatibility(Path(__file__).resolve().parent.parent, os.environ.get("STATERIPPLE"))


if __name__ == "__main__":
    raise SystemExit(main())
