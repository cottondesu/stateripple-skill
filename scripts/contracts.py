"""Development-only report contract checks; never compute propagated risk."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

JSONValue = Union[None, bool, int, float, str, List['JSONValue'], Dict[str, 'JSONValue']]


class ContractError(ValueError):
    """Untrusted evidence violates the pinned report contract."""


@dataclass(frozen=True)
class ReportOutcome:
    status: str
    report: Optional[Dict[str, JSONValue]]
    limitations: Tuple[str, ...]


def _pairs(pairs: List[Tuple[str, JSONValue]]) -> Dict[str, JSONValue]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ContractError('non-JSON numeric literal: ' + value)


def _object(value: JSONValue, keys: Tuple[str, ...], optional: Tuple[str, ...] = ()) -> Dict[str, JSONValue]:
    if not isinstance(value, dict) or set(value) - set(keys) - set(optional) or set(keys) - set(value):
        raise ContractError('object fields do not match schema')
    return value


def _array(value: JSONValue) -> List[JSONValue]:
    if not isinstance(value, list):
        raise ContractError('expected array, never null')
    return value


def _string(value: JSONValue, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ContractError('expected string')
    try:
        value.encode('utf-8', errors='strict')
    except UnicodeEncodeError as error:
        raise ContractError('invalid Unicode') from error
    return value


def _integer(value: JSONValue, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError('expected bounded integer')
    return value


def _causes(value: JSONValue, target: str, ids: set) -> None:
    for item in _array(value):
        cause = _object(item, ('source', 'reason'), ('edge',))
        source = _string(cause['source'])
        _string(cause['reason'])
        if source not in ids:
            raise ContractError('unknown cause source')
        if 'edge' in cause:
            edge = _object(cause['edge'], ('from', 'to', 'kind', 'attenuation'))
            if edge['from'] != source or edge['to'] != target:
                raise ContractError('cause edge endpoints inconsistent')
            _string(edge['kind'])
            _integer(edge['attenuation'], 0, 3)
        elif source != target:
            raise ContractError('seed cause source must equal target')


def _validate(report: Dict[str, JSONValue], exit_code: int, mode: str) -> None:
    _object(report, ('schema_version', 'ruleset', 'status', 'input_sha256',
                     'generation_count', 'cells', 'test_targets', 'waves',
                     'unknown_changes', 'warnings', 'changes', 'build_context', 'stop_reason'))
    if type(report['schema_version']) is not int or report['schema_version'] != 1:
        raise ContractError('unknown report schema')
    if report['ruleset'] != 'risk-propagation-v1':
        raise ContractError('unknown ruleset')
    if mode not in ('simulate', 'analyze'):
        raise ContractError('unknown invocation mode')
    if report['status'] != {0: 'complete', 2: 'incomplete'}[exit_code]:
        raise ContractError('exit/status mismatch')
    if report['stop_reason'] not in ('converged', 'generation_limit'):
        raise ContractError('unknown stopping reason')
    if report['status'] == 'complete' and report['stop_reason'] != 'converged':
        raise ContractError('complete requires convergence')
    if not re.fullmatch('[0-9a-f]{64}', _string(report['input_sha256'])):
        raise ContractError('invalid canonical input hash')
    count = _integer(report['generation_count'], 1, 1024)
    for key in ('unknown_changes', 'warnings'):
        for value in _array(report[key]):
            _string(value)
    if report['status'] == 'complete' and (report['unknown_changes'] or report['warnings']):
        raise ContractError('complete contains unresolved evidence warnings')
    for value in _array(report['changes']):
        change = _object(value, ('path', 'status', 'classification'))
        for entry in change.values():
            _string(entry)
    context = report['build_context']
    if mode == 'simulate':
        if context is not None or report['changes'] or report['unknown_changes']:
            raise ContractError('simulate contains adapter metadata')
        if report['status'] == 'incomplete' and report['stop_reason'] != 'generation_limit':
            raise ContractError('simulate incomplete must hit generation limit')
    else:
        context = _object(context, ('go_version', 'goos', 'goarch', 'cgo_enabled',
                                    'module', 'build_tags', 'tool_tags', 'release_tags'))
        for key in ('go_version', 'goos', 'goarch', 'cgo_enabled', 'module'):
            _string(context[key])
        for key in ('build_tags', 'tool_tags', 'release_tags'):
            for tag in _array(context[key]):
                _string(tag)
        if report['stop_reason'] != 'converged':
            raise ContractError('pinned Go profile always converges within 16 updates')
        if report['status'] == 'incomplete' and not (report['warnings'] or report['unknown_changes']):
            raise ContractError('adapter incompleteness lacks diagnostics')
    cells = []
    ids = set()
    by_id = {}
    for value in _array(report['cells']):
        cell = _object(value, ('id', 'kind', 'risk', 'seeds', 'first_active_generation', 'causes'))
        node_id = _string(cell['id'])
        if node_id in ids:
            raise ContractError('duplicate report node')
        ids.add(node_id)
        by_id[node_id] = cell
        _string(cell['kind'])
        risk = _integer(cell['risk'], 0, 3)
        first = cell['first_active_generation']
        if risk == 0 and first is not None:
            raise ContractError('dormant cell has activation generation')
        if risk > 0:
            _integer(first, 0, count)
        for seed_value in _array(cell['seeds']):
            seed = _object(seed_value, ('id', 'risk', 'reason'))
            if seed['id'] != node_id:
                raise ContractError('seed belongs to another cell')
            _integer(seed['risk'], 0, 3)
            _string(seed['reason'])
        cells.append(cell)
    for cell in cells:
        _causes(cell['causes'], cell['id'], ids)
    targets = []
    for value in _array(report['test_targets']):
        target = _object(value, ('id', 'risk'))
        targets.append((_string(target['id']), _integer(target['risk'], 1, 3)))
    expected = sorted([(cell['id'], cell['risk']) for cell in cells
                       if cell['kind'] == 'test' and cell['risk'] > 0], key=lambda pair: (-pair[1], pair[0]))
    if targets != expected:
        raise ContractError('test_targets differs from positive test-node projection')
    last_generation = -1
    history = {node_id: 0 for node_id in ids}
    first_seen = {}
    for value in _array(report['waves']):
        wave = _object(value, ('generation', 'changes'))
        generation = _integer(wave['generation'], 0, count)
        if generation <= last_generation or (report['stop_reason'] == 'converged' and generation >= count):
            raise ContractError('wave generation invalid')
        last_generation = generation
        wave_ids = set()
        for value in _array(wave['changes']):
            change = _object(value, ('id', 'before', 'after', 'causes'))
            node_id = _string(change['id'])
            before = _integer(change['before'], 0, 3)
            after = _integer(change['after'], 1, 3)
            if node_id not in ids or node_id in wave_ids or before != history[node_id] or after <= before:
                raise ContractError('wave history inconsistent')
            wave_ids.add(node_id)
            _causes(change['causes'], node_id, ids)
            if not change['causes']:
                raise ContractError('historical increase has no cause')
            history[node_id] = after
            first_seen.setdefault(node_id, generation)
    for node_id, cell in by_id.items():
        if history[node_id] != cell['risk'] or first_seen.get(node_id) != cell['first_active_generation']:
            raise ContractError('final cell and historical waves disagree')


def parse_report(stdout: str, exit_code: int, mode: str) -> ReportOutcome:
    """Combine actual process status with strict pinned JSON structure."""
    if type(exit_code) is not int:
        return ReportOutcome('INVALID', None, ('Process exit code is not an integer.',))
    if exit_code == 1:
        return ReportOutcome('FAILED', None, ('Execution/output failed; any remaining output is not success evidence.',))
    if exit_code not in (0, 2):
        return ReportOutcome('INVALID', None, ('Unexpected process exit code.',))
    try:
        data = json.loads(stdout, object_pairs_hook=_pairs, parse_constant=_constant)
        report = _object(data, ('schema_version', 'ruleset', 'status', 'input_sha256',
                                'generation_count', 'cells', 'test_targets', 'waves',
                                'unknown_changes', 'warnings', 'changes', 'build_context', 'stop_reason'))
        _validate(report, exit_code, mode)
    except (ContractError, json.JSONDecodeError, RecursionError) as error:
        return ReportOutcome('INVALID', None, (str(error),))
    limitations = [
        'Risk is a priority, not a probability; risk 0 does not prove safety.',
        'test_targets are candidates; StateRipple did not execute tests.',
        'Final causes are support; waves describe historical changes and tied causes must be retained.',
        'Complete computation does not prove all real-world dependencies were supplied.',
    ]
    if exit_code == 2:
        if mode == 'simulate':
            limitations.append('Convergence was not confirmed; current risks are lower bounds for this supplied graph.')
        else:
            limitations.append('Adapter evidence is incomplete despite convergence; unknown changes were not seeded. No real-world lower-bound guarantee.')
    return ReportOutcome('COMPLETE' if exit_code == 0 else 'PARTIAL', report, tuple(limitations))


def verify_freshness(recorded_input_sha256: str, current_input_bytes: bytes) -> None:
    """Check acquisition freshness using raw bytes, separately from canonical CLI hash."""
    if hashlib.sha256(current_input_bytes).hexdigest() != recorded_input_sha256:
        raise ContractError('stale input evidence; run again for the changed input')
