#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
# --- How to run ---
# python3 scripts/validate.py
from __future__ import annotations

import json
import ast
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Set

CONTRACT_IDS = {
    "CLI-001", "CLI-002", "CLI-003", "ROUTE-001", "ROUTE-002", "ROUTE-003",
    "ROUTE-004", "RISK-001", "RISK-002", "TRACE-001", "TRACE-002", "COMP-001",
    "COMP-002", "COMP-003", "COMP-004", "TEST-001", "SAFE-001", "SAFE-002",
    "SAFE-003", "SAFE-004", "SAFE-005", "SAFE-006",
}
REQUIRED_LIVE_CASES = {'L%02d' % number for number in range(1, 15)}
REFERENCE_FILES = {
    "references/cli-contract.md", "references/routing.md",
    "references/result-interpretation.md", "references/safety-contract.md",
}


class ValidationError(RuntimeError):
    pass


def _load_json(path: Path) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError("%s: invalid JSON: %s" % (path, error)) from error
    if not isinstance(value, dict):
        raise ValidationError("%s: top level must be an object" % path)
    if value.get("schema_version") != 1:
        raise ValidationError("%s: schema_version must be 1" % path)
    return value


def _require_list(value: object, label: str) -> List[object]:
    if not isinstance(value, list):
        raise ValidationError("%s must be an array" % label)
    return value


# Pinned digest of the 22 normative rows in references/safety-contract.md.
SAFETY_TABLE_SHA256 = '4682293a4a8eba7b4d7550288de8ab443c6bdfe318db5f0c18c5a16dc160fb4e'

# Normative statements whose removal or inversion must fail static validation.
REQUIRED_STATEMENTS = {
    'SKILL.md': (
        'Never implement or estimate propagation yourself.',
        'require exactly `stateripple v0.1.0`',
        'Treat risk as priority, not probability.',
        'risk 0 is inactive in this analysis, not safe',
        '`test_targets` are candidates selected by the report, not tests run by StateRipple.',
        'Exit 2 or `status: incomplete` is a usable partial result only when both agree.',
        'Never turn a failed `analyze` into `simulate`',
        'retain tied causes',
    ),
    'references/cli-contract.md': (
        '| 0 | Complete result | Require valid JSON with `status: complete`; otherwise fail closed. |',
        '| 1 | Input, usage, requirement, execution, or output failure | Do not interpret as an analysis result',
        '| 2 | Incomplete result | Require valid JSON with `status: incomplete`; report partial findings and their limit. |',
        'adapter incompleteness can produce `status: incomplete` with `stop_reason: converged`',
        'A present file therefore does not establish success.',
        '`analyze --base` defaults to `HEAD~1`',
        '`max_generations` is optional and normalizes to 16',
    ),
    'references/result-interpretation.md': (
        'exit 0 with `status: complete`, or exit 2 with `status: incomplete`',
        'Exit 1 is a failure even when its stdout happens to parse as a report.',
        '| 0 | Dormant | Inactive in this analysis; not proof of safety or absence of dependency. |',
        'never a probability',
        'Convergence does not upgrade adapter completeness.',
        '`cells[].causes` records support for the final maximum risk.',
        '`waves[].changes[].causes` records why a value changed at a particular generation.',
        'All causes tied for the maximum are significant.',
        'An empty array does not mean tests are unnecessary. StateRipple never runs tests.',
        'the old report is historical evidence only',
    ),
    'references/routing.md': (
        '| An `analyze` prerequisite or run fails | Report the failure; do not fall back to `simulate` |',
        'Never modify, stash, clean, commit, or reset a worktree to satisfy this preflight.',
        'Do not create temporary or scratch files to hold stdout or stderr unless the user authorized a location',
    ),
}


def _validate_statements(skill_dir: Path) -> None:
    for relative, statements in REQUIRED_STATEMENTS.items():
        text = ' '.join((skill_dir / relative).read_text(encoding='utf-8').split())
        for statement in statements:
            if ' '.join(statement.split()) not in text:
                raise ValidationError('%s lost a normative statement: %s' % (relative, statement))


