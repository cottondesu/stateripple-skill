#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

if not __package__:
    from eval_common import EvaluationError, case_map, read_catalogs
else:
    from .eval_common import EvaluationError, case_map, read_catalogs


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect a StateRipple evaluation coverage profile")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--classification", choices=("CORE", "CONDITIONAL", "NOT_APPLICABLE"))
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--eval-dir", type=Path)
    return parser.parse_args(argv)


def describe_profile(profile_name: str, catalogs: Dict[str, Any], classification: Optional[str]) -> Dict[str, Any]:
    profiles = catalogs["profiles"]["profiles"]
    if profile_name not in profiles:
        raise EvaluationError("unknown coverage profile: %s" % profile_name)
    profile = profiles[profile_name]
    cases = case_map(catalogs)
    rows = []
    for case_id in sorted(cases):
        value = profile["cases"][case_id]
        if classification is None or value == classification:
            rows.append({"case_id": case_id, "title": cases[case_id]["title"], "classification": value})
    return {
        "schema_version": 1,
        "profile": profile_name,
        "description": profile["description"],
        "cases": rows,
        "note": "Profile classification declares applicability; it does not prove execution or quality.",
    }


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    eval_dir = (args.eval_dir or project_root / "tests/evals").resolve()
    catalogs = read_catalogs(eval_dir)
    print(json.dumps(describe_profile(args.profile, catalogs, args.classification), ensure_ascii=False, sort_keys=True))
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except EvaluationError as exc:
        print("eval-profile: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
