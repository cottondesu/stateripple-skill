from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

if not __package__:
    from eval_common import EvaluationError, load_json
    from review_execution import (REVIEW_CHECKS, ReviewContext, execution_evidence, log_reference,
                                  regular_bytes, require, sealed_json, text_present, utc_timestamp)
else:
    from .eval_common import EvaluationError, load_json
    from .review_execution import (REVIEW_CHECKS, ReviewContext, execution_evidence, log_reference,
                                   regular_bytes, require, sealed_json, text_present, utc_timestamp)

TRUST_LIMITATION = ("Unsigned local records and hashes detect inconsistency, not a malicious author who rewrites and "
                    "reseals a bundle. Session identity, actual independence, read-only execution, log truthfulness and "
                    "review quality still depend on trusted execution records and reviewer judgment.")
RECORD_FIELDS = {"schema_version", "record_type", "reviewer_kind", "reviewer_id", "model", "session_id", "reviewed_at",
                 "method", "skill_revision", "source_tree_sha256", "evidence_digest", "independence", "execution",
                 "evidence_review", "safety_contracts", "findings", "decision", "rationale", "integrity"}


def implementation_provenance(root: Path) -> Tuple[List[str], str]:
    path = root / "docs/release-implementation.json"
    regular_bytes(path)
    value = load_json(path)
    require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1
            and value.get("record_type") == "release-implementation", "missing frozen implementation provenance")
    sessions = value.get("sessions")
    require(isinstance(sessions, list) and bool(sessions) and all(text_present(item) for item in sessions)
            and len(set(sessions)) == len(sessions), "invalid frozen implementation sessions")
    baseline = value.get("baseline_source_tree_sha256")
    require(isinstance(baseline, str) and bool(re.fullmatch(r"[0-9a-f]{64}", baseline)), "missing frozen baseline digest")
    return sessions, baseline


def review_findings(value: Any) -> None:
    require(isinstance(value, list), "findings must be an array, including when empty")
    seen = set()
    for finding in value:
        require(isinstance(finding, dict) and set(finding) ==
                {"id", "severity", "description", "evidence", "status", "resolution"}, "malformed finding")
        require(all(text_present(finding[key]) for key in ("id", "description", "evidence")), "finding lacks evidence or identity")
        require(finding["id"] not in seen, "duplicate finding ID")
        seen.add(finding["id"])
        require(finding["severity"] in ("BLOCKER", "MAJOR", "MINOR"), "unknown finding severity")
        require(finding["status"] in ("OPEN", "RESOLVED", "ACCEPTED"), "unknown finding status")
        require(finding["status"] != "OPEN", "unresolved finding has no accepted resolution")
        require(finding["severity"] == "MINOR" or finding["status"] == "RESOLVED", "unresolved BLOCKER or MAJOR")
        require(text_present(finding["resolution"]), "finding needs resolution evidence or an acceptance rationale")


def validate_review(path: Path, record: Mapping[str, Any], context: ReviewContext) -> None:
    require(set(record) == RECORD_FIELDS and type(record.get("schema_version")) is int and record["schema_version"] == 1
            and record.get("record_type") == "ai-release-review" and record.get("reviewer_kind") == "ai",
            "not a complete ai-release-review schema 1 record")
    for key in ("reviewer_id", "model", "session_id"):
        require(isinstance(record[key], str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{2,159}", record[key])),
                "invalid reviewer identity: " + key)
    utc_timestamp(record["reviewed_at"])
    require(text_present(record["method"]) and text_present(record["rationale"]), "review method and decision rationale are required")
    for key, expected in context.target.items():
        require(record[key] == expected, "stale review target: " + key)
    require(record["decision"] in ("APPROVE", "REJECT"), "unknown review decision")
    sessions, baseline = implementation_provenance(context.project_root)
    independence = record["independence"]
    require(isinstance(independence, dict) and set(independence) ==
            {"implementation_sessions", "baseline_source_tree_sha256", "basis", "limitations"}
            and independence["implementation_sessions"] == sessions and text_present(independence["basis"])
            and independence["baseline_source_tree_sha256"] == baseline
            and text_present(independence["limitations"]), "independence needs frozen provenance, basis and trust limitations")
    require(record["session_id"] not in sessions, "implementation session cannot approve its own release")
    lines = execution_evidence(path, record, context)
    checks = record["evidence_review"]
    require(isinstance(checks, dict) and set(checks) == set(REVIEW_CHECKS), "missing or unknown evidence review checks")
    for name, check in checks.items():
        require(isinstance(check, dict) and set(check) == {"status", "notes", "log_lines"}
                and check["status"] == "PASS" and text_present(check["notes"]), "incomplete evidence inspection: " + name)
        log_reference(check["log_lines"], lines)
    contracts = record["safety_contracts"]
    require(isinstance(contracts, dict) and set(contracts) == set(context.contract_ids)
            and all(status == "PASS" for status in contracts.values()), "safety contract review is incomplete or failed")
    review_findings(record["findings"])


def ai_review_status(path: Optional[Path], context: ReviewContext) -> Dict[str, Any]:
    if path is None:
        return {"status": "NOT_RUN", "reasons": ["no independent AI release review was supplied"]}
    summary = {"trust_limitations": TRUST_LIMITATION}
    try:
        data = regular_bytes(path)
        summary["file_sha256"] = hashlib.sha256(data).hexdigest()
        record = sealed_json(path)
        validate_review(path, record, context)
    except (EvaluationError, OSError, ValueError, UnicodeError, RecursionError) as error:
        return dict(summary, status="FAIL", reasons=[str(error)])
    summary.update(reviewer_id=record["reviewer_id"], model=record["model"], session_id=record["session_id"],
                   reviewed_at=record["reviewed_at"], decision=record["decision"])
    if record["decision"] != "APPROVE":
        return dict(summary, status="FAIL", reasons=["the independent AI reviewer rejected this release"])
    return dict(summary, status="PASS", reasons=[])
