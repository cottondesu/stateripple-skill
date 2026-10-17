from __future__ import annotations

import copy
import json

from release_review_fixture import ReleaseFixture
from scripts import eval_common as common


class AIReleaseReviewTests(ReleaseFixture):
    def setUp(self):
        super().setUp()
        self.write_ai()

    def assert_rejected(self):
        report = self.report(ai=self.ai_path)
        self.assertEqual(report['readiness'], 'NOT READY')
        self.assertEqual(report['ai_review']['status'], 'FAIL')
        return ' '.join(report['ai_review']['reasons'])

    def test_ai_only_approval_needs_no_human_and_all_technical_checks_pass(self):
        report = self.report(ai=self.ai_path)
        self.assertEqual(report['readiness'], 'READY', report['ai_review'])
        self.assertEqual(report['human_review']['status'], 'NOT_REQUIRED')
        self.assertEqual(report['ai_review']['decision'], 'APPROVE')
        self.assertEqual(report['release_approval']['status'], 'PASS')
        self.assertEqual(report['package']['status'], 'PASS')
        self.assertEqual(report['evidence_manifests']['status'], 'PASS')
        self.assertEqual((report['gaps'], report['activation_gaps']), ([], []))

    def test_reject_and_unknown_decisions_fail(self):
        for decision in ('REJECT', 'PASS', '', None, [], {}):
            with self.subTest(decision=decision):
                self.ai['decision'] = decision
                self.save_ai()
                self.assert_rejected()

    def test_every_required_field_is_required(self):
        original = copy.deepcopy(self.ai)
        for field in original:
            with self.subTest(field=field):
                self.ai = copy.deepcopy(original)
                self.ai.pop(field)
                self.save_ai()
                self.assert_rejected()

    def test_stale_targets_empty_rationale_bad_identity_and_times_fail(self):
        original = copy.deepcopy(self.ai)
        mutations = [(key, '0' * 64) for key in ('skill_revision', 'source_tree_sha256', 'evidence_digest')]
        mutations += [('rationale', ' '), ('method', ''), ('reviewer_id', ' '), ('model', ''), ('session_id', None),
                      ('reviewer_kind', 'human'), ('schema_version', True), ('record_type', 'manual-review'),
                      ('reviewed_at', '2026-02-30T00:00:00Z'), ('reviewed_at', '2026-10-16T00:00:00+00:00'),
                      ('reviewed_at', '2999-01-01T00:00:00Z'), ('reviewed_at', 'yesterday')]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.ai = copy.deepcopy(original)
                self.ai[key] = value
                self.save_ai()
                self.assert_rejected()

    def test_unresolved_serious_findings_and_unaccepted_minor_fail(self):
        for severity, status, resolution in (('BLOCKER', 'OPEN', ''), ('MAJOR', 'OPEN', ''),
                                              ('BLOCKER', 'ACCEPTED', 'accepted'), ('MAJOR', 'ACCEPTED', 'accepted'),
                                              ('MINOR', 'ACCEPTED', ''), ('MINOR', 'OPEN', 'not actually accepted')):
            with self.subTest(severity=severity, status=status):
                self.ai['findings'] = [{'id': 'F01', 'severity': severity, 'status': status, 'description': 'Test finding.',
                                       'evidence': 'transcript.txt:1', 'resolution': resolution}]
                self.save_ai()
                self.assert_rejected()

    def test_accepted_minor_and_evidenced_resolved_findings_allow_approval(self):
        self.ai['findings'] = [{'id': 'F01', 'severity': 'MINOR', 'status': 'ACCEPTED', 'description': 'Test limitation.',
                               'evidence': 'transcript.txt:1', 'resolution': 'Accepted for this isolated test with a stated reason.'}]
        self.save_ai()
        self.assertEqual(self.report(ai=self.ai_path)['readiness'], 'READY')

    def test_self_review_and_false_implementation_provenance_fail(self):
        self.ai['session_id'] = self.ai['independence']['implementation_sessions'][0]
        self.execution['session_id'] = self.ai['session_id']
        self.save_execution()
        self.assertIn('own release', self.assert_rejected())
        self.ai['session_id'] = 'test-review-session'
        self.execution['session_id'] = self.ai['session_id']
        self.ai['independence']['implementation_sessions'] = ['invented-implementation-session']
        self.save_execution()
        self.assertIn('provenance', self.assert_rejected())

    def test_missing_provenance_cannot_be_replaced_with_independent_true(self):
        (self.root / 'docs/release-implementation.json').unlink()
        self.write_ai_missing_provenance()

    def write_ai_missing_provenance(self):
        self.ai['independence'] = {'independent': True}
        self.ai.update(self.report()['review_target'])
        self.save_ai()
        self.assert_rejected()

    def test_incomplete_checks_contracts_cases_and_inventories_fail_even_when_resealed(self):
        original_ai, original_execution = copy.deepcopy(self.ai), copy.deepcopy(self.execution)
        changes = [lambda: self.ai['evidence_review'].pop('negative_activation'),
                   lambda: self.ai['safety_contracts'].pop(next(iter(self.ai['safety_contracts']))),
                   lambda: self.execution['case_reviews'].pop(),
                   lambda: self.execution['case_reviews'].append(self.execution['case_reviews'][0]),
                   lambda: self.execution['case_reviews'][0]['records'].update(live='0' * 64),
                   lambda: self.execution['case_reviews'][0].update(log_lines=[1, 99999]),
                   lambda: self.execution['source_files'].pop(next(iter(self.execution['source_files']))),
                   lambda: self.execution['manifest_files'].pop(next(iter(self.execution['manifest_files']))),
                   lambda: self.execution['verification'].pop('make test'),
                   lambda: self.execution['verification']['make test'].update(exit_code=1),
                   lambda: self.execution.pop('diff'),
                   lambda: self.execution.update(baseline_source_tree_sha256='0' * 64),
                   lambda: self.execution.update(read_only=False),
                   lambda: self.execution.update(session_id='another-reviewer-session'),
                   lambda: self.execution.update(finished_at='2026-10-15T00:00:00Z'),
                   lambda: self.execution.update(source_tree_after='0' * 64)]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.ai, self.execution = copy.deepcopy(original_ai), copy.deepcopy(original_execution)
                change()
                self.save_execution()
                self.assert_rejected()

    def test_invalid_json_seals_duplicate_keys_and_wrong_shapes_fail_closed(self):
        for data in ('{bad', '[]', 'null', '{"schema_version":1,"schema_version":1}', json.dumps(self.ai), '\ud800'):
            with self.subTest(data=data[:20]):
                self.ai_path.write_bytes(data.encode('utf-8', errors='surrogatepass'))
                self.assert_rejected()
        self.save_ai()
        value = common.load_json(self.ai_path)
        value['rationale'] = 'Changed without resealing.'
        self.write_json(self.ai_path, value, sealed=False)
        self.assert_rejected()

    def test_missing_tampered_symlink_and_traversal_artifacts_fail(self):
        transcript = self.bundle / 'transcript.txt'
        transcript.write_text('tampered\n')
        self.assert_rejected()
        transcript.unlink()
        transcript.symlink_to(self.bundle / 'command-0.log')
        self.execution['transcript'] = {'path': 'transcript.txt', 'sha256': common.sha256_file(transcript)}
        self.save_execution()
        self.assert_rejected()
        for name in ('../outside', '/tmp/outside', './execution.json', 'nested/../execution.json'):
            self.ai['execution'] = {'path': name, 'sha256': '0' * 64}
            self.save_ai()
            self.assert_rejected()

    def test_review_artifacts_are_outside_source_digest_but_still_hash_checked(self):
        before = self.report()['review_target']['source_tree_sha256']
        (self.bundle / 'unrelated.txt').write_text('Local review artifact.\n')
        self.assertEqual(self.report(ai=self.ai_path)['review_target']['source_tree_sha256'], before)
        self.ai_path.unlink()
        self.assert_rejected()
