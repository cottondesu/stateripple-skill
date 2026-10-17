from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import validate

ROOT = Path(__file__).resolve().parents[1]


class StaticValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        for relative in ('src', 'scripts', 'tests/evals', 'tests/fixtures'):
            shutil.copytree(ROOT / relative, self.root / relative)
        for path in (ROOT / 'tests').glob('test_*.py'):
            shutil.copyfile(path, self.root / 'tests' / path.name)

    def tearDown(self):
        self.temp.cleanup()

    def _edit(self, relative, old, new):
        path = self.root / relative
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding='utf-8')

    def _edit_json(self, relative, change):
        path = self.root / relative
        value = json.loads(path.read_text(encoding='utf-8'))
        change(value)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_pristine_copy_validates(self):
        validate.validate(self.root)

    def test_inverted_normative_statements_fail(self):
        mutations = (
            ('src/stateripple/SKILL.md', 'risk 0 is inactive in this analysis, not safe', 'risk 0 means safe'),
            ('src/stateripple/SKILL.md', 'not tests run by StateRipple', 'the tests StateRipple ran'),
            ('src/stateripple/references/cli-contract.md', '| 2 | Incomplete result |', '| 2 | Successful result |'),
            ('src/stateripple/references/result-interpretation.md',
             '`cells[].causes` records support for the final maximum risk', '`cells[].causes` records the history'),
            ('src/stateripple/references/result-interpretation.md',
             'Convergence does not upgrade adapter completeness', 'Convergence proves completeness'),
            ('src/stateripple/references/result-interpretation.md',
             'An empty array does not mean tests are unnecessary', 'An empty array means tests are unnecessary'),
            ('src/stateripple/references/routing.md', 'do not fall back to `simulate`', 'fall back to `simulate`'),
            ('src/stateripple/SKILL.md', 'retain tied causes', 'pick the first cause'),
            ('src/stateripple/references/cli-contract.md', 'defaults to `HEAD~1`', 'defaults to `main`'),
            ('src/stateripple/references/cli-contract.md', 'normalizes to 16', 'normalizes to 64'),
        )
        for relative, old, new in mutations:
            with self.subTest(old=old):
                original = (self.root / relative).read_text(encoding='utf-8')
                self._edit(relative, old, new)
                with self.assertRaisesRegex(validate.ValidationError, 'lost a normative statement'):
                    validate.validate(self.root)
                (self.root / relative).write_text(original, encoding='utf-8')

    def test_edited_safety_contract_rows_fail(self):
        for old, new in (('| RISK-001 | Risk 0 is not safety |', '| RISK-001 | Risk 0 is safety |'),
                         ('| SAFE-002 | Never overwrite existing output |', '| SAFE-002 | Overwrite existing output |')):
            with self.subTest(old=old):
                relative = 'src/stateripple/references/safety-contract.md'
                original = (self.root / relative).read_text(encoding='utf-8')
                self._edit(relative, old, new)
                with self.assertRaisesRegex(validate.ValidationError, 'normative safety contract rows changed'):
                    validate.validate(self.root)
                (self.root / relative).write_text(original, encoding='utf-8')

    def test_contract_live_mapping_must_match_declaring_cases(self):
        def unmap(catalog):
            next(item for item in catalog['contracts'] if item['id'] == 'TEST-001')['live'] = ['L05']
        self._edit_json('tests/evals/contracts.json', unmap)
        with self.assertRaisesRegex(validate.ValidationError, 'TEST-001 live mapping disagrees'):
            validate.validate(self.root)

    def test_contract_without_any_verification_fails(self):
        def empty(catalog):
            for item in catalog['contracts']:
                item.update(automated=[], live=[], manual=[], unverified=[])
        self._edit_json('tests/evals/contracts.json', empty)
        with self.assertRaisesRegex(validate.ValidationError, 'neither automated nor live'):
            validate.validate(self.root)

    def test_contract_without_live_case_must_admit_it(self):
        def undeclare(catalog):
            for case in catalog['cases']:
                if 'COMP-004' in case['expected']['required_contracts']:
                    case['expected']['required_contracts'].remove('COMP-004')
        def silence(catalog):
            item = next(item for item in catalog['contracts'] if item['id'] == 'COMP-004')
            item['live'] = []
            item['unverified'] = ['Generic caveat.']
        self._edit_json('tests/evals/cases.json', undeclare)
        self._edit_json('tests/evals/contracts.json', silence)
        with self.assertRaisesRegex(validate.ValidationError, 'COMP-004 has no live case'):
            validate.validate(self.root)

    def test_release_suite_and_profiles_follow_the_declared_cases(self):
        # Counts are derived from the catalogs: dropping a case anywhere must fail.
        def drop_from_suite(catalog):
            catalog['suites']['release']['cases'].remove('L13')
        self._edit_json('tests/evals/live_suites.json', drop_from_suite)
        with self.assertRaisesRegex(validate.ValidationError, 'release suite must contain every declared live case'):
            validate.validate(self.root)

    def test_required_live_cases_cannot_be_removed(self):
        def drop_case(catalog):
            catalog['cases'] = [case for case in catalog['cases'] if case['id'] != 'L14']
        self._edit_json('tests/evals/cases.json', drop_case)
        with self.assertRaisesRegex(validate.ValidationError, 'required live cases are missing'):
            validate.validate(self.root)

    def test_safety_case_flags_cannot_be_dropped(self):
        def drop_guard(catalog):
            next(case for case in catalog['cases'] if case['id'] == 'L14')['expected'].pop('git_guard')
        self._edit_json('tests/evals/cases.json', drop_guard)
        with self.assertRaisesRegex(validate.ValidationError, 'L14 must stay a Git-guard case'):
            validate.validate(self.root)

    def test_followup_case_cannot_become_single_turn(self):
        def drop_followup(catalog):
            next(case for case in catalog['cases'] if case['id'] == 'L13').pop('followup')
        self._edit_json('tests/evals/cases.json', drop_followup)
        with self.assertRaisesRegex(validate.ValidationError, 'L13 must stay a follow-up case'):
            validate.validate(self.root)

    def test_release_gate_must_keep_activation_and_human_review(self):
        def drop(catalog):
            catalog['release_gate']['suites'] = ['release']
        self._edit_json('tests/evals/live_suites.json', drop)
        with self.assertRaisesRegex(validate.ValidationError, 'release_gate must require'):
            validate.validate(self.root)

    def test_personal_absolute_paths_are_rejected(self):
        # Audit F-007.
        self.assertEqual(validate.personal_paths(self.root), [])
        (self.root / 'docs').mkdir(exist_ok=True)
        for leak in ('/Users/' + 'someone/workspace/project', '/home/' + 'someone/bin/tool', 'C:' + chr(92) + 'Users' + chr(92) + 'bob' + chr(92) + 'x'):
            (self.root / 'docs/NOTE.md').write_text('Binary at `%s`.\n' % leak, encoding='utf-8')
            self.assertEqual(validate.personal_paths(self.root), ['docs/NOTE.md:1'])
            with self.assertRaisesRegex(validate.ValidationError, 'personal absolute paths'):
                validate.validate(self.root)
        (self.root / 'docs/NOTE.md').write_text('Binary at `<HOME>/go/bin/tool` or $HOME/bin.\n', encoding='utf-8')
        self.assertEqual(validate.personal_paths(self.root), [])

    def test_required_activation_baseline_is_enforced_by_static_validation(self):
        # Audit F-004.
        def drop(catalog):
            catalog['positive'] = [item for item in catalog['positive'] if item['id'] != 'A-P04']
        self._edit_json('tests/evals/activation.json', drop)
        with self.assertRaisesRegex(validate.ValidationError, 'A-P04'):
            validate.validate(self.root)

    def test_followup_snapshot_must_differ_from_the_first(self):
        def same(catalog):
            case = next(case for case in catalog['cases'] if case['id'] == 'L13')
            case['followup']['fixture_id'] = case['fixture_id']
        self._edit_json('tests/evals/cases.json', same)
        with self.assertRaisesRegex(validate.ValidationError, 'follow-up fixture must differ'):
            validate.validate(self.root)


if __name__ == '__main__':
    unittest.main()
