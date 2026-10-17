#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

if not __package__:
    from ai_release_review import ai_review_status
    from review_execution import ReviewContext
    from validate import CONTRACT_IDS
    from build import BuildError, package_entries, package_source_problems, verify_distribution
    from eval_common import (
        EvaluationError,
        case_map,
        MANIFEST_TYPES,
        discover_evidence,
        distinctive_lines,
        grade_evidence,
        invocation_modes,
        load_json,
        manifest_problems,
        read_catalogs,
        read_skill_files,
        review_problems,
        seal_record,
        sha256_file,
        sha256_tree,
        utc_now,
        verify_record,
        write_json_exclusive,
    )
else:
    from .ai_release_review import ai_review_status
    from .review_execution import ReviewContext
    from .validate import CONTRACT_IDS
    from .build import BuildError, package_entries, package_source_problems, verify_distribution
    from .eval_common import (
        EvaluationError,
        case_map,
        MANIFEST_TYPES,
        discover_evidence,
        distinctive_lines,
        grade_evidence,
        invocation_modes,
        load_json,
        manifest_problems,
        read_catalogs,
        read_skill_files,
        review_problems,
        seal_record,
        sha256_file,
        sha256_tree,
        utc_now,
        verify_record,
        write_json_exclusive,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report declared and evidenced StateRipple evaluation coverage")
    parser.add_argument("--profile")
    parser.add_argument("--release", action="store_true",
                        help="require every profile for both agents; cannot be narrowed")
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--agent", action="append", choices=("codex", "claude"))
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--eval-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--human-review", type=Path,
                        help="optional human maintainer release approval; every supplied review must approve")
    parser.add_argument("--ai-review", type=Path,
                        help="independent AI release approval with a bound execution evidence bundle")
    return parser.parse_args(argv)


def load_evidence_records(paths: Sequence[Path]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    records: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for path in discover_evidence(paths):
        value = load_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("evaluation_type"), str) \
                or value["evaluation_type"] not in {"live", "replay"}:
            continue
        verify_record(value)
        agent_value = value.get("agent")
        agent = agent_value.get("name") if isinstance(agent_value, dict) else None
        case_id = value.get("case_id")
        kind = value.get("evaluation_type")
        if isinstance(agent, str) and isinstance(case_id, str) and isinstance(kind, str):
            if (case_id,agent,kind) in records:
                raise EvaluationError('multiple evidence records for one case/agent/type; select one run explicitly')
            records[(case_id, agent, kind)] = value
    return records


def evidence_manifest_status(paths: Optional[Sequence[Path]], skill_revision: Optional[str] = None) -> Dict[str, Any]:
    """Whether the evidence given to the gate, and everything its manifests refer to, is intact.

    Each record is sealed on its own; the manifest ties it to the run that produced it. For every
    run directory an adopted record lives in, the manifest and all of its entries are validated:
    structure, seal, plain file names inside the directory, existence, file hash, the record's own
    seal, kind, case, agent, status and skill revision, duplicates, and files the manifest does not
    list. A record that is listed but not adopted (for example a kept failure) may have any status,
    but it must exist and be exactly what the manifest says.
    """
    empty = {"checked": 0, "manifests": 0, "entries": 0, "adopted": 0, "unadopted": 0}
    if paths is None:
        return dict(empty, status="NOT_RUN", problems=["evidence files were not given, so their manifests were not checked"])
    problems: List[str] = []
    adopted: Dict[Path, List[Path]] = {}
    for path in discover_evidence(paths):
        try:
            record = load_json(path)
        except (EvaluationError, OSError, ValueError):
            problems.append("%s/%s: unreadable evidence file" % (path.parent.name, path.name))
            continue
        if not isinstance(record, dict) or not isinstance(record.get("evaluation_type"), str) \
                or record["evaluation_type"] not in MANIFEST_TYPES:
            continue
        if path.is_symlink():
            problems.append("%s/%s: evidence must be a regular file, not a link" % (path.parent.name, path.name))
            continue
        adopted.setdefault(path.parent, []).append(path)
    entries = 0
    adopted_count = sum(len(files) for files in adopted.values())
    source_directories = {}
    for directory in adopted:
        try:
            manifest = load_json(directory / "manifest.json")
            if isinstance(manifest, dict) and manifest.get("evaluation_type") == "live-manifest":
                if directory.name in source_directories:
                    problems.append("%s: more than one selected live run has this name" % directory.name)
                source_directories[directory.name] = directory
        except EvaluationError:
            pass
    for directory in sorted(adopted):
        directory_problems, kind, records = manifest_problems(directory, skill_revision, source_directories)
        problems.extend(directory_problems)
        try:
            listed = load_json(directory / "manifest.json").get("records")
            entries += len(listed) if isinstance(listed, list) else 0
        except (EvaluationError, OSError, ValueError, AttributeError):
            pass
        for path in adopted[directory]:
            if path.name not in records and not any(problem.startswith("%s/%s:" % (directory.name, path.name))
                                                    for problem in directory_problems):
                problems.append("%s/%s: not a valid entry of the manifest of its run" % (directory.name, path.name))
    if not adopted_count:
        problems.append("no evidence files were checked")
    return {"status": "FAIL" if problems else "PASS", "checked": adopted_count, "manifests": len(adopted), "entries": entries,
            "adopted": adopted_count, "unadopted": max(entries - adopted_count, 0), "problems": problems}


