from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import build, eval_common as common, eval_coverage as coverage
from scripts.review_execution import REVIEW_CHECKS, VERIFICATION_COMMANDS
from scripts.validate import CONTRACT_IDS

ROOT = Path(__file__).resolve().parents[1]


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        selected = [ROOT / name for name in (ROOT / 'docs/RELEASE_EVIDENCE.txt').read_text().splitlines() if name]
        if not all(path.is_dir() for path in selected):
            self.skipTest('integration requires the adopted Live and Replay bundles; no live agent is run')
        self.temp = tempfile.TemporaryDirectory(prefix='stateripple-approval-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in coverage.SOURCE_TREE_PATHS:
            source = ROOT / name
            destination = self.root / name
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
            elif source.is_file():
                shutil.copy2(source, destination)
        self.paths = []
        for source in selected:
            destination = self.root / source.relative_to(ROOT)
            shutil.copytree(source, destination)
            self.paths.append(destination)
        build.build(self.root)
        self.catalogs = common.read_catalogs(self.root / 'tests/evals')
        self.records = coverage.load_evidence_records(self.paths)
        self.revision = common.sha256_tree(self.root / 'dist/stateripple')
        self.bundle = self.root / 'release/test-only'
        self.bundle.mkdir(parents=True)
        self.ai_path = self.bundle / 'ai.json'
        self.human_path = self.bundle / 'human.json'

    def report(self, ai=None, human=None):
        return coverage.release_report(self.catalogs, self.records, self.revision, self.root, human, self.paths, ai)

    def write_json(self, path, value, sealed=True):
        path.write_text(json.dumps(common.seal_record(value) if sealed else value), encoding='utf-8')

    def reference(self, path):
        return {'path': path.relative_to(self.bundle).as_posix(), 'sha256': common.sha256_file(path)}

    def write_ai(self):
        target = self.report()['review_target']
        transcript = self.bundle / 'transcript.txt'
        transcript.write_text('Synthetic isolated test review only.\nSource and evidence inspection fixture.\n')
        diff = self.bundle / 'changes.diff'
        diff.write_text('Synthetic isolated test diff, not a real candidate audit.\n')
        provenance = common.load_json(self.root / 'docs/release-implementation.json')
        sources = {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in coverage.source_tree_files(self.root)}
        manifests = {path.relative_to(self.root).as_posix(): common.sha256_file(path)
                     for directory in self.paths for path in [directory / 'manifest.json']}
        identity = {'reviewer_id': 'test-reviewer', 'model': 'test/model', 'session_id': 'test-review-session'}
        verification = {}
        for index, command in enumerate(VERIFICATION_COMMANDS):
            log = self.bundle / ('command-%d.log' % index)
            log.write_text('Synthetic isolated command-result fixture for %s. Not a real candidate verification.\n' % command)
            verification[command] = {'exit_code': 0, 'source_tree_sha256': target['source_tree_sha256'], 'log': self.reference(log)}
        pairs = sorted({(case_id, agent) for case_id, agent, _ in self.records})
        self.execution = dict(identity, schema_version=1, record_type='ai-review-execution', review_target=target,
                              read_only=True, started_at='2026-10-16T00:00:00Z', finished_at='2026-10-16T01:00:00Z',
                              source_tree_before=target['source_tree_sha256'], source_tree_after=target['source_tree_sha256'],
                              source_files=sources, manifest_files=manifests, transcript=self.reference(transcript),
                              baseline_source_tree_sha256=provenance['baseline_source_tree_sha256'], diff=self.reference(diff),
                              verification=verification, case_reviews=[
                                  {'case_id': case_id, 'agent': agent, 'status': 'PASS', 'notes': 'Isolated test fixture only.',
                                   'log_lines': [1, 2], 'records': {kind: common.verify_record(self.records[(case_id, agent, kind)])
                                                                  for kind in ('live', 'replay') if (case_id, agent, kind) in self.records}}
                                  for case_id, agent in pairs])
        implementation = provenance['sessions']
        self.ai = dict(identity, schema_version=1, record_type='ai-release-review', reviewer_kind='ai',
                       reviewed_at='2026-10-16T02:00:00Z', method='Synthetic test review, not release authorization.',
                       independence={'implementation_sessions': implementation, 'basis': 'Distinct test sessions.',
                                     'baseline_source_tree_sha256': provenance['baseline_source_tree_sha256'],
                                     'limitations': 'Synthetic metadata proves no actual reviewer independence.'},
                       evidence_review={name: {'status': 'PASS', 'notes': 'Isolated test inspection fixture.', 'log_lines': [1, 2]}
                                        for name in REVIEW_CHECKS}, safety_contracts={key: 'PASS' for key in CONTRACT_IDS},
                       findings=[], decision='APPROVE', rationale='Isolated test-only decision; never a candidate approval.', **target)
        self.save_execution()
        return self.ai_path

    def save_execution(self):
        path = self.bundle / 'execution.json'
        self.write_json(path, self.execution)
        self.ai['execution'] = self.reference(path)
        self.save_ai()

    def save_ai(self):
        self.write_json(self.ai_path, self.ai)

    def write_human(self):
        record = dict(self.report()['review_target'], schema_version=1, record_type='human-release-review', reviewer_kind='human',
                      reviewer='Example Maintainer', reviewed_at='2026-10-16T02:00:00Z', decision='APPROVE',
                      attestation=coverage.HUMAN_ATTESTATION, checklist={key: True for key, _ in coverage.HUMAN_REVIEW_CHECKLIST},
                      notes='Synthetic Human record in an isolated test directory only.')
        self.write_json(self.human_path, record, sealed=False)
        return self.human_path
