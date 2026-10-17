#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

if not __package__:
    from eval_common import (
        EvaluationError,
        case_map,
        final_status,
        grade_evidence,
        load_json,
        manifest_problems,
        read_catalogs,
        seal_record,
        sha256_file,
        utc_now,
        verify_record,
        write_json_exclusive,
    )
else:
    from .eval_common import (
        EvaluationError,
        case_map,
        final_status,
        grade_evidence,
        load_json,
        manifest_problems,
        read_catalogs,
        seal_record,
        sha256_file,
        utc_now,
        verify_record,
        write_json_exclusive,
    )


REVIEWER_KINDS = {"human", "agent"}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay sealed StateRipple live evidence without running agents or StateRipple")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--manual-review", type=Path)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--eval-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def manifest_records(evidence: Path) -> List[Path]:
    """The live records to replay, after the run they belong to has been validated as a whole.

    The same validation the release gate applies: the sealed manifest of the run directory, every
    entry's file, hash, own seal, kind, case, agent, status and revision, duplicates, and unlisted
    files. A single record may be named instead of its directory; it must still be an entry of a
    manifest that validates.
    """
    directory = evidence.parent if evidence.is_file() else evidence
    problems, kind, records = manifest_problems(directory, None)
    if problems:
        raise EvaluationError("live evidence manifest does not validate: %s" % "; ".join(problems[:5]))
    if kind != "live":
        raise EvaluationError("replay source is not a live evidence run")
    if evidence.is_file():
        if evidence.name not in records or evidence.is_symlink():
            raise EvaluationError("the named record is not an entry of the manifest of its run")
        return [evidence]
    return [directory / name for name in sorted(records)]


