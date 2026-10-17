from __future__ import annotations

import contextlib
import copy
import io
import json

from release_review_fixture import ReleaseFixture
from scripts import eval_common as common, eval_coverage as coverage


class ReleaseApprovalGateTests(ReleaseFixture):
    def test_no_approval_cannot_be_replaced_by_grader_and_replay_reviews(self):
        report = self.report()
        self.assertEqual((report['gaps'], report['activation_gaps']), ([], []))
        self.assertEqual(report['package']['status'], 'PASS')
        self.assertEqual(report['evidence_manifests']['status'], 'PASS')
        self.assertEqual(report['readiness'], 'NOT READY')
        self.assertEqual(report['release_approval']['status'], 'NOT_RUN')

    def test_human_only_approval_preserves_the_existing_route(self):
        self.write_human()
        report = self.report(human=self.human_path)
        self.assertEqual(report['readiness'], 'READY', report['blocking'])
        self.assertEqual(report['human_review']['status'], 'PASS')
        self.assertEqual(report['ai_review']['status'], 'NOT_RUN')

    def test_both_approvals_succeed_only_if_both_are_valid(self):
        self.write_ai()
        self.write_human()
        report = self.report(ai=self.ai_path, human=self.human_path)
        self.assertEqual(report['readiness'], 'READY', report['blocking'])
        self.ai['decision'] = 'REJECT'
        self.save_ai()
        self.assertEqual(self.report(ai=self.ai_path, human=self.human_path)['readiness'], 'NOT READY')

    def test_human_rejection_and_invalid_supplied_records_never_fall_back_to_ai(self):
        self.write_ai()
        self.write_human()
        original = self.human_path.read_text()
        for change in ({'decision': 'REJECT'}, {'source_tree_sha256': '0' * 64}, {'reviewer_kind': 'ai'},
                       {'reviewer': 'Codex independent AI'}, {'notes': ''}):
            with self.subTest(change=change):
                human = json.loads(original)
                human.update(change)
                self.write_json(self.human_path, human, sealed=False)
                report = self.report(ai=self.ai_path, human=self.human_path)
                self.assertEqual(report['ai_review']['status'], 'PASS')
                self.assertEqual(report['readiness'], 'NOT READY')
        self.human_path.unlink()
        self.assertEqual(self.report(ai=self.ai_path, human=self.human_path)['readiness'], 'NOT READY')

    def test_invalid_supplied_ai_never_falls_back_to_human(self):
        self.write_ai()
        self.write_human()
        self.ai_path.write_text('{bad')
        report = self.report(ai=self.ai_path, human=self.human_path)
        self.assertEqual(report['human_review']['status'], 'PASS')
        self.assertEqual(report['readiness'], 'NOT READY')
        self.ai_path.unlink()
        self.assertEqual(self.report(ai=self.ai_path, human=self.human_path)['readiness'], 'NOT READY')

    def test_ai_record_cannot_impersonate_a_human_record(self):
        self.write_ai()
        report = self.report(human=self.ai_path)
        self.assertEqual(report['human_review']['status'], 'FAIL')
        self.assertEqual(report['readiness'], 'NOT READY')

    def test_package_failure_cannot_be_overridden_by_valid_ai_approval(self):
        self.write_ai()
        (self.root / 'dist' / 'stateripple-skill-v0.1.0.zip').write_bytes(b'invalid archive')
        report = self.report(ai=self.ai_path)
        self.assertEqual(report['ai_review']['status'], 'PASS')
        self.assertEqual(report['package']['status'], 'FAIL')
        self.assertEqual(report['readiness'], 'NOT READY')

    def test_live_and_positive_negative_activation_gaps_block_approval(self):
        original = dict(self.records)
        for case_id in ('L01', 'L13', 'L14', 'A-P01', 'A-N01'):
            with self.subTest(case_id=case_id):
                self.records = {key: value for key, value in original.items() if key[:2] != (case_id, 'codex')}
                self.write_ai()
                report = self.report(ai=self.ai_path)
                self.assertEqual(report['readiness'], 'NOT READY')
                gaps = report['activation_gaps'] if case_id.startswith('A-') else report['gaps']
                self.assertTrue(any(gap['case_id'] == case_id for gap in gaps))

    def test_manifest_corruption_blocks_even_when_review_inventory_is_rehashed(self):
        manifest_path = self.paths[0] / 'manifest.json'
        manifest = common.load_json(manifest_path)
        manifest['records'][0]['sha256'] = '0' * 64
        self.write_json(manifest_path, manifest)
        self.write_ai()
        report = self.report(ai=self.ai_path)
        self.assertEqual(report['ai_review']['status'], 'PASS')
        self.assertEqual(report['evidence_manifests']['status'], 'FAIL')
        self.assertEqual(report['readiness'], 'NOT READY')

    def test_current_grader_rejects_live_failure_despite_a_reasoned_ai_record(self):
        key = ('L01', 'codex', 'live')
        record = dict(self.records[key])
        record['precondition_status'] = 'FAIL'
        self.records[key] = common.seal_record(record)
        replay_key = ('L01', 'codex', 'replay')
        replay = copy.deepcopy(self.records[replay_key])
        replay['source']['record_sha256'] = common.verify_record(self.records[key])
        self.records[replay_key] = common.seal_record(replay)
        self.write_ai()
        report = self.report(ai=self.ai_path)
        self.assertEqual(report['readiness'], 'NOT READY')
        self.assertTrue(any(gap['case_id'] == 'L01' for gap in report['gaps']))

    def test_cli_reads_and_validates_the_actual_files_and_exit_status(self):
        args = ['--release', '--project-root', str(self.root)]
        for path in self.paths:
            args += ['--evidence', str(path)]
        def run(extra):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = coverage.run(args + extra)
            return code, json.loads(output.getvalue())
        self.assertEqual(run([])[0], 1)
        self.write_ai()
        code, report = run(['--ai-review', str(self.ai_path)])
        self.assertEqual((code, report['readiness']), (0, 'READY'), report['ai_review'])
        self.ai_path.write_text('{invalid')
        self.assertEqual(run(['--ai-review', str(self.ai_path)])[0], 1)
        self.write_human()
        self.assertEqual(run(['--human-review', str(self.human_path)])[0], 0)
        self.assertEqual(run(['--ai-review', str(self.ai_path), '--human-review', str(self.human_path)])[0], 1)

    def test_cli_rejects_review_flags_outside_release_mode(self):
        for flag in ('--ai-review', '--human-review'):
            with self.subTest(flag=flag), self.assertRaises(common.EvaluationError):
                coverage.run(['--profile', 'snapshot-simulation', '--project-root', str(self.root), flag, str(self.ai_path)])