def case_dimensions(
    case: Mapping[str, Any],
    classification: str,
    agent: str,
    records: Mapping[Tuple[str, str, str], Mapping[str, Any]],
    skill_revision: Optional[str] = None,
    skill_reference: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    live = records.get((case["id"], agent, "live"))
    replay = records.get((case["id"], agent, "replay"))
    live_seal = verify_record(live) if live is not None else None
    replay_source = replay.get('source') if replay is not None and isinstance(replay.get('source'), Mapping) else {}
    if replay is not None and (live is None or replay_source.get('record_sha256') != live_seal):
        raise EvaluationError('replay does not belong to the selected live execution')
    # Evidence from another skill revision is history, not evidence for this build.
    skill_current = bool(live) and (skill_revision is None or live.get('skill_revision') == skill_revision)
    prompt_current = bool(live) and prompt_matches_case(case, live)
    process = live.get("process") if live and isinstance(live.get("process"), Mapping) else {}
    observations = live.get("observations") if live and isinstance(live.get("observations"), Mapping) else {}
    executed = bool(skill_current and prompt_current and live.get("precondition_status") == "PASS" and process.get("exit_code") is not None)
    expected_route = case["expected"]["route"]
    invocations = observations.get("cli_invocations", [])
    modes = invocation_modes(invocations if isinstance(invocations, list) else [])
    if not executed:
        cli_evidence = False
    elif expected_route == "none":
        cli_evidence = not modes
    else:
        cli_evidence = expected_route in modes
    # The stored verdict must survive the current grader; a replay must agree as well.
    # A record whose tool outputs were compared with other text than the current skill's decides nothing here.
    reference_current = skill_reference is None or (
        executed and isinstance(observations.get("agent_events"), Mapping)
        and observations["agent_events"].get("skill_reference") == dict(skill_reference))
    grader_pass = bool(executed and reference_current
                       and isinstance(live.get("automatic"), Mapping) and live["automatic"].get("status") == "PASS"
                       and grade_evidence(case, live)["status"] == "PASS"
                       and (replay is None or (isinstance(replay.get("automatic"), Mapping)
                                               and replay["automatic"].get("status") == "PASS")))
    # Only a replay carries a review bound to the sealed live record, and the gate checks that review itself:
    # a replay written by another tool or an older version is not trusted to have validated it.
    problems = review_problems(case, live, replay) if executed and replay is not None else []
    if not executed or replay is None:
        manual_status = "NOT_RUN"
    elif problems:
        manual_status = "FAIL"
    else:
        manual_status = replay["manual"]["status"]
    return {
        "case_id": case["id"],
        "classification": classification,
        "declared": classification != "NOT_APPLICABLE",
        "skill_current": skill_current,
        "prompt_current": prompt_current,
        "test_executed": executed,
        "cli_evidence": cli_evidence,
        "grader_pass": grader_pass,
        "manual_complete": manual_status in {"PASS", "FAIL"},
        "manual_status": manual_status,
        'manual_pass': manual_status == 'PASS',
        "review_problems": problems,
        "live_status": live.get("status") if live else "NOT_RUN",
        "replay_status": replay.get("status") if replay else "NOT_RUN",
    }


_PROMPT_PATHS = {"fixture": "<EVAL_TMP>/workspace/fixture.json", "output": "<EVAL_TMP>/workspace/existing-result.json",
                 "workspace": "<EVAL_TMP>/workspace"}


def prompt_matches_case(case: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    """Whether the record was produced with the prompt the catalog declares now, turn by turn."""
    def rendered(text: Any) -> Optional[str]:
        try:
            return text.format(**_PROMPT_PATHS) if isinstance(text, str) else None
        except (KeyError, IndexError, ValueError):
            return None

    def starts(recorded: Any, expected: Optional[str]) -> bool:
        return isinstance(recorded, str) and isinstance(expected, str) and recorded.startswith(expected + "\nEvaluation boundary:")

    followup = case.get("followup") if isinstance(case.get("followup"), Mapping) else None
    if followup is None:
        return starts(live.get("prompt"), rendered(case.get("prompt")))
    observations = live.get("observations") if isinstance(live.get("observations"), Mapping) else {}
    turns = observations.get("turns")
    return (isinstance(turns, list) and len(turns) == 2 and all(isinstance(turn, Mapping) for turn in turns)
            and starts(turns[0].get("prompt"), rendered(case.get("prompt")))
            and starts(turns[1].get("prompt"), rendered(followup.get("prompt"))))


def build_coverage(
    profile_name: str,
    catalogs: Mapping[str, Any],
    records: Mapping[Tuple[str, str, str], Mapping[str, Any]],
    agents: Sequence[str],
    skill_revision: Optional[str] = None,
    skill_reference: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    profiles = catalogs["profiles"]["profiles"]
    if profile_name not in profiles:
        raise EvaluationError("unknown coverage profile: %s" % profile_name)
    profile = profiles[profile_name]
    cases = case_map(catalogs)
    rows = []
    for agent in agents:
        for case_id in sorted(cases):
            rows.append(case_dimensions(cases[case_id], profile["cases"][case_id], agent, records, skill_revision,
                                        skill_reference))
            rows[-1]["agent"] = agent
    return {
        "schema_version": 1,
        "evaluation_type": "coverage",
        "created_at": utc_now(),
        "profile": profile_name,
        "description": profile["description"],
        "agents": list(agents),
        "skill_revision": skill_revision,
        "dimensions": ["declared", "test_executed", "cli_evidence", "grader_pass", "manual_complete"],
        "rows": rows,
        "note": "Coverage dimensions are evidence states, not a quality percentage or success probability.",
    }


RELEASE_AGENTS = ("codex", "claude")


def release_gaps(reports: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """CORE rows that lack current, graded, reviewed live evidence."""
    return [{"profile": report["profile"], "agent": row["agent"], "case_id": row["case_id"]}
            for report in reports for row in report["rows"]
            if row["classification"] == "CORE"
            and not (row["test_executed"] and row["cli_evidence"] and row["grader_pass"] and row["manual_pass"])]


def activation_coverage(
    catalogs: Mapping[str, Any],
    records: Mapping[Tuple[str, str, str], Mapping[str, Any]],
    agents: Sequence[str],
    skill_revision: Optional[str] = None,
    skill_reference: Optional[Mapping[str, int]] = None,
) -> List[Dict[str, Any]]:
    """One row per positive and negative activation case and agent, judged like a CORE live case."""
    cases = case_map(catalogs, include_activation=True)
    rows = []
    for agent in agents:
        for group in ("positive", "negative"):
            for item in catalogs["activation"][group]:
                row = case_dimensions(cases[item["id"]], "CORE", agent, records, skill_revision, skill_reference)
                row.update(agent=agent, group=group)
                rows.append(row)
    return rows


def activation_gaps(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    return [{"agent": row["agent"], "case_id": row["case_id"], "group": row["group"]} for row in rows
            if not (row["test_executed"] and row["cli_evidence"] and row["grader_pass"] and row["manual_pass"])]


# What the maintainer confirms personally. The gate needs every item answered true.
HUMAN_REVIEW_CHECKLIST = (
    ("H01", "I am a human maintainer of this project; no AI system wrote or submitted this record for me."),
    ("H02", "The source tree I reviewed is the frozen release candidate and matches source_tree_sha256 below."),
    ("H03", "I read src/stateripple/SKILL.md and the four reference files and accept them as the v0.1.0 skill text."),
    ("H04", "I ran, or watched, make build, make check, make test, make cli-compat and make release-check and they exited 0."),
    ("H05", "I read live answers myself for both agents, including L05, L06, L07, L11, L13 and L14, and agree with their verdicts."),
    ("H06", "I read positive and negative activation answers for both agents and agree with their verdicts."),
    ("H07", "I understand the agent reviews and audits were done by AI systems and I do not treat them as my review."),
    ("H08", "I read the known limitations in CHANGELOG.md and docs/EVALUATION.md, including L14's incomplete Git monitoring, and accept them for this release."),
    ("H09", "I checked that no secrets, personal paths or unrelated content are in the files to be published."),
    ("H10", "I decide the commit, tag, push and release myself; none was made by an AI system."),
)
HUMAN_ATTESTATION = "I am a human maintainer and I reviewed this release myself; this record was not written by an AI system."
SOURCE_TREE_PATHS = ("src", "scripts", "tests", "docs", ".github", "Makefile", "README.md", "README.ja.md",
                     "CHANGELOG.md", "LICENSE", ".gitignore")


def source_tree_files(project_root: Path) -> List[Path]:
    """The files the digest covers.

    Inside the package source nothing is skipped: the list is the build's own input list, so any
    entry that changes or stops the build changes the digest. Elsewhere only interpreter caches and
    Finder metadata are skipped; they are ignored by Git, never published, and cannot affect a build.
    """
    package = project_root / "src" / "stateripple"
    files = []
    for name in SOURCE_TREE_PATHS:
        path = project_root / name
        if path.is_symlink() or path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(item for item in path.rglob("*")
                         if (item.is_symlink() or item.is_file()) and package not in item.parents
                         and "__pycache__" not in item.parts and item.suffix not in (".pyc", ".pyo") and item.name != ".DS_Store")
    if package.is_dir() and not package.is_symlink():
        files.extend(package_entries(package))
    return sorted(set(files), key=lambda item: item.relative_to(project_root).as_posix())


def source_tree_sha256(project_root: Path) -> str:
    """Digest of every file a release publishes or that decides its verdicts; evidence and the review record are outside it."""
    digest = hashlib.sha256()
    for path in source_tree_files(project_root):
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big") + relative)
        data = os.readlink(str(path)).encode("utf-8") if path.is_symlink() else path.read_bytes() if path.is_file() else b"<special>"
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def package_status(project_root: Path, skill_revision: str) -> Dict[str, Any]:
    """Whether the package source can be built and the built skill is that source."""
    problems = package_source_problems(project_root)
    if not problems:
        try:
            verify_distribution(project_root)
        except (BuildError, OSError, ValueError, KeyError) as error:
            problems.append("built skill does not match the source: %s" % error)
    if not problems and sha256_tree(project_root / "src" / "stateripple") != skill_revision:
        problems.append("built skill revision differs from the package source revision")
    return {"status": "FAIL" if problems else "PASS", "problems": problems}


def evidence_digest(records: Mapping[Tuple[str, str, str], Mapping[str, Any]]) -> str:
    """Digest of exactly which sealed records the gate was computed from."""
    entries = sorted([case_id, agent, kind, verify_record(record)] for (case_id, agent, kind), record in records.items())
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode("utf-8")).hexdigest()


def human_review_status(path: Optional[Path], target: Mapping[str, str]) -> Dict[str, Any]:
    """NOT_RUN without a record; PASS only for a complete human approval of exactly this revision and evidence."""
    if path is None:
        return {"status": "NOT_RUN", "reasons": ["no human review record was supplied"]}
    if not path.is_file():
        return {"status": "NOT_RUN", "reasons": ["human review record does not exist: %s" % path.name]}
    try:
        record = load_json(path)
    except EvaluationError as error:
        return {"status": "FAIL", "reasons": ["human review record is not valid JSON: %s" % error]}
    reasons = []
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("record_type") != "human-release-review":
        return {"status": "FAIL", "reasons": ["not a human-release-review record"]}
    reviewer = record.get("reviewer")
    if record.get("reviewer_kind") != "human":
        reasons.append("reviewer_kind must be human; agent reviews and audits are not human review")
    if not isinstance(reviewer, str) or not reviewer.strip():
        reasons.append("reviewer must name the maintainer")
    elif re.search(r"claude|codex|chatgpt|gpt|copilot|gemini|\bai\b|\bagent\b|\bbot\b|assistant|subagent|llm", reviewer, re.I):
        reasons.append("reviewer names an AI system")
    if record.get("attestation") != HUMAN_ATTESTATION:
        reasons.append("attestation text is missing or altered")
    if not isinstance(record.get("reviewed_at"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", record["reviewed_at"]):
        reasons.append("reviewed_at must be an ISO 8601 UTC timestamp")
    else:
        try:
            datetime.strptime(record["reviewed_at"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            reasons.append("reviewed_at is not a real date")
    for key in ("skill_revision", "source_tree_sha256", "evidence_digest"):
        if record.get(key) != target[key]:
            reasons.append("%s does not match what the gate computed; the review covers another revision or evidence set" % key)
    checklist = record.get("checklist")
    required = [item_id for item_id, _ in HUMAN_REVIEW_CHECKLIST]
    if not isinstance(checklist, dict) or sorted(checklist) != sorted(required):
        reasons.append("checklist must answer exactly %s" % ", ".join(required))
    elif any(checklist[item_id] is not True for item_id in required):
        reasons.append("unconfirmed checklist items: %s" % ", ".join(item for item in required if checklist[item] is not True))
    if not isinstance(record.get("notes"), str) or not record["notes"].strip():
        reasons.append("notes must say what the maintainer looked at")
    decision = record.get("decision")
    if decision not in ("APPROVE", "REJECT"):
        reasons.append("decision must be APPROVE or REJECT")
    summary = {"reviewer": reviewer if isinstance(reviewer, str) else None, "reviewed_at": record.get("reviewed_at"),
               "decision": decision, "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    if reasons:
        return dict(summary, status="FAIL", reasons=reasons)
    if decision == "REJECT":
        return dict(summary, status="FAIL", reasons=["the maintainer rejected this release"])
    return dict(summary, status="PASS", reasons=[])


def current_skill_reference(skill_source: Path) -> Optional[Dict[str, int]]:
    """Distinctive line counts of the built skill, as the live runner records them; None when it is not built.

    A missing build is reported by the package check, which blocks the gate on its own.
    """
    if not skill_source.is_dir():
        return None
    return {name: len(distinctive_lines(text)) for name, text in read_skill_files(skill_source).items()}


def release_report(catalogs: Mapping[str, Any], records: Mapping[Tuple[str, str, str], Mapping[str, Any]],
                   skill_revision: str, project_root: Path, human_review: Optional[Path],
                   evidence_paths: Optional[Sequence[Path]] = None, ai_review: Optional[Path] = None) -> Dict[str, Any]:
    agents = list(RELEASE_AGENTS)
    reference = current_skill_reference(project_root / "dist" / "stateripple")
    reports = [build_coverage(name, catalogs, records, agents, skill_revision, reference)
               for name in sorted(catalogs["profiles"]["profiles"])]
    activation = activation_coverage(catalogs, records, agents, skill_revision, reference)
    target = {"skill_revision": skill_revision, "source_tree_sha256": source_tree_sha256(project_root),
              "evidence_digest": evidence_digest(records)}
    review = human_review_status(human_review, target)
    source_files = {path.relative_to(project_root).as_posix(): hashlib.sha256(
        os.readlink(str(path)).encode("utf-8") if path.is_symlink() else path.read_bytes()).hexdigest()
        for path in source_tree_files(project_root)}
    manifest_files = {}
    if ai_review is not None and evidence_paths is not None:
        for directory in sorted({path.parent for path in discover_evidence(evidence_paths)}):
            manifest = directory / "manifest.json"
            if manifest.is_file() and not manifest.is_symlink():
                label = os.path.relpath(str(manifest), str(project_root))
                manifest_files[label] = sha256_file(manifest)
    case_records = {}
    for case_id in case_map(catalogs, include_activation=True):
        for agent in RELEASE_AGENTS:
            case_records[(case_id, agent)] = {kind: verify_record(records[(case_id, agent, kind)])
                                             for kind in ("live", "replay") if (case_id, agent, kind) in records}
    context = ReviewContext(project_root, target, source_files, manifest_files, case_records,
                            sorted(CONTRACT_IDS))
    ai = ai_review_status(ai_review, context)
    package = package_status(project_root, skill_revision)
    manifests = evidence_manifest_status(evidence_paths, skill_revision)
    gaps, missing_activation = release_gaps(reports), activation_gaps(activation)
    blocking = []
    if manifests["status"] != "PASS":
        blocking.append("evidence manifests: %s" % "; ".join(manifests["problems"][:5]))
    if package["status"] != "PASS":
        blocking.append("package source cannot be built or differs from the built skill: %s" % "; ".join(package["problems"]))
    if gaps:
        blocking.append("%d required live rows lack current, graded, reviewed evidence" % len(gaps))
    if missing_activation:
        blocking.append("%d required activation rows lack current, graded, reviewed evidence" % len(missing_activation))
    supplied = [(name, result) for path, name, result in
                ((human_review, "human review", review), (ai_review, "independent AI review", ai)) if path is not None]
    for name, result in supplied:
        if result["status"] != "PASS":
            blocking.append("%s is %s" % (name, result["status"]))
    if not supplied:
        blocking.append("release approval is NOT_RUN; supply an independent AI review or human review")
    if human_review is None and ai_review is not None:
        review = {"status": "NOT_REQUIRED", "reasons": ["the independent AI approval route was selected"]}
    approval = {"status": "NOT_RUN" if not supplied else "PASS" if all(result["status"] == "PASS" for _, result in supplied) else "FAIL"}
    return {"schema_version": 1, "evaluation_type": "release-coverage", "created_at": utc_now(),
            "agents": agents, "skill_revision": skill_revision, "required_suites": list(catalogs["live_suites"]["release_gate"]["suites"]),
            "profiles": reports, "gaps": gaps, "activation": activation, "activation_gaps": missing_activation,
            "package": package, "evidence_manifests": manifests, "review_target": target, "human_review": review,
            "ai_review": ai, "release_approval": approval,
            "readiness": "NOT READY" if blocking else "READY", "blocking": blocking,
            "note": "READY requires every technical check and at least one valid independent AI or Human release approval. "
                    "Every supplied review must approve. Grader and Replay reviews alone cannot approve a release. "
                    "READY does not publish anything."}


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    if args.release and (args.eval_dir or args.skill_source):
        # The human review binds the project tree; the gate must not read catalogs or the skill from elsewhere.
        raise EvaluationError("--release reads catalogs and the built skill from the project root only")
    eval_dir = (args.eval_dir or project_root / "tests/evals").resolve()
    catalogs = read_catalogs(eval_dir)
    evidence_paths = [path.resolve() for path in args.evidence]
    records = load_evidence_records(evidence_paths)
    skill_source = (args.skill_source or project_root / "dist/stateripple").resolve()
    if not skill_source.is_dir():
        raise EvaluationError("built skill does not exist; run the build first: %s" % skill_source)
    skill_revision = sha256_tree(skill_source)
    if args.release:
        if args.profile or args.agent:
            raise EvaluationError("--release always covers every profile and both agents")
        profile_names, agents = sorted(catalogs["profiles"]["profiles"]), list(RELEASE_AGENTS)
    else:
        if not args.profile:
            raise EvaluationError("--profile or --release is required")
        profile_names, agents = [args.profile], args.agent or list(RELEASE_AGENTS)
    if (args.human_review or args.ai_review) and not args.release:
        raise EvaluationError("--human-review and --ai-review apply only to --release")
    if args.release:
        coverage = seal_record(release_report(catalogs, records, skill_revision, project_root,
                                              args.human_review.resolve() if args.human_review else None, evidence_paths,
                                              args.ai_review))
        ready = coverage["readiness"] == "READY"
    else:
        reports = [build_coverage(name, catalogs, records, agents, skill_revision, current_skill_reference(skill_source))
                   for name in profile_names]
        coverage = seal_record(reports[0])
        ready = not release_gaps(reports)
    if args.output:
        write_json_exclusive(args.output.resolve(), coverage)
    print(json.dumps(coverage, ensure_ascii=False, sort_keys=True))
    return 0 if ready else 1


def main() -> None:
    try:
        raise SystemExit(run())
    except EvaluationError as exc:
        print("eval-coverage: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