def _validate_skill(root: Path) -> None:
    skill = root / "src" / "stateripple" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise ValidationError("SKILL.md requires YAML frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            if key.strip() in fields:
                raise ValidationError('duplicate frontmatter key')
            fields[key.strip()] = value.strip()
    if set(fields) != {'name','description'}:
        raise ValidationError('unsupported or missing frontmatter fields')
    if fields.get("name") != "stateripple":
        raise ValidationError("SKILL.md name must be stateripple")
    description = fields.get("description", "")
    if not description or len(description) > 1024 or len(text.encode('utf-8')) > 8192:
        raise ValidationError('skill metadata or entrypoint exceeds size contract')
    if not all(token in description.lower() for token in ("stateripple", "simulate", "analyze")):
        raise ValidationError("description must carry StateRipple simulate/analyze routing terms")
    links = set(re.findall(r"\]\((references/[^)#]+\.md)(?:#[^)]+)?\)", text))
    if not REFERENCE_FILES.issubset(links):
        raise ValidationError("SKILL.md must link every required reference")
    for relative in REFERENCE_FILES:
        if not (skill.parent / relative).is_file():
            raise ValidationError("missing skill reference: %s" % relative)
    safety = (skill.parent / 'references/safety-contract.md').read_text(encoding='utf-8')
    ids = re.findall(r'^\| ([A-Z]+-\d{3}) \|',safety,re.MULTILINE)
    if set(ids) != CONTRACT_IDS or len(ids) != len(CONTRACT_IDS):
        raise ValidationError('normative safety prose must declare all unique contracts')
    rows = re.findall(r'^\| [A-Z]+-\d{3} \|.*$',safety,re.MULTILINE)
    if hashlib.sha256('\n'.join(rows).encode('utf-8')).hexdigest() != SAFETY_TABLE_SHA256:
        raise ValidationError('normative safety contract rows changed; review them and update SAFETY_TABLE_SHA256 deliberately')
    _validate_statements(skill.parent)


def _local_path(root: Path, relative: str) -> Path:
    if not isinstance(relative,str) or Path(relative).is_absolute():
        raise ValidationError('reference must be a repository-relative path')
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as error:
        raise ValidationError('reference escapes repository') from error
    return target


def _validate_contracts(root: Path) -> Set[str]:
    catalog = _load_json(root / "tests" / "evals" / "contracts.json")
    contracts = _require_list(catalog.get("contracts"), "contracts")
    ids: Set[str] = set()
    required_fields = {"requirement", "basis", "implementation", "fixtures", "automated", "live", "manual", "unverified"}
    for index, value in enumerate(contracts):
        if not isinstance(value, dict):
            raise ValidationError("contracts[%d] must be an object" % index)
        contract_id = value.get("id")
        if not isinstance(contract_id, str) or contract_id in ids:
            raise ValidationError("contract IDs must be unique strings")
        ids.add(contract_id)
        missing = required_fields - set(value)
        if missing:
            raise ValidationError("%s missing trace fields: %s" % (contract_id, sorted(missing)))
        for field in required_fields - {"requirement"}:
            _require_list(value[field], "%s.%s" % (contract_id, field))
        for relative in value["implementation"] + value["fixtures"]:
            target = str(relative).split("#", 1)[0]
            if target and not _local_path(root,target).is_file():
                raise ValidationError("%s references missing path: %s" % (contract_id, target))
        if not isinstance(value['requirement'],str) or not value['requirement'].strip() or not value['basis'] or not value['implementation'] or not value['fixtures']:
            raise ValidationError('contract requires concrete traceability')
        if not value['automated'] and not value['live']:
            raise ValidationError('%s has neither automated nor live verification' % contract_id)
        if not value['manual'] or not value['unverified']:
            raise ValidationError('%s must state its manual review and unverified limits' % contract_id)
        if not value['live'] and not any('No live case' in str(item) for item in value['unverified']):
            raise ValidationError('%s has no live case and must say so under unverified' % contract_id)
        for selector in value['automated']:
            path, separator, name = selector.partition('::')
            if not separator or '.' not in name:
                raise ValidationError('automated selector must name test class and method')
            tree = ast.parse(_local_path(root,path).read_text(encoding='utf-8'))
            cls, method = name.split('.',1)
            if not any(isinstance(node,ast.ClassDef) and node.name == cls and any(isinstance(member,ast.FunctionDef) and member.name == method for member in node.body) for node in tree.body):
                raise ValidationError('automated test selector does not exist: ' + selector)
    if ids != CONTRACT_IDS:
        raise ValidationError("contract ID set mismatch: missing=%s extra=%s" % (sorted(CONTRACT_IDS - ids), sorted(ids - CONTRACT_IDS)))
    baseline = catalog.get("baseline")
    if not isinstance(baseline, dict) or baseline.get("version") != "v0.1.0" or baseline.get("commit") != "2334ba7573c1d0b4565189c93f780b35e08ea81d":
        raise ValidationError("contracts baseline must pin StateRipple v0.1.0 and its commit")
    return ids


def _validate_evaluations(root: Path, contract_ids: Set[str]) -> None:
    directory = root / "tests" / "evals"
    activation = _load_json(directory / "activation.json")
    for group in ("positive", "negative"):
        entries = _require_list(activation.get(group), "activation.%s" % group)
        if not entries:
            raise ValidationError("activation.%s must not be empty" % group)
        ids = []
        for entry in entries:
            if not isinstance(entry,dict) or not isinstance(entry.get('id'),str) or not isinstance(entry.get('prompt'),str) or not entry['prompt'].strip() or entry.get('expected_activation') is not (group == 'positive'):
                raise ValidationError('invalid activation entry')
            if group == 'positive' and entry.get('expected_route') not in ('simulate','analyze'):
                raise ValidationError('positive activation needs a route')
            ids.append(entry['id'])
        if len(ids) != len(set(ids)):
            raise ValidationError('duplicate activation id')
    cases_doc = _load_json(directory / "cases.json")
    cases = _require_list(cases_doc.get("cases"), "cases")
    fail_closed = _require_list(cases_doc.get('fail_closed'),'fail_closed')
    fail_ids = []
    for item in fail_closed:
        if not isinstance(item,dict) or not isinstance(item.get('id'),str):
            raise ValidationError('invalid fail-closed declaration')
        fail_ids.append(item['id'])
        if not _local_path(root / 'tests/fixtures',item.get('fixture_id')).is_file():
            raise ValidationError('fail-closed fixture missing')
        if not set(_require_list(item.get('required_contracts'),'fail_closed required_contracts')).issubset(contract_ids):
            raise ValidationError('fail-closed case declares unknown contract')
    if set(fail_ids) != {'F%02d' % n for n in range(1,11)} or len(fail_ids) != 10:
        raise ValidationError('declare exactly F01-F10 once each')
    case_ids: Set[str] = set()
    for value in cases:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ValidationError("every evaluation case requires a string id")
        case_id = value["id"]
        if case_id in case_ids:
            raise ValidationError("duplicate evaluation case: %s" % case_id)
        case_ids.add(case_id)
        fixture = value.get('fixture_id')
        if fixture is not None and not _local_path(root / 'tests/fixtures',fixture).is_file():
            raise ValidationError('live fixture missing')
        if not isinstance(value.get('prompt'),str) or not value['prompt'].strip():
            raise ValidationError('live case requires prompt')
        followup = value.get('followup')
        if followup is not None:
            if not isinstance(followup,dict) or not isinstance(followup.get('prompt'),str) or not followup['prompt'].strip():
                raise ValidationError('%s follow-up requires a prompt' % case_id)
            if fixture is None or not _local_path(root / 'tests/fixtures',followup.get('fixture_id')).is_file():
                raise ValidationError('%s follow-up fixture missing' % case_id)
            if _local_path(root / 'tests/fixtures',followup['fixture_id']).read_bytes() == _local_path(root / 'tests/fixtures',fixture).read_bytes():
                raise ValidationError('%s follow-up fixture must differ from the first snapshot' % case_id)
        expected = value.get("expected")
        if not isinstance(expected, dict):
            raise ValidationError("%s.expected must be an object" % case_id)
        required = _require_list(expected.get("required_contracts"), "%s.required_contracts" % case_id)
        unknown = set(required) - contract_ids
        if unknown:
            raise ValidationError("%s has unknown contracts: %s" % (case_id, sorted(unknown)))
    suites = _load_json(directory / "live_suites.json").get("suites")
    profiles = _load_json(directory / "profiles.json").get("profiles")
    if not isinstance(suites, dict) or "release" not in suites:
        raise ValidationError("live_suites requires a release suite")
    semantics = {value['id']: value for value in cases}
    if 'L13' in semantics and (not isinstance(semantics['L13'].get('followup'), dict)
                               or 'COMP-004' not in semantics['L13']['expected']['required_contracts']):
        raise ValidationError('L13 must stay a follow-up case that requires COMP-004')
    if 'L14' in semantics and (semantics['L14']['expected'].get('git_guard') is not True
                               or not {'SAFE-005', 'SAFE-006'}.issubset(semantics['L14']['expected']['required_contracts'])):
        raise ValidationError('L14 must stay a Git-guard case that requires SAFE-005 and SAFE-006')
    gate = _load_json(directory / "live_suites.json").get("release_gate")
    if (not isinstance(gate, dict) or gate.get("suites") != ["release", "activation"]
            or sorted(gate.get("agents", [])) != ["claude", "codex"]
            or gate.get("approval") != {"at_least_one": ["ai-release-review", "human-release-review"],
                                        "all_supplied_must_approve": True} or "human_review" in gate):
        raise ValidationError("release_gate must require both suites and agents, and AI or Human approval with no fallback")
    release_cases = _require_list(suites['release'].get('cases') if isinstance(suites['release'], dict) else None, 'release suite cases')
    # Counts come from the catalogs; only the floor of required cases is fixed.
    if not REQUIRED_LIVE_CASES.issubset(case_ids):
        raise ValidationError('required live cases are missing: %s' % sorted(REQUIRED_LIVE_CASES - case_ids))
    if set(release_cases) != case_ids or len(release_cases) != len(set(release_cases)):
        raise ValidationError('release suite must contain every declared live case exactly once')
    if not isinstance(profiles, dict) or set(profiles) != {"snapshot-simulation", "go-change-impact", "incomplete-analysis-recovery"}:
        raise ValidationError("coverage profiles must define the three required profiles")
    for name, suite in suites.items():
        if not isinstance(suite, dict):
            raise ValidationError("suite %s must be an object" % name)
        unknown = set(_require_list(suite.get("cases"), "suite %s cases" % name)) - case_ids
        if unknown:
            raise ValidationError("suite %s has unknown cases: %s" % (name, sorted(unknown)))
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or not isinstance(profile.get("cases"), dict):
            raise ValidationError("profile %s must contain a cases object" % name)
        unknown = set(profile["cases"]) - case_ids
        values = set(profile["cases"].values())
        if unknown or set(profile['cases']) != case_ids or not values.issubset({"CORE", "CONDITIONAL", "NOT_APPLICABLE"}):
            raise ValidationError("profile %s contains invalid cases or classifications" % name)
    declared: Dict[str, Set[str]] = {}
    for case in cases:
        for contract_id in case['expected']['required_contracts']:
            declared.setdefault(contract_id, set()).add(case['id'])
    for contract in _load_json(directory / 'contracts.json')['contracts']:
        if not set(contract['live']).issubset(case_ids):
            raise ValidationError('contract references unknown live case')
        if set(contract['live']) != declared.get(contract['id'], set()) or len(contract['live']) != len(set(contract['live'])):
            raise ValidationError('%s live mapping disagrees with the cases that declare it' % contract['id'])


PUBLISHED_PATHS = ("src", "scripts", "tests", "docs", ".github", "Makefile", "README.md", "README.ja.md", "CHANGELOG.md",
                   "LICENSE", ".gitignore")
_PERSONAL_PATH = re.compile(r"(/Users/|/home/|[A-Za-z]:\\+Users\\+)(?!<|\$|example|runner|USER|user/|you/|name>)[A-Za-z0-9._-]+")


def personal_paths(root: Path) -> List[str]:
    """Absolute home-directory paths in files that would be published."""
    found = []
    for name in PUBLISHED_PATHS:
        base = root / name
        files = [base] if base.is_file() else sorted(path for path in base.rglob("*") if path.is_file()) if base.is_dir() else []
        for path in files:
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo", ".zip") or path.name == ".DS_Store":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if _PERSONAL_PATH.search(line):
                    found.append("%s:%d" % (path.relative_to(root).as_posix(), number))
    return found


def validate(root: Path) -> None:
    leaked = personal_paths(root)
    if leaked:
        raise ValidationError("personal absolute paths in publishable files: %s" % ", ".join(leaked[:10]))
    _validate_skill(root)
    contract_ids = _validate_contracts(root)
    _validate_evaluations(root, contract_ids)
    # The evaluation scripts' own catalog rules (required cases, release gate, activation baseline) are static rules too.
    if __package__:
        from .eval_common import EvaluationError, read_catalogs
    else:
        from eval_common import EvaluationError, read_catalogs
    try:
        read_catalogs(root / 'tests' / 'evals')
    except EvaluationError as error:
        raise ValidationError(str(error)) from error
    upstream = _load_json(root / 'tests/fixtures/upstream/manifest.json')
    if upstream.get('source_commit') != '2334ba7573c1d0b4565189c93f780b35e08ea81d':
        raise ValidationError('upstream fixture provenance is not pinned')
    for entry in upstream['files']:
        data = _local_path(root / 'tests/fixtures/upstream',entry['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry['sha256'] or hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != entry['git_blob']:
            raise ValidationError('upstream fixture bytes differ from recorded provenance')
    captures = _load_json(root / 'tests/fixtures/recorded/manifest.json')
    if __package__:
        from .contracts import parse_report
    else:
        from contracts import parse_report
    for entry in captures['records']:
        path = _local_path(root / 'tests/fixtures',entry['report'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['report_sha256']:
            raise ValidationError('recorded CLI output changed')
        if entry['input_raw_sha256'] is not None:
            data = _local_path(root / 'tests/fixtures',entry['fixture']).read_bytes()
            if hashlib.sha256(data).hexdigest() != entry['input_raw_sha256']:
                raise ValidationError('recorded CLI evidence is stale for fixture bytes')
        if parse_report(path.read_text(encoding='utf-8'),entry['exit_code'],entry['mode']).status not in ('COMPLETE','PARTIAL'):
            raise ValidationError('recorded CLI result violates pinned report contract')
    if (root / 'dist').exists():
        if __package__:
            from .build import BuildError, verify_distribution
        else:
            from build import BuildError, verify_distribution
        try:
            verify_distribution(root)
        except BuildError as error:
            raise ValidationError(str(error)) from error


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        validate(root)
    except (OSError, ValidationError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "PASS", "contracts": len(CONTRACT_IDS)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
