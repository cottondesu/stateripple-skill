from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.ai_release_review import RECORD_FIELDS, ai_review_status
from scripts.review_execution import REVIEW_CHECKS, VERIFICATION_COMMANDS, ReviewContext
from scripts.validate import CONTRACT_IDS

ROOT = Path(__file__).resolve().parents[1]


class ReleaseReviewTemplateTests(unittest.TestCase):
    def test_ai_template_matches_the_dedicated_schema_and_never_approves(self):
        path = ROOT / 'docs/ai-release-review.template.json'
        value = json.loads(path.read_text())
        self.assertEqual(set(value), RECORD_FIELDS)
        self.assertEqual(set(value['evidence_review']), set(REVIEW_CHECKS))
        self.assertEqual(set(value['safety_contracts']), CONTRACT_IDS)
        self.assertEqual(value['record_type'], 'ai-release-review')
        self.assertEqual(value['reviewer_kind'], 'ai')
        self.assertIsNone(value['decision'])
        self.assertNotIn('attestation', value)
        self.assertNotIn('checklist', value)
        target = {key: '0' * 64 for key in ('skill_revision', 'source_tree_sha256', 'evidence_digest')}
        context = ReviewContext(ROOT, target, {}, {}, {}, sorted(CONTRACT_IDS))
        self.assertEqual(ai_review_status(path, context)['status'], 'FAIL')

    def test_execution_template_needs_real_evidence_before_use(self):
        value = json.loads((ROOT / 'docs/ai-review-execution.template.json').read_text())
        self.assertEqual(set(value['verification']), set(VERIFICATION_COMMANDS))
        self.assertEqual(value['record_type'], 'ai-review-execution')
        self.assertFalse(value['read_only'])
        self.assertEqual(value['case_reviews'], [])
        self.assertTrue(all(item['exit_code'] is None for item in value['verification'].values()))
