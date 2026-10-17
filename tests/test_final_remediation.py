from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_round3
from scripts import eval_common as common
from scripts import eval_coverage as coverage
from scripts import eval_replay as replay
from scripts import release_check


class ActivationContractTests(test_round3._Synthetic):
    def test_derived_content_access_is_not_a_load_but_fails_negative_evidence(self):
        # Given
        commands = ('shasum -a 256 $(find . -type f)', 'sha256sum $FILES', 'md5 *',
                    'wc -c $(find . -type f)', 'base64 $FILE', 'tar -cf bundle.tar $FILES',
                    'openssl dgst -sha256 $FILE', 'cksum $FILE', 'custom-reader $FILE',
                    'file .agents/skills/stateripple/SKILL.md', 'file $FILE', 'tar -cf /dev/null .')
        for agent, command in ((agent, command) for agent in ('codex', 'claude') for command in commands):
            with self.subTest(agent=agent, command=command):
                # When
                events = self.codex([("/bin/zsh -lc '%s'" % command, 'derived metadata only', 0)]) if agent == 'codex' else self.claude([
                    ('assistant', [{'type': 'tool_use', 'id': 'read', 'name': 'Bash', 'input': {'command': command}}]),
                    ('user', [{'type': 'tool_result', 'tool_use_id': 'read', 'content': 'derived metadata only'}])])
                access = common.skill_access(events)
                # Then
                self.assertFalse(access['loaded'])
                self.assertTrue(access['touched'])
                self.assertEqual(self.negative(events, agent), 'FAIL')

    def test_listing_skill_paths_without_content_remains_accepted(self):
        # Given
        commands = ('find . -type f', 'ls -la .agents/skills/stateripple', 'stat .agents/skills/stateripple/SKILL.md')
        for command in commands:
            with self.subTest(command=command):
                # When
                events = self.codex([("/bin/zsh -lc '%s'" % command, '.agents/skills/stateripple/SKILL.md', 0)])
                # Then
                self.assertFalse(common.skill_access(events)['loaded'])
                self.assertEqual(self.negative(events), 'PASS')


class ReplayReferenceTests(test_round3._Synthetic):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.h._synthetic_record()
        self.live = test_round3._write_run(self.root, 'live-run', 'live', {'L01.json': self.record})
        self.replayed = replay.replay_record(self.record, self.live / 'L01.json', self.cases['L01'], {})
        self.run = test_round3._write_run(self.root, 'replay-run', 'replay', {'L01-codex.json': self.replayed})

    def test_valid_replay_resolves_its_source(self):
        # Given
        paths = [self.live, self.run]
        # When
        status = coverage.evidence_manifest_status(paths, self.record['skill_revision'])
        # Then
        self.assertEqual(status['problems'], [])

    def test_resealed_manifest_rejects_missing_or_other_source_run(self):
        # Given
        other = common.seal_record(dict(self.record, run_id='f' * 32))
        test_round3._write_run(self.root, 'other-run', 'live', {'L01.json': other})
        for source in ('missing-run', 'other-run', 'replay-run'):
            with self.subTest(source=source):
                test_round3._reseal_manifest(self.run, lambda value: value.update(source=source))
                # When
                problems = common.manifest_problems(self.run, self.record['skill_revision'])[0]
                # Then
                self.assertTrue(problems)

    def test_resealed_replay_rejects_false_source_file_or_hash(self):
        # Given
        changes = ({'file': 'missing.json'}, {'file': '../L01.json'}, {'file_sha256': '0' * 64},
                   {'file_sha256': None}, {'record_sha256': '0' * 64}, {'evaluation_type': 'replay'})
        for change in changes:
            with self.subTest(change=change):
                changed = copy.deepcopy(self.replayed)
                changed['source'].update(change)
                (self.run / 'L01-codex.json').write_bytes(common.canonical_json(common.seal_record(changed)))
                test_round3._reseal_manifest(self.run, lambda value: value['records'][0].update(
                    sha256=common.sha256_file(self.run / 'L01-codex.json')))
                # When
                status = coverage.evidence_manifest_status([self.live, self.run], self.record['skill_revision'])
                # Then
                self.assertEqual(status['status'], 'FAIL')

    def test_explicit_sources_support_external_replay_directories(self):
        # Given
        with tempfile.TemporaryDirectory() as output:
            # When
            with patch('builtins.print'):
                result = replay.run(['--evidence', str(self.live / 'L01.json'), '--output-dir', output])
            run = next(Path(output).iterdir())
            status = coverage.evidence_manifest_status([self.live, run], self.record['skill_revision'])
            # Then
            self.assertEqual(result, 2)
            self.assertEqual(common.load_json(run / 'manifest.json')['source'], self.live.name)
            self.assertEqual(status['problems'], [])

    def test_resealed_replay_revision_must_match_source_without_current_revision(self):
        # Given
        changed = common.seal_record(dict(self.replayed, skill_revision='0' * 64))
        (self.run / 'L01-codex.json').write_bytes(common.canonical_json(changed))
        test_round3._reseal_manifest(self.run, lambda value: value['records'][0].update(
            sha256=common.sha256_file(self.run / 'L01-codex.json')))
        # When
        problems = common.manifest_problems(self.run, None)[0]
        # Then
        self.assertTrue(any('revision differs' in problem for problem in problems))


class BuildResultTests(unittest.TestCase):
    def test_invalid_exit_zero_build_results_fail_closed(self):
        # Given
        valid = {'status': 'PASS', 'archive': 'a' * 64, 'manifest': 'b' * 64}
        invalid = [{}, [], dict(valid, status='FAIL'), dict(valid, archive='a'), dict(valid, archive=True),
                   dict(valid, archive='A' * 64), dict(valid, manifest=None), {'status': 'PASS', 'archive': 'a' * 64}]
        for value in invalid:
            with self.subTest(value=value):
                previous = []
                process = subprocess.CompletedProcess([], 0, json.dumps(value), '')
                # When
                with patch.object(release_check, '_execute', return_value=process):
                    results = [release_check._build(Path('.'), name, 'python3', previous)
                               for name in ('build', 'reproducible-build')]
                # Then
                self.assertEqual([result.status for result in results], ['FAIL', 'FAIL'])
                self.assertEqual(previous, [])

    def test_valid_build_hashes_must_match(self):
        # Given
        for second_hash, expected in (('a', 'PASS'), ('c', 'FAIL')):
            with self.subTest(second_hash=second_hash):
                previous = []
                processes = [subprocess.CompletedProcess([], 0, json.dumps(
                    {'status': 'PASS', 'archive': value * 64, 'manifest': 'b' * 64}), '') for value in ('a', second_hash)]
                # When
                with patch.object(release_check, '_execute', side_effect=processes):
                    first = release_check._build(Path('.'), 'build', 'python3', previous)
                    second = release_check._build(Path('.'), 'reproducible-build', 'python3', previous)
                # Then
                self.assertEqual(first.status, 'PASS')
                self.assertEqual(second.status, expected)
