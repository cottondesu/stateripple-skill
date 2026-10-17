import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from contracts import ContractError, parse_report, verify_freshness


def load_report(name):
    return json.loads((ROOT / 'tests/fixtures' / name).read_text(encoding='utf-8'))


class ReportContracts(unittest.TestCase):
    def test_official_golden_shapes(self):
        for name in ('cli-empty.golden.json', 'example.report.json', 'generic-supply-chain.report.json'):
            with self.subTest(name=name):
                text = (ROOT / 'tests/fixtures/upstream' / name).read_text(encoding='utf-8')
                self.assertEqual(parse_report(text, 0, 'simulate').status, 'COMPLETE')

    def test_f01_risk_zero_has_no_safety_claim(self):
        report = load_report('recorded/risk-zero.report.json')
        result = parse_report(json.dumps(report), 0, 'simulate')
        self.assertEqual(result.status, 'COMPLETE')
        self.assertEqual(result.report['cells'][0]['risk'], 0)
        self.assertIn('risk 0 does not prove safety', ' '.join(result.limitations))

    def test_f02_limit_is_partial_and_unconfirmed(self):
        report = load_report('recorded/generation-limit.report.json')
        result = parse_report(json.dumps(report), 2, 'simulate')
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIn('Convergence was not confirmed', ' '.join(result.limitations))
        self.assertEqual(parse_report(json.dumps(report), 0, 'simulate').status, 'INVALID')

    def test_f03_synthetic_adapter_incomplete_despite_convergence(self):
        report = load_report('upstream/cli-empty.golden.json')
        report.update(status='incomplete', warnings=['unknown changed path'], unknown_changes=['data.bin'],
                      changes=[{'path':'data.bin','status':'M','classification':'unknown'}],
                      build_context={'go_version':'go1.23','goos':'linux','goarch':'amd64',
                                     'cgo_enabled':'0','module':'example.invalid/test',
                                     'build_tags':[],'tool_tags':[],'release_tags':[]})
        result = parse_report(json.dumps(report), 2, 'analyze')
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIn('No real-world lower-bound guarantee', ' '.join(result.limitations))
        self.assertEqual(parse_report(json.dumps(report), 0, 'analyze').status, 'INVALID')

    def test_f04_exit_one_overrules_complete_output(self):
        text = (ROOT / 'tests/fixtures/upstream/cli-empty.golden.json').read_text()
        outcome = parse_report(text, 1, 'simulate')
        self.assertEqual(outcome.status, 'FAILED')
        self.assertIsNone(outcome.report)

    def test_f05_empty_targets_do_not_waive_tests(self):
        report = load_report('recorded/risk-zero.report.json')
        outcome = parse_report(json.dumps(report), 0, 'simulate')
        self.assertEqual(outcome.report['test_targets'], [])
        self.assertIn('StateRipple did not execute tests', ' '.join(outcome.limitations))

    def test_f06_tied_causes_are_preserved(self):
        report = load_report('recorded/tied-cycle.report.json')
        outcome = parse_report(json.dumps(report), 0, 'simulate')
        self.assertEqual(outcome.status, 'COMPLETE')
        change = outcome.report['waves'][1]['changes'][0]
        self.assertEqual([cause['source'] for cause in change['causes']], ['a','b'])

    def test_f07_cycle_support_is_distinct_from_origin(self):
        report = load_report('recorded/tied-cycle.report.json')
        outcome = parse_report(json.dumps(report), 0, 'simulate')
        self.assertIn('c', [cause['source'] for cause in outcome.report['cells'][2]['causes']])
        self.assertNotIn('c', [cause['source'] for cause in outcome.report['waves'][1]['changes'][0]['causes']])

    def test_f08_raw_input_freshness_is_separate_from_canonical_hash(self):
        original = (ROOT / 'tests/fixtures/risk-zero.json').read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        verify_freshness(digest, original)
        with self.assertRaises(ContractError):
            verify_freshness(digest, original + b' ')

    def test_f09_untrusted_reason_remains_data(self):
        report = load_report('recorded/malicious.report.json')
        outcome = parse_report(json.dumps(report), 0, 'simulate')
        self.assertEqual(outcome.status, 'COMPLETE')
        self.assertEqual(outcome.report['cells'][0]['id'], '$(touch injected)')
        self.assertFalse((ROOT / 'injected').exists())

    def test_f10_missing_process_cannot_produce_success(self):
        report = load_report('upstream/cli-empty.golden.json')
        self.assertEqual(parse_report(json.dumps(report), 127, 'simulate').status, 'INVALID')

    def test_unknown_schema_types_and_status_fail_closed(self):
        original = load_report('upstream/example.report.json')
        mutations = [('schema_version',True),('schema_version',2),('generation_count',1.0),
                     ('cells',None),('ruleset','unknown'),('status','incomplete'),
                     ('input_sha256','not-a-hash'),('stop_reason','generation_limit')]
        for key, value in mutations:
            with self.subTest(key=key,value=value):
                report = copy.deepcopy(original)
                report[key] = value
                self.assertEqual(parse_report(json.dumps(report),0,'simulate').status,'INVALID')

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        for text in ('{"schema_version":1,"schema_version":1}', '{"cells":NaN}', '[]', 'null', '{'):
            with self.subTest(text=text):
                self.assertEqual(parse_report(text,0,'simulate').status,'INVALID')

    def test_risk_is_an_ordinal_priority_not_a_probability(self):
        # RISK-002: a report is only accepted with whole risk levels 0..3, and its limitation says so.
        original = load_report('upstream/example.report.json')
        accepted = parse_report(json.dumps(original), 0, 'simulate')
        self.assertEqual(accepted.status, 'COMPLETE')
        self.assertIn('Risk is a priority, not a probability', ' '.join(accepted.limitations))
        self.assertTrue(all(type(cell['risk']) is int and 0 <= cell['risk'] <= 3 for cell in accepted.report['cells']))
        for probability in (0.75, 75, '75%', '0.75', 4, -1, 2.0, True, None):
            for field in ('cells', 'test_targets'):
                report = copy.deepcopy(original)
                report[field][0]['risk'] = probability
                with self.subTest(field=field, value=probability):
                    self.assertEqual(parse_report(json.dumps(report), 0, 'simulate').status, 'INVALID')
        report = copy.deepcopy(original)
        report['waves'][0]['changes'][0]['after'] = 0.9
        self.assertEqual(parse_report(json.dumps(report), 0, 'simulate').status, 'INVALID')
        # The skill text carries the same statement, and static validation refuses to lose it.
        skill = (ROOT / 'src/stateripple/SKILL.md').read_text(encoding='utf-8')
        interpretation = (ROOT / 'src/stateripple/references/result-interpretation.md').read_text(encoding='utf-8')
        self.assertIn('Treat risk as priority, not probability.', skill)
        self.assertIn('never a probability', interpretation)
        self.assertNotRegex(interpretation + skill, r'\d\s?% (chance|probability|likelihood)')

    def test_non_integer_exit_codes_are_invalid(self):
        text = (ROOT / 'tests/fixtures/upstream/example.report.json').read_text(encoding='utf-8')
        for exit_code in (False, True, 0.0, 2.0, '0', None):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(parse_report(text, exit_code, 'simulate').status, 'INVALID')

    def test_projection_and_wave_integrity(self):
        original = load_report('upstream/example.report.json')
        mutations = []
        report = copy.deepcopy(original)
        report['test_targets'] = []
        mutations.append(report)
        report = copy.deepcopy(original)
        report['waves'][1]['changes'][0]['after'] = 1
        mutations.append(report)
        report = copy.deepcopy(original)
        report['cells'][0]['risk'] = False
        mutations.append(report)
        report = copy.deepcopy(original)
        report['cells'][1]['causes'][0]['edge']['to'] = 'another'
        mutations.append(report)
        for report in mutations:
            self.assertEqual(parse_report(json.dumps(report),0,'simulate').status,'INVALID')


if __name__ == '__main__':
    unittest.main()
