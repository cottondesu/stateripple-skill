from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

if not __package__:
    from eval_common import EvaluationError, load_json, verify_record
else:
    from .eval_common import EvaluationError, load_json, verify_record

REVIEW_CHECKS = ("live", "replay", "positive_activation", "negative_activation", "package",
                 "manifests", "safety_contracts", "known_limitations")
VERIFICATION_COMMANDS = ("make build", "make check", "make test", "make cli-compat", "make release-check")
MAX_REVIEW_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ReviewContext:
    project_root: Path
    target: Mapping[str, str]
    source_files: Mapping[str, str]
    manifest_files: Mapping[str, str]
    case_records: Mapping[Tuple[str, str], Mapping[str, str]]
    contract_ids: Sequence[str]


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvaluationError(reason)


def text_present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def utc_timestamp(value: Any) -> datetime:
    require(isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)),
            "review time must be an ISO 8601 UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise EvaluationError("review time is not a real date") from error
    require(parsed <= datetime.now(timezone.utc), "review time is in the future")
    return parsed


def regular_bytes(path: Path) -> bytes:
    require(stat.S_ISREG(path.lstat().st_mode), "review artifact is not a regular file")
    require(path.stat().st_size <= MAX_REVIEW_BYTES, "review artifact exceeds the size limit")
    data = path.read_bytes()
    require(bool(data.strip()), "review artifact is empty")
    return data


def artifact_bytes(directory: Path, reference: Any) -> bytes:
    require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}, "invalid artifact reference")
    name, digest = reference["path"], reference["sha256"]
    require(isinstance(name, str) and bool(name) and "\\" not in name and not Path(name).is_absolute()
            and all(part not in ("", ".", "..") for part in name.split("/")), "unsafe review artifact path")
    require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "invalid artifact hash")
    path = directory
    for part in name.split("/"):
        path = path / part
        require(not path.is_symlink(), "review artifact path contains a symlink")
    data = regular_bytes(path)
    require(hashlib.sha256(data).hexdigest() == digest, "review artifact hash mismatch")
    return data


def sealed_json(path: Path) -> Dict[str, Any]:
    regular_bytes(path)
    value = load_json(path)
    require(isinstance(value, dict), "review artifact must be a JSON object")
    verify_record(value)
    return value


def log_reference(value: Any, lines: Sequence[str]) -> None:
    require(isinstance(value, list) and len(value) == 2 and all(type(n) is int for n in value),
            "inspection needs a transcript line range")
    start, end = value
    require(1 <= start <= end <= len(lines) and bool("\n".join(lines[start - 1:end]).strip()),
            "inspection refers to missing or empty transcript lines")


def execution_evidence(path: Path, record: Mapping[str, Any], context: ReviewContext) -> Sequence[str]:
    directory = path.parent.resolve()
    artifact_bytes(directory, record.get("execution"))
    execution = sealed_json(directory / record["execution"]["path"])
    require(type(execution.get("schema_version")) is int and execution["schema_version"] == 1
            and execution.get("record_type") == "ai-review-execution", "unknown review execution schema")
    for key in ("reviewer_id", "model", "session_id"):
        require(execution.get(key) == record[key], "review execution identity differs: " + key)
    require(execution.get("review_target") == dict(context.target), "review execution target differs")
    require(execution.get("baseline_source_tree_sha256") == record["independence"]["baseline_source_tree_sha256"],
            "review execution baseline differs")
    artifact_bytes(directory, execution.get("diff"))
    require(execution.get("read_only") is True, "review execution was not read-only")
    require(execution.get("source_tree_before") == context.target["source_tree_sha256"]
            and execution.get("source_tree_after") == context.target["source_tree_sha256"],
            "source changed during review")
    started, finished = utc_timestamp(execution.get("started_at")), utc_timestamp(execution.get("finished_at"))
    require(started <= finished <= utc_timestamp(record["reviewed_at"]), "review execution times are inconsistent")
    require(execution.get("source_files") == dict(context.source_files), "review source inventory is incomplete or stale")
    require(bool(context.manifest_files) and execution.get("manifest_files") == dict(context.manifest_files),
            "review manifest inventory is incomplete or stale")
    lines = artifact_bytes(directory, execution.get("transcript")).decode("utf-8").splitlines()
    commands = execution.get("verification")
    require(isinstance(commands, dict) and set(commands) == set(VERIFICATION_COMMANDS),
            "review must include all five verification command logs")
    for command, result in commands.items():
        require(isinstance(result, dict) and type(result.get("exit_code")) is int and result["exit_code"] == 0
                and result.get("source_tree_sha256") == context.target["source_tree_sha256"],
                "verification failed or covers another source: " + command)
        artifact_bytes(directory, result.get("log"))
    cases = execution.get("case_reviews")
    require(isinstance(cases, list) and len(cases) == len(context.case_records), "review case coverage is incomplete")
    seen = set()
    for case in cases:
        require(isinstance(case, dict) and isinstance(case.get("case_id"), str) and isinstance(case.get("agent"), str),
                "malformed case review")
        key = (case["case_id"], case["agent"])
        require(key in context.case_records and key not in seen, "unknown or duplicate case review")
        seen.add(key)
        require(case.get("records") == dict(context.case_records[key]) and set(context.case_records[key]) == {"live", "replay"},
                "case review does not match both adopted Live and Replay records")
        require(case.get("status") == "PASS" and text_present(case.get("notes")), "case review is not a reasoned PASS")
        log_reference(case.get("log_lines"), lines)
    return lines