def load_manual_reviews(path: Optional[Path]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if path is None:
        return {}
    paths = [path] if path.is_file() else sorted(path.glob("*.json"))
    reviews: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for review_path in paths:
        value = load_json(review_path)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise EvaluationError("invalid manual review: %s" % review_path)
        case_id = value.get("case_id")
        agent = value.get("agent")
        status = value.get("status")
        if not isinstance(case_id, str) or not isinstance(agent, str) or status not in {"PASS", "FAIL"}:
            raise EvaluationError("manual review must identify case, agent, and PASS or FAIL")
        if not isinstance(value.get("evidence_sha256"), str):
            raise EvaluationError("manual review must bind to an evidence hash")
        if not isinstance(value.get("reviewer"), str) or not value["reviewer"].strip():
            raise EvaluationError("manual review must name a reviewer")
        reviewed_at = value.get("reviewed_at")
        if not isinstance(reviewed_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", reviewed_at):
            raise EvaluationError("manual review reviewed_at must be an ISO 8601 UTC timestamp")
        if value.get("reviewer_kind") not in REVIEWER_KINDS:
            raise EvaluationError("manual review must say whether the reviewer is human or agent")
        if not isinstance(value.get("notes"), str) or not value["notes"].strip():
            raise EvaluationError("manual review must record what was inspected in notes")
        contracts = value.get("contracts")
        if not isinstance(contracts, dict) or any(result not in {"PASS", "FAIL"} for result in contracts.values()):
            raise EvaluationError("manual review must give PASS or FAIL per contract ID")
        key = (case_id, agent)
        if key in reviews:
            raise EvaluationError("duplicate manual review for %s/%s" % key)
        reviews[key] = value
    return reviews


def replay_record(
    source: Mapping[str, Any],
    source_path: Path,
    case: Mapping[str, Any],
    manual_reviews: Mapping[Tuple[str, str], Mapping[str, Any]],
) -> Dict[str, Any]:
    source_seal = verify_record(source)
    if source.get("evaluation_type") != "live":
        raise EvaluationError("replay source is not live evidence: %s" % source_path)
    agent_value = source.get("agent")
    agent_name = agent_value.get("name") if isinstance(agent_value, dict) else None
    if agent_name not in ('codex', 'claude'):
        raise EvaluationError("live evidence has no agent name")
    automatic = grade_evidence(case, source)
    original = source.get("automatic")
    original_status = original.get("status") if isinstance(original, dict) else None
    deterministic_match = original_status == automatic["status"]
    automatic["original_status"] = original_status
    automatic["deterministic_match"] = deterministic_match

    review = manual_reviews.get((case["id"], agent_name))
    if review is None:
        manual = {"status": "NOT_RUN", "reviewer": None, "reviewed_at": None, "notes": None}
    else:
        contracts = review.get("contracts") if isinstance(review.get("contracts"), dict) else {}
        required = set(case["expected"].get("required_contracts", []))
        if review["evidence_sha256"] != source_seal:
            status, notes = "FAIL", "manual review evidence hash does not match"
        elif set(contracts) != required:
            status, notes = "FAIL", "manual review does not cover exactly the contracts this case requires"
        elif review["status"] == "PASS" and any(result != "PASS" for result in contracts.values()):
            status, notes = "FAIL", "manual review claims PASS while a contract is marked FAIL"
        else:
            status, notes = review["status"], review.get("notes")
        manual = {
            "status": status,
            "reviewer": review["reviewer"],
            "reviewer_kind": review.get("reviewer_kind"),
            "reviewed_at": review["reviewed_at"],
            "evidence_sha256": review["evidence_sha256"],
            "contracts": dict(contracts),
            "notes": notes,
        }

    replay: Dict[str, Any] = {
        "schema_version": 1,
        "evaluation_type": "replay",
        "case_id": case["id"],
        "recorded_at": utc_now(),
        "agent": dict(agent_value),
        "skill_revision": source.get("skill_revision"),
        "fixture_id": source.get("fixture_id"),
        "source": {
            "evaluation_type": "live",
            "file": source_path.name,
            "record_sha256": source_seal,
            "file_sha256": sha256_file(source_path),
        },
        "automatic": automatic,
        "manual": manual,
        "status": final_status(automatic["status"], manual["status"]),
    }
    return seal_record(replay)


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    eval_dir = (args.eval_dir or project_root / "tests/evals").resolve()
    output_parent = (args.output_dir or project_root / "artifacts/evaluations/replay").resolve()
    catalogs = read_catalogs(eval_dir)
    cases = case_map(catalogs,include_activation=True)
    source_paths = manifest_records(args.evidence.resolve())
    source_hashes = {path: sha256_file(path) for path in source_paths}
    reviews = load_manual_reviews(args.manual_review.resolve() if args.manual_review else None)
    run_dir = output_parent / (utc_now().replace(":", "").replace("-", "") + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    records = []
    statuses = []
    for source_path in source_paths:
        source = load_json(source_path)
        if not isinstance(source, dict):
            raise EvaluationError("live evidence must be a JSON object")
        case_id = source.get("case_id")
        if not isinstance(case_id, str) or case_id not in cases:
            raise EvaluationError("live evidence references an unknown case")
        replay = replay_record(source, source_path, cases[case_id], reviews)
        agent = replay["agent"]["name"]
        destination = run_dir / (case_id + "-" + agent + ".json")
        write_json_exclusive(destination, replay)
        records.append({"case_id": case_id, "agent": agent, "file": destination.name, "sha256": sha256_file(destination), "status": replay["status"]})
        statuses.append(replay["status"])
    for path, original_hash in source_hashes.items():
        if sha256_file(path) != original_hash:
            raise EvaluationError("source evidence changed during replay: %s" % path)
    manifest = seal_record(
        {
            "schema_version": 1,
            "evaluation_type": "replay-manifest",
            "created_at": utc_now(),
            "source": source_paths[0].parent.name,
            "records": records,
        }
    )
    write_json_exclusive(run_dir / "manifest.json", manifest)
    print(json.dumps({"run_dir": str(run_dir), "statuses": {status: statuses.count(status) for status in sorted(set(statuses))}}, sort_keys=True))
    if 'FAIL' in statuses:
        return 1
    return 2 if any(status in {'BLOCKED', 'NOT_RUN'} for status in statuses) else 0


def main() -> None:
    try:
        raise SystemExit(run())
    except EvaluationError as exc:
        print("eval-replay: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
