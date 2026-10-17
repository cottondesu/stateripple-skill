"""Regression tests for the second independent audit (F2-01 .. F2-04).

Everything here uses synthetic event streams, dictionaries, replaced process starters and
temporary directories. No agent, no Git with injected settings and no host file is involved.
"""
from __future__ import annotations

import copy
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import test_evaluation
from scripts import eval_common as common
from scripts import eval_coverage as coverage
from scripts import eval_live as live
from scripts import eval_replay as replay
from scripts import gitenv
from scripts import release_check
from scripts import writeguard

ROOT = Path(__file__).resolve().parents[1]
CODEX_BODY = '.agents/skills/stateripple/SKILL.md'
CLAUDE_BODY = '<EVAL_TMP>/workspace/.claude/skills/stateripple/SKILL.md'


class _Synthetic(unittest.TestCase):
    """Helpers shared with the main evaluation tests."""

    def setUp(self):
        self.h = test_evaluation.EvaluationTests('test_synthetic_grader_does_not_replace_manual_or_live')
        self.h.setUp()
        self.files = self.h.SKILL_FILES
        self.body = self.files['SKILL.md']
        self.cases = common.case_map(self.h.catalogs, include_activation=True)

    def codex(self, commands):
        """Events of a Codex run in which each (command, output, exit code) started and completed."""
        return self.h._events(commands)

    def claude(self, blocks):
        events = self.h._claude_events(blocks)
        events.update(final_response='Synthetic prose.', json_parse_errors=0, tool_log_version=common.TOOL_LOG_VERSION)
        return events

    def positive(self, events, agent='codex'):
        """Status of a positive case (L01) whose record carries these events."""
        record = copy.deepcopy(self.h._synthetic_record())
        record['agent'] = {'name': agent, 'version': 'test-double', 'argv': self.h._argv(agent)}
        record['observations'].update(agent_events=events, write_guard=self.h._guard(agent, 'L01'))
        graded = common.grade_evidence(self.cases['L01'], record)
        return graded['status'], [check['id'] for check in graded['checks'] if not check['pass']]

    def negative(self, events, agent='codex'):
        record, _ = self.h._activation_record('A-N01', agent)
        record = copy.deepcopy(record)
        record['observations']['agent_events'] = events
        return common.grade_evidence(self.cases['A-N01'], record)['status']


class ActivationAttributionTests(_Synthetic):
    """F2-01: a load is one request paired with the host's result for that same request."""

    def assert_not_loaded(self, events, label, agent='codex'):
        self.assertFalse(common.skill_access(events)['loaded'], label)
        status, failed = self.positive(events, agent)
        self.assertEqual(status, 'FAIL', label)
        self.assertIn('skill-activation', failed, label)

    def test_legitimate_loads_are_recognised_for_both_agents(self):
        for label, events, agent in (
                ('codex cat', self.codex([("/bin/zsh -lc 'cat %s'" % CODEX_BODY, self.body, 0)]), 'codex'),
                ('codex cat, double quotes', self.codex([('/bin/zsh -lc "cat %s"' % CODEX_BODY, self.body, 0)]), 'codex'),
                ('codex cat, absolute', self.codex([("/bin/zsh -lc 'cat <EVAL_TMP>/workspace/%s'" % CODEX_BODY, self.body, 0)]), 'codex'),
                ('codex cat after unrelated commands', self.codex([("/bin/zsh -lc 'ls'", 'inventory.py', 0),
                                                                    ("/bin/zsh -lc 'cat %s'" % CODEX_BODY, self.body, 0)]), 'codex'),
                ('claude skill', self.claude(self.h._skill_delivery()), 'claude'),
                ('claude skill, private prefix', self.claude(self.h._skill_delivery(
                    base='/private' + self.h.CLAUDE_SKILL_DIRECTORY)), 'claude'),
                ('claude read', self.claude([('assistant', [{'type': 'tool_use', 'id': 'r1', 'name': 'Read', 'input': {'file_path': CLAUDE_BODY}}]),
                                             ('user', [{'type': 'tool_result', 'tool_use_id': 'r1', 'content': '\n'.join(
                                                 '%6d→%s' % (number, line) for number, line in enumerate(self.body.splitlines(), 1))}])]), 'claude'),
                ('claude bash cat', self.claude([('assistant', [{'type': 'tool_use', 'id': 'b1', 'name': 'Bash', 'input': {'command': 'cat ' + CLAUDE_BODY}}]),
                                                 ('user', [{'type': 'tool_result', 'tool_use_id': 'b1', 'content': self.body.rstrip('\n')}])]), 'claude')):
            self.assertTrue(common.skill_access(events)['loaded'], label)
            self.assertEqual(self.positive(events, agent), ('PASS', []), label)

    def test_output_of_one_command_is_never_credited_to_another(self):
        # The audited case: one command prints text like the skill, another names the read but does not run it.
        echo = "/bin/zsh -lc 'echo -e \"%s\"'" % self.body.replace('\n', '\\n').replace('"', '\\"').replace("'", '')
        cases = {
            'echo prints the body, separate unexecuted cat': [(echo, self.body, 0), ("/bin/zsh -lc 'true || cat %s'" % CODEX_BODY, '', 0)],
            'echo and unexecuted cat in one request': [("/bin/zsh -lc 'printf %%s BODY; true || cat %s'" % CODEX_BODY, self.body, 0)],
            'echo and cat in one request': [("/bin/zsh -lc 'echo x; cat %s'" % CODEX_BODY, 'x\n' + self.body, 0)],
            'another command prints the body, then a failed cat': [("/bin/zsh -lc 'python3 show.py'", self.body, 0),
                                                                  ("/bin/zsh -lc 'cat %s'" % CODEX_BODY, '', 1)],
            'another command prints the body, cat prints nothing': [("/bin/zsh -lc './show.sh'", self.body, 0),
                                                                   ("/bin/zsh -lc 'cat %s'" % CODEX_BODY, '', 0)],
            'cat of a copy prints the body, cat of the installed file is unexecuted':
                [("/bin/zsh -lc 'cat /tmp/copy.md'", self.body, 0), ("/bin/zsh -lc 'false && cat %s'" % CODEX_BODY, '', 1)],
        }
        for label, commands in cases.items():
            self.assert_not_loaded(self.codex(commands), label)

    def test_requests_that_do_not_run_or_do_not_read_are_not_loads(self):
        path = CODEX_BODY
        for command, output, exit_code in (
                ('false && cat %s' % path, '', 1), ('true || cat %s' % path, '', 0),
                ('false && cat %s' % path, self.body, 0),          # even if the recorded output were the body
                ('true || cat %s' % path, self.body, 0),
                ('echo %s' % path, path, 0), ('echo cat %s' % path, 'cat ' + path, 0),
                ('ls .agents/skills/stateripple', 'SKILL.md\nreferences', 0), ('ls -la %s' % path, path, 0),
                ('test -f %s' % path, '', 0), ('cat %s > /dev/null' % path, '', 0), ('cat %s | head -3' % path, self.body, 0),
                ('cat %s' % path, '', 0),                          # succeeded, nothing came back: unknown
                ('cat %s' % path, self.body, 1),                   # reported as failed
                ('cat %s' % path, self.body[:len(self.body) // 2], 0),
                ('cat %s' % path, self.body + 'one more line\n', 0),
                ('cat %s' % path, 'intro\n' + self.body, 0),
                ('cat %s' % path, self.body.replace('StateRipple', 'Other'), 0),
                ('cat %s %s' % (path, path), self.body + self.body, 0),
                ('cat %s notes.md' % path, self.body, 0), ('cat -n %s' % path, self.body, 0), ('nl %s' % path, self.body, 0),
                ('sed -n 1,200p %s' % path, self.body, 0), ('head -500 %s' % path, self.body, 0),
                ('cat %s; true' % path, self.body, 0), ('cat %s && true' % path, self.body, 0), ('cat %s 2>&1' % path, self.body, 0),
                ('cat %s # note' % path, self.body, 0), ('cat $(echo %s)' % path, self.body, 0), ('cat `echo %s`' % path, self.body, 0),
                ('X=1 cat %s' % path, self.body, 0), ('env cat %s' % path, self.body, 0), ('command cat %s' % path, self.body, 0),
                ('cat .agents/skills/stateripple/SKILL*', self.body, 0), ('cat .agents/skills/stateripple/skill.md', self.body, 0),
                ('cat ./%s' % path, self.body, 0), ('cat .agents/skills/../skills/stateripple/SKILL.md', self.body, 0),
                ('cat .agents//skills/stateripple/SKILL.md', self.body, 0),
                ('cat ~/.codex/skills/stateripple/SKILL.md', self.body, 0), ('cat <HOME>/.agents/skills/stateripple/SKILL.md', self.body, 0),
                ('cat /tmp/SKILL.md', self.body, 0), ('cat dist/stateripple/SKILL.md', self.body, 0),
                ('cat .claude/skills/stateripple/SKILL.md', self.body, 0),   # the other agent's directory
                ('cd .agents/skills/stateripple && cat SKILL.md', self.body, 0), ('cd /x; cat %s' % path, self.body, 0),
                ('python3 -c "print(open(\'%s\').read())"' % path, self.body, 0), ('./show.sh', self.body, 0)):
            self.assert_not_loaded(self.codex([("/bin/zsh -lc '%s'" % command.replace("'", "'\\''"), output, exit_code)]), command)
        # Wrappers other than the one Codex puts around a command are not read either.
        for request in ('cat %s' % path, "/bin/zsh -lc 'cat %s' extra" % path, "/usr/bin/env zsh -lc 'cat %s'" % path,
                        "/bin/zsh -c -l 'cat %s'" % path, "/bin/zsh -lc 'cat %s'; echo x" % path,
                        "/bin/zsh -lc 'cat '%s" % path, "/tmp/zsh -lc 'cat %s'" % path, "/bin/zsh -lc $'cat %s'" % path):
            self.assert_not_loaded(self.codex([(request, self.body, 0)]), request)

    def test_missing_repeated_or_mismatched_results_are_not_loads(self):
        cat = "/bin/zsh -lc 'cat %s'" % CODEX_BODY
        item = {'id': 'i1', 'type': 'command_execution', 'command': cat, 'aggregated_output': '', 'exit_code': None, 'status': 'in_progress'}
        done = dict(item, aggregated_output=self.body, exit_code=0, status='completed')
        message = {'type': 'item.completed', 'item': {'id': 'm', 'type': 'agent_message', 'text': 'Synthetic prose.'}}

        def events(*lines):
            parsed = common.extract_agent_events('codex', '\n'.join(json.dumps(line) for line in lines + (message,)).encode(), {}, self.files)
            parsed.update(json_parse_errors=0, tool_log_version=common.TOOL_LOG_VERSION)
            return parsed
        started, completed = {'type': 'item.started', 'item': item}, {'type': 'item.completed', 'item': done}
        self.assertTrue(common.skill_access(events(started, completed))['loaded'])
        self.assert_not_loaded(events(started), 'started, no result')
        self.assert_not_loaded(events(completed), 'result with no start before it')
        self.assert_not_loaded(events(completed, started), 'result before the start')
        self.assert_not_loaded(events(started, completed, completed), 'two results for one call')
        self.assert_not_loaded(events(started, {'type': 'item.completed', 'item': dict(done, id='other')}), 'result under another id')
        self.assert_not_loaded(events(started, started, completed), 'two calls under one id')
        self.assert_not_loaded(events(started, {'type': 'item.completed', 'item': dict(done, type='mcp_tool_call')}), 'result of another kind')
        self.assert_not_loaded(events(started, {'type': 'item.completed', 'item': dict(done, status='failed')}), 'failed status')
        self.assert_not_loaded(events(started, {'type': 'item.completed', 'item': dict(done, exit_code=None)}), 'no exit status')
        self.assert_not_loaded(events(started, {'type': 'item.completed', 'item': dict(done, exit_code=True)}), 'boolean exit status')
        # The output of the completion belongs to the call it completes, not to a later or earlier one.
        other = dict(item, id='i2', command="/bin/zsh -lc 'python3 show.py'")
        self.assert_not_loaded(events(started, {'type': 'item.started', 'item': other},
                                      {'type': 'item.completed', 'item': dict(other, aggregated_output=self.body, exit_code=0, status='completed')},
                                      {'type': 'item.completed', 'item': dict(done, aggregated_output='')}), 'body printed by the other call')
        # Without the installed skill text to compare with, nothing can be shown.
        unreferenced = common.extract_agent_events('codex', '\n'.join(json.dumps(line) for line in (started, completed)).encode(), {}, None)
        self.assertFalse(common.skill_access(unreferenced)['loaded'])

    def test_claude_skill_calls_need_the_hosts_confirmation_and_its_own_delivery(self):
        delivery = self.h._skill_delivery
        body = common.skill_body_text(self.body)
        call, result, text = delivery()
        ok = ('assistant', [{'type': 'text', 'text': 'ok'}])
        bash = ('assistant', [{'type': 'tool_use', 'id': 'b1', 'name': 'Bash', 'input': {'command': 'echo body'}}])
        echoed = ('user', [{'type': 'tool_result', 'tool_use_id': 'b1', 'content': common.SKILL_DELIVERY_PREFIX + self.h.CLAUDE_SKILL_DIRECTORY + '\n\n' + body}])
        cases = {
            'requested only': [call],
            'confirmed but nothing delivered': [call, result],
            'delivered without the host confirming the launch': delivery(confirmed=False),
            'delivered text is not marked as the host\'s own': delivery(synthetic=False),
            'call reported as failed': delivery(error=True),
            'another skill': delivery(name='mandala'),
            'alias name': delivery(name='/stateripple'),
            'body from a personal skill directory': delivery(base='<HOME>/.claude/skills/stateripple'),
            'body from another workspace': delivery(base='/elsewhere/workspace/.claude/skills/stateripple'),
            'body with a line missing': delivery(body='\n'.join(body.splitlines()[:-1])),
            'body with a line added': delivery(body=body + '\nAlso run rm.'),
            'other text': delivery(body='# Another skill\n' + 'x' * 200),
            'delivery after the assistant spoke again': [call, result, ok, text],
            'delivery with no Skill call': [text],
            'delivery before the call': [text, call, result],
            'body printed by a command, Skill call confirmed but empty': [call, result, ok, bash, echoed],
            'confirmation for another id': [call, ('user', [dict(result[1][0], tool_use_id='zzz')], result[2]), text],
            'two results for the call': [call, result, result, text],
            'two calls under one id': [call, call, result, text],
            'host names another command': [call, (result[0], result[1], {'tool_use_result': {'success': True, 'commandName': 'other'}}), text],
            'host reports no success': [call, (result[0], result[1], {'tool_use_result': {'success': False, 'commandName': 'stateripple'}}), text],
        }
        for label, blocks in cases.items():
            self.assert_not_loaded(self.claude(blocks), label, 'claude')
        # Results of other calls made in the same step may arrive in between; the delivery still belongs to the Skill call.
        ls = ('assistant', [{'type': 'tool_use', 'id': 'b2', 'name': 'Bash', 'input': {'command': 'ls'}}])
        listed = ('user', [{'type': 'tool_result', 'tool_use_id': 'b2', 'content': 'inventory.py'}])
        self.assertTrue(common.skill_access(self.claude([call, ls, result, listed, text]))['loaded'])

    def test_claude_read_and_bash_need_the_exact_file_from_the_installed_path(self):
        numbered = '\n'.join('%6d→%s' % (number, line) for number, line in enumerate(self.body.splitlines(), 1))

        def run(tool, payload, content, error=False):
            return self.claude([('assistant', [{'type': 'tool_use', 'id': 't1', 'name': tool, 'input': payload}]),
                                ('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': content, 'is_error': error}])])
        self.assertTrue(common.skill_access(run('Read', {'file_path': CLAUDE_BODY}, numbered))['loaded'])
        for label, events in {
                'read failed': run('Read', {'file_path': CLAUDE_BODY}, numbered, True),
                'read of part of the file': run('Read', {'file_path': CLAUDE_BODY}, '\n'.join(numbered.splitlines()[:10])),
                'read returned other text': run('Read', {'file_path': CLAUDE_BODY}, '     1→unrelated'),
                'read of a home copy': run('Read', {'file_path': '<HOME>/.claude/skills/stateripple/SKILL.md'}, numbered),
                'read with a relative path': run('Read', {'file_path': '.claude/skills/stateripple/SKILL.md'}, numbered),
                'read of the codex directory': run('Read', {'file_path': '<EVAL_TMP>/workspace/' + CODEX_BODY}, numbered),
                'read of a path that leaves the workspace': run('Read', {'file_path': '<EVAL_TMP>/workspace/../x/.claude/skills/stateripple/SKILL.md'}, numbered),
                'bash cat with a relative path': run('Bash', {'command': 'cat .claude/skills/stateripple/SKILL.md'}, self.body),
                'bash short circuit': run('Bash', {'command': 'true || cat ' + CLAUDE_BODY}, self.body),
                'bash echo then unexecuted cat': run('Bash', {'command': 'echo x; false && cat ' + CLAUDE_BODY}, self.body),
                'bash cat wrapped in a shell': run('Bash', {'command': "/bin/zsh -lc 'cat %s'" % CLAUDE_BODY}, self.body),
                'bash cat reported as failed': run('Bash', {'command': 'cat ' + CLAUDE_BODY}, self.body, True),
                'grep returned the body': run('Grep', {'pattern': '.', 'path': CLAUDE_BODY}, self.body),
                'unknown tool returned the body': run('Fetch', {'path': CLAUDE_BODY}, self.body)}.items():
            self.assert_not_loaded(events, label, 'claude')
        self.assertTrue(common.skill_access(run('Bash', {'command': 'cat ' + CLAUDE_BODY}, self.body.rstrip('\n')))['loaded'])
        # A result that answers no call, or arrives before its call, shows nothing.
        orphan = ('user', [{'type': 'tool_result', 'tool_use_id': 'r9', 'content': numbered}])
        use = ('assistant', [{'type': 'tool_use', 'id': 'r9', 'name': 'Read', 'input': {'file_path': CLAUDE_BODY}}])
        self.assert_not_loaded(self.claude([orphan]), 'result without a call', 'claude')
        self.assert_not_loaded(self.claude([orphan, use]), 'result before its call', 'claude')

    def test_a_positive_needs_the_body_and_a_negative_must_not_get_it(self):
        # A positive case whose agent never obtained the skill body fails; a negative case whose agent did, fails too.
        self.assertEqual(self.positive(self.codex([("/bin/zsh -lc 'stateripple version'", 'stateripple v0.1.0', 0)]))[0], 'FAIL')
        self.assertEqual(self.positive(self.codex([]))[0], 'FAIL')
        self.assertEqual(self.negative(self.codex([("/bin/zsh -lc 'ls'", 'inventory.py', 0)])), 'PASS')
        for label, events, agent in (
                ('codex cat', self.codex([("/bin/zsh -lc 'cat %s'" % CODEX_BODY, self.body, 0)]), 'codex'),
                ('body printed by an unnamed program', self.codex([("/bin/zsh -lc 'python3 walk.py'", self.body, 0)]), 'codex'),
                ('one reference file', self.codex([("/bin/zsh -lc './x'", self.files['references/routing.md'], 0)]), 'codex'),
                ('a few lines of the body', self.codex([("/bin/zsh -lc './x'", '\n'.join(self.body.splitlines()[5:9]), 0)]), 'codex'),
                ('claude skill', self.claude(self.h._skill_delivery()), 'claude'),
                ('claude skill under another name', self.claude(self.h._skill_delivery(name='zzz')), 'claude'),
                ('host text with no call', self.claude([self.h._skill_delivery()[2]]), 'claude')):
            self.assertEqual(self.negative(events, agent), 'FAIL', label)

    def test_requests_the_recorder_cannot_read_unambiguously_prove_nothing(self):
        # Internal audit A3-04: a placeholder typed by the agent looked like the sanitised installed path.
        for request in ("/bin/zsh -lc 'cat @EVAL_TMP@/workspace/%s'" % CODEX_BODY, "/bin/zsh -lc 'cat /private@EVAL_TMP@/workspace/%s'" % CODEX_BODY):
            self.assert_not_loaded(self.codex([(request, self.body, 0)]), request)
        literal = json.dumps({'type': 'item.started', 'item': {'id': 'i', 'type': 'command_execution', 'command': "/bin/zsh -lc 'cat <EVAL_TMP>/workspace/%s'" % CODEX_BODY}})
        done = json.dumps({'type': 'item.completed', 'item': {'id': 'i', 'type': 'command_execution', 'command': "/bin/zsh -lc 'cat <EVAL_TMP>/workspace/%s'" % CODEX_BODY,
                                                              'aggregated_output': self.body, 'exit_code': 0, 'status': 'completed'}})
        typed = common.extract_agent_events('codex', (literal + '\n' + done).encode(), {}, self.files)
        self.assertTrue(typed['tool_calls'][0]['ambiguous'])
        self.assertFalse(common.skill_access(typed)['loaded'])
        self.assertIn('a tool call cannot be read unambiguously', common.tool_log_problems(dict(typed, json_parse_errors=0, tool_log_version=common.TOOL_LOG_VERSION)))
        for name, payload in (('Read', {'file_path': '<EVAL_TMP>/workspace/.claude/skills/stateripple/SKILL.md'}),
                              ('Bash', {'command': 'cat <EVAL_TMP>/workspace/.claude/skills/stateripple/SKILL.md'})):
            stream = '\n'.join(json.dumps(event) for event in (
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't', 'name': name, 'input': payload}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': self.body}]}}))
            self.assertFalse(common.skill_access(common.extract_agent_events('claude', stream.encode(), {}, self.files))['loaded'], name)
        spoofed = self.h._skill_delivery()
        raw = '\n'.join(json.dumps(dict({'type': item[0], 'message': {'content': item[1]}}, **(item[2] if len(item) > 2 else {}))) for item in spoofed)
        self.assertFalse(common.skill_access(common.extract_agent_events('claude', raw.encode(), {}, self.files))['loaded'])
        # A3-05: the request is the tool's own input field and nothing else.
        numbered = '\n'.join('%6d\u2192%s' % (number, line) for number, line in enumerate(self.body.splitlines(), 1))

        def run(tool, payload, content):
            return self.claude([('assistant', [{'type': 'tool_use', 'id': 't1', 'name': tool, 'input': payload}]),
                                ('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': content}])])
        self.assert_not_loaded(run('Read', {'command': CLAUDE_BODY, 'file_path': '/etc/hosts'}, numbered), 'read of another file', 'claude')
        self.assert_not_loaded(run('Read', {'command': CLAUDE_BODY}, numbered), 'read with no file_path', 'claude')
        self.assert_not_loaded(run('Read', {'file_path': CLAUDE_BODY, 'command': 'x'}, numbered), 'read with a second request field', 'claude')
        self.assert_not_loaded(run('Bash', {'command': 5, 'file_path': 'cat ' + CLAUDE_BODY}, self.body), 'bash with no command', 'claude')
        self.assert_not_loaded(run('Bash', {'command': 'cat ' + CLAUDE_BODY, 'skill': 'stateripple'}, self.body), 'bash with a second request field', 'claude')
        self.assertTrue(common.skill_access(run('Read', {'file_path': CLAUDE_BODY, 'limit': 2000}, numbered))['loaded'])
        # Second internal review R3B-01: an event between start and completion that names another command, and
        # an item whose first event is not a start.
        show = "/bin/zsh -lc 'python3 show.py'"
        begun = {'type': 'item.started', 'item': {'id': 'i1', 'type': 'command_execution', 'command': "/bin/zsh -lc 'cat %s'" % CODEX_BODY, 'status': 'in_progress'}}
        for label, lines in (
                ('updated to another command', [begun, {'type': 'item.updated', 'item': dict(begun['item'], command=show)},
                                                {'type': 'item.completed', 'item': dict(begun['item'], command=show, aggregated_output=self.body, exit_code=0, status='completed')}]),
                ('updated to another command and back', [begun, {'type': 'item.updated', 'item': dict(begun['item'], command=show)},
                                                         {'type': 'item.completed', 'item': dict(begun['item'], aggregated_output=self.body, exit_code=0, status='completed')}]),
                ('first event is an update', [{'type': 'item.updated', 'item': begun['item']},
                                              {'type': 'item.completed', 'item': dict(begun['item'], aggregated_output=self.body, exit_code=0, status='completed')}])):
            events = common.extract_agent_events('codex', '\n'.join(json.dumps(line) for line in lines).encode(), {}, self.files)
            self.assertFalse(common.skill_access(events)['loaded'], label)
        honest = [begun, {'type': 'item.updated', 'item': begun['item']},
                  {'type': 'item.completed', 'item': dict(begun['item'], aggregated_output=self.body, exit_code=0, status='completed')}]
        self.assertTrue(common.skill_access(common.extract_agent_events(
            'codex', '\n'.join(json.dumps(line) for line in honest).encode(), {}, self.files))['loaded'])
        # A3-06: a completion for another command under the same identifier, or without a completed status.
        cat = "/bin/zsh -lc 'cat %s'" % CODEX_BODY
        start = {'type': 'item.started', 'item': {'id': 'i1', 'type': 'command_execution', 'command': cat, 'status': 'in_progress'}}
        for label, item in (('another command', {'command': "/bin/zsh -lc 'python3 show.py'", 'status': 'completed'}),
                            ('no command', {'status': 'completed'}), ('no status', {'command': cat}), ('status in progress', {'command': cat, 'status': 'in_progress'})):
            finish = {'type': 'item.completed', 'item': dict({'id': 'i1', 'type': 'command_execution', 'aggregated_output': self.body, 'exit_code': 0}, **item)}
            events = common.extract_agent_events('codex', (json.dumps(start) + '\n' + json.dumps(finish)).encode(), {}, self.files)
            self.assertFalse(common.skill_access(events)['loaded'], label)

    def test_tampered_proof_fields_fail_closed(self):
        events = self.codex([("/bin/zsh -lc 'cat %s'" % CODEX_BODY, self.body, 0)])
        for change in (lambda call: call.update(exact=None), lambda call: call.update(exact='references/routing.md'),
                       lambda call: call.update(exact=True), lambda call: call.update(exact='nonexistent.md'),
                       lambda call: call.update(started=False), lambda call: call.update(started='yes'),
                       lambda call: call.update(results=0), lambda call: call.update(results=2), lambda call: call.update(results=True),
                       lambda call: call.update(completed=False), lambda call: call.update(is_error=True), lambda call: call.update(is_error=None),
                       lambda call: call.update(exit_code=1), lambda call: call.update(exit_code=None), lambda call: call.update(exit_code=False),
                       lambda call: call.update(duplicate=True), lambda call: call.update(ambiguous=True), lambda call: call.update(ambiguous='no'),
                       lambda call: call.update(tool='Bash'), lambda call: call.update(tool=['Skill']),
                       lambda call: call.update(tool='Skill'), lambda call: call.update(request=None), lambda call: call.pop('exact'),
                       lambda call: call.pop('started'), lambda call: call.pop('results'), lambda call: call.update(output_chars=0)):
            changed = copy.deepcopy(events)
            change(changed['tool_calls'][0])
            self.assertEqual(self.positive(changed)[0], 'FAIL')
        skill = self.claude(self.h._skill_delivery())
        self.assertEqual(self.positive(skill, 'claude')[0], 'PASS')
        for change in (lambda call: call.update(host_confirmed=False), lambda call: call.update(host_confirmed='yes'),
                       lambda call: call.update(delivery=None), lambda call: call['delivery'].update(exact=False),
                       lambda call: call['delivery'].update(synthetic=False), lambda call: call['delivery'].update(base=None),
                       lambda call: call['delivery'].update(base='<HOME>/.claude/skills/stateripple'),
                       lambda call: call['delivery'].update(extra=1), lambda call: call.update(delivery='exact'),
                       lambda call: call.update(skill='other'), lambda call: call.update(skill=None), lambda call: call.update(results=2)):
            changed = copy.deepcopy(skill)
            change(changed['tool_calls'][0])
            self.assertEqual(self.positive(changed, 'claude')[0], 'FAIL')
        # Records of earlier runner versions carry no pairing of calls with results and decide nothing.
        for version in (1, 2, 3, True, '4', None):
            changed = copy.deepcopy(events)
            changed['tool_log_version'] = version
            self.assertEqual(self.positive(changed)[0], 'FAIL', version)
        old = {'final_response': 'x', 'tool_commands': ["/bin/zsh -lc 'cat %s'" % CODEX_BODY], 'read_paths': [], 'skill_invocations': [],
               'tool_uses': ['command_execution'], 'searches': [], 'json_parse_errors': 0, 'tool_log_version': 3,
               'skill_reference': events['skill_reference'],
               'tool_calls': [{'tool': 'command_execution', 'completed': True, 'is_error': False, 'exit_code': 0, 'request': "/bin/zsh -lc 'cat %s'" % CODEX_BODY,
                               'output_chars': 3180, 'output_sha256': '0' * 64, 'skill_content': {'SKILL.md': [17, 17]}}]}
        self.assertEqual(self.positive(old)[0], 'FAIL')

    def test_the_runner_records_each_turns_calls_separately_and_merges_them(self):
        first = self.codex([("/bin/zsh -lc 'cat %s'" % CODEX_BODY, self.body, 0)])
        second = self.codex([("/bin/zsh -lc 'stateripple version'", 'stateripple v0.1.0', 0)])
        merged = live.merge_turn_events([first, second])
        self.assertEqual([call['exact'] for call in merged['tool_calls']], ['SKILL.md', None])
        self.assertEqual(merged['tool_log_version'], common.TOOL_LOG_VERSION)
        self.assertEqual(common.tool_log_problems(merged), [])
        self.assertTrue(common.skill_access(merged)['loaded'])


def _write_run(root, name, kind, records, agent='codex', **manifest_fields):
    """A run directory with sealed records and a sealed manifest, like the runner and replay write them."""
    run = root / name
    run.mkdir()
    entries = []
    for file_name, record in records.items():
        common.write_json_exclusive(run / file_name, record)
        entry = {'case_id': record['case_id'], 'file': file_name, 'sha256': common.sha256_file(run / file_name), 'status': record['status']}
        if kind == 'replay':
            entry['agent'] = record['agent']['name']
        entries.append(entry)
    manifest = {'schema_version': 1, 'evaluation_type': kind + '-manifest', 'records': entries}
    if kind == 'live':
        manifest.update(agent=agent, suite='release', selected_cases=[entry['case_id'] for entry in entries], suite_complete=False)
    else:
        manifest['source'] = 'live-run'
    manifest.update(manifest_fields)
    common.write_json_exclusive(run / 'manifest.json', common.seal_record(manifest))
    return run


def _reseal_manifest(run, change):
    """Rewrite a manifest after a change and seal it correctly again, as a careful forger would."""
    manifest = json.loads((run / 'manifest.json').read_text())
    manifest.pop('integrity')
    change(manifest)
    (run / 'manifest.json').write_text(json.dumps(common.seal_record(manifest)))


class ManifestValidationTests(_Synthetic):
    """F2-02: the gate validates every entry of every manifest it relies on."""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = self.h._synthetic_record()
        self.revision = self.record['skill_revision']
        failed = copy.deepcopy({key: value for key, value in self.h._synthetic_record('L05').items() if key != 'integrity'})
        failed['observations']['filesystem_unchanged'] = False
        failed['automatic'] = common.grade_evidence(self.cases['L05'], failed)
        failed['status'] = 'FAIL'
        self.failed = common.seal_record(failed)
        self.live = _write_run(self.root, 'live-run', 'live', {'L01.json': self.record, 'L05.json': self.failed})
        self.replayed = replay.replay_record(self.record, self.live / 'L01.json', self.cases['L01'], {})
        self.replay = _write_run(self.root, 'replay-run', 'replay', {'L01-codex.json': self.replayed})
        # The gate is given only the adopted record; the kept failure is listed in the same manifest.
        self.adopted = [self.live / 'L01.json', self.replay / 'L01-codex.json']

    def tearDown(self):
        self.temp.cleanup()

    def status(self, paths=None):
        return coverage.evidence_manifest_status(self.adopted if paths is None else paths, self.revision)

    def assert_rejected(self, label, fragment=None):
        result = self.status()
        self.assertEqual(result['status'], 'FAIL', label)
        if fragment:
            self.assertIn(fragment, ' '.join(result['problems']), label)
        # The gate itself is blocked by it, whatever else is true.
        records = coverage.load_evidence_records(self.adopted) if all(path.is_file() for path in self.adopted) else {}
        report = coverage.release_report(self.h.catalogs, records, self.revision, ROOT, None, self.adopted)
        self.assertEqual(report['readiness'], 'NOT READY', label)
        self.assertTrue(any(reason.startswith('evidence manifests') for reason in report['blocking']), label)

    def test_intact_runs_pass_and_counts_separate_adopted_from_kept_records(self):
        result = self.status()
        self.assertEqual((result['status'], result['problems']), ('PASS', []))
        self.assertEqual((result['manifests'], result['entries'], result['adopted'], result['unadopted']), (2, 3, 2, 1))
        # A kept failure does not block: it is listed, present and exactly what the manifest says.
        self.assertEqual(self.failed['status'], 'FAIL')
        self.assertEqual(coverage.evidence_manifest_status(None)['status'], 'NOT_RUN')
        self.assertEqual(coverage.evidence_manifest_status([self.root / 'nothing'])['status'], 'FAIL')

    def test_a_listed_file_that_is_not_adopted_must_still_exist_and_match(self):
        # The audited case: the 88 adopted records are intact, another entry of the same manifest is not.
        (self.live / 'L05.json').unlink()
        self.assert_rejected('unadopted record deleted', 'missing')

    def test_resealed_manifest_with_an_added_missing_entry_is_rejected(self):
        _reseal_manifest(self.live, lambda manifest: (
            manifest['records'].append({'case_id': 'L09', 'file': 'missing.json', 'sha256': '0' * 64, 'status': 'PASS'}),
            manifest['selected_cases'].append('L09')))
        self.assert_rejected('added missing.json', 'missing')

    def test_unadopted_failure_replaced_by_a_missing_file_is_rejected(self):
        _reseal_manifest(self.live, lambda manifest: manifest['records'][1].update(file='gone.json'))
        self.assert_rejected('kept failure repointed', 'missing')

    def test_changed_content_and_broken_seals_are_rejected(self):
        original = (self.live / 'L05.json').read_bytes()
        (self.live / 'L05.json').write_bytes(original.replace(b'"FAIL"', b'"PASS"', 1))
        self.assert_rejected('content changed', 'differs from the hash')
        # The same change with the manifest hash updated and resealed: now the record's own seal gives it away.
        _reseal_manifest(self.live, lambda manifest: manifest['records'][1].update(sha256=common.sha256_file(self.live / 'L05.json')))
        self.assert_rejected('content changed, manifest resealed', 'not a sealed JSON record')
        (self.live / 'L05.json').write_text('{not json')
        _reseal_manifest(self.live, lambda manifest: manifest['records'][1].update(sha256=common.sha256_file(self.live / 'L05.json')))
        self.assert_rejected('invalid JSON', 'not a sealed JSON record')
        (self.live / 'L05.json').write_text('[1, 2]')
        _reseal_manifest(self.live, lambda manifest: manifest['records'][1].update(sha256=common.sha256_file(self.live / 'L05.json')))
        self.assert_rejected('not an object', 'not a sealed JSON record')

    def test_manifest_seal_and_structure(self):
        manifest = json.loads((self.live / 'manifest.json').read_text())
        manifest['records'][1]['status'] = 'PASS'
        (self.live / 'manifest.json').write_text(json.dumps(manifest))
        self.assert_rejected('manifest edited without resealing', 'not a sealed JSON object')
        for label, change in (('schema version', lambda m: m.update(schema_version=2)), ('boolean schema', lambda m: m.update(schema_version=True)),
                              ('kind', lambda m: m.update(evaluation_type='replay-manifest')), ('unknown kind', lambda m: m.update(evaluation_type='x')),
                              ('records not a list', lambda m: m.update(records={})), ('no records', lambda m: m.update(records=[])),
                              ('agent', lambda m: m.update(agent='someone')), ('no agent', lambda m: m.pop('agent')),
                              ('selected cases', lambda m: m.update(selected_cases=['L01'])),
                              ('entry not an object', lambda m: m['records'].append('L05.json')),
                              ('entry without hash', lambda m: m['records'][1].pop('sha256')),
                              ('entry with short hash', lambda m: m['records'][1].update(sha256='abc')),
                              ('entry with unknown status', lambda m: m['records'][1].update(status='OK')),
                              ('entry with empty case', lambda m: m['records'][1].update(case_id=''))):
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                _reseal_manifest(self.live, change)
                self.assert_rejected(label)

    def test_metadata_must_match_the_referenced_record(self):
        for label, change, fragment in (
                ('another case', lambda m: (m['records'][1].update(case_id='L06'), m.update(selected_cases=['L01', 'L06'])), 'another case'),
                ('another agent', lambda m: m.update(agent='claude'), 'another agent'),
                ('another status', lambda m: m['records'][1].update(status='PASS'), 'status differs'),
                ('entry repointed to another case\'s file', lambda m: m['records'][1].update(
                    file='L01.json', sha256=m['records'][0]['sha256']), 'more than once')):
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                _reseal_manifest(self.live, change)
                self.assert_rejected(label, fragment)
        # Replay manifests name the agent per entry.
        _reseal_manifest(self.replay, lambda manifest: manifest['records'][0].update(agent='claude'))
        self.assert_rejected('replay entry for another agent', 'another agent')

    def test_live_and_replay_cannot_be_exchanged(self):
        common.write_json_exclusive(self.live / 'L01-codex.json', self.replayed)
        _reseal_manifest(self.live, lambda manifest: (manifest['records'].append(
            {'case_id': 'L01', 'file': 'L01-codex.json', 'sha256': common.sha256_file(self.live / 'L01-codex.json'), 'status': self.replayed['status']}),
            manifest['selected_cases'].append('L01')))
        self.assert_rejected('replay record in a live run')
        self.tearDown()
        self.setUp()
        _reseal_manifest(self.replay, lambda manifest: manifest.update(evaluation_type='live-manifest', agent='codex', selected_cases=['L01']))
        self.assert_rejected('replay run relabelled as live')

    def test_duplicates_and_unlisted_files(self):
        _reseal_manifest(self.live, lambda manifest: manifest['records'].append(dict(manifest['records'][0])))
        self.assert_rejected('same file twice', 'more than once')
        self.tearDown()
        self.setUp()
        common.write_json_exclusive(self.live / 'L01-again.json', self.record)
        _reseal_manifest(self.live, lambda manifest: (manifest['records'].append(dict(manifest['records'][0], file='L01-again.json')),
                                                      manifest['selected_cases'].append('L01')))
        self.assert_rejected('same case and agent twice', 'more than once')
        self.tearDown()
        self.setUp()
        common.write_json_exclusive(self.live / 'extra.json', self.record)
        self.assert_rejected('file the manifest does not list', 'not listed')

    def test_paths_cannot_leave_the_run_directory(self):
        outside = self.root / 'outside.json'
        common.write_json_exclusive(outside, self.failed)
        digest = common.sha256_file(outside)
        (self.live / 'sub').mkdir()
        common.write_json_exclusive(self.live / 'sub' / 'inner.json', self.failed)
        for name in ('../outside.json', str(outside), 'sub/inner.json', './L05.json', 'L05.json/', '', '.', '..', 'manifest.json', 'L05', '.json',
                     'a\\..\\outside.json', 'L05.json\x00.json', None, 5, ['L05.json']):
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                common.write_json_exclusive(self.root / 'outside.json', self.failed)
                _reseal_manifest(self.live, lambda manifest: manifest['records'][1].update(file=name, sha256=digest))
                self.assert_rejected('path %r' % (name,))
        self.tearDown()
        self.setUp()
        common.write_json_exclusive(self.root / 'outside.json', self.failed)
        (self.live / 'L05.json').unlink()
        (self.live / 'L05.json').symlink_to(self.root / 'outside.json')
        self.assert_rejected('listed file is a link', 'link')

    def test_other_revisions_and_adopted_files_outside_a_manifest(self):
        result = coverage.evidence_manifest_status(self.adopted, 'another-revision')
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('another skill revision', ' '.join(result['problems']))
        loose = self.root / 'loose'
        loose.mkdir()
        common.write_json_exclusive(loose / 'L01.json', self.record)
        self.assertIn('no manifest', ' '.join(self.status([loose])['problems']))
        (self.live / 'manifest.json').unlink()
        (self.live / 'manifest.json').symlink_to(self.replay / 'manifest.json')
        self.assert_rejected('manifest is a link', 'no manifest')

    def test_listed_release_evidence_is_complete(self):
        listing = ROOT / 'docs/RELEASE_EVIDENCE.txt'
        paths = [ROOT / line.strip() for line in listing.read_text().splitlines() if line.strip()]
        if not all(path.exists() for path in paths):
            self.skipTest('release evidence is not present in this checkout')
        records = coverage.load_evidence_records(paths)
        result = coverage.evidence_manifest_status(paths, next(iter(records.values()))['skill_revision'])
        self.assertEqual(result['problems'], [])
        self.assertEqual(result['adopted'], len(records))
        self.assertEqual(result['entries'], result['adopted'] + result['unadopted'])
        live_count = len([key for key in records if key[2] == 'live'])
        self.assertEqual((live_count, result['adopted']), (44, 88))




class ProcessGroupTests(unittest.TestCase):
    """F2-04: the group is signalled only while the child's id is still held by the child."""

    def _run(self, script, timeout=10, interrupt=False):
        created = []
        real_popen = subprocess.Popen
        real_wait = gitenv._wait_for_exit

        def popen(*args, **kwargs):
            created.append(real_popen(*args, **kwargs))
            return created[-1]

        def wait(process, limit):
            if interrupt:
                time.sleep(0.4)
                raise KeyboardInterrupt
            return real_wait(process, limit)
        with tempfile.TemporaryDirectory() as temp, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with patch.object(gitenv.subprocess, 'Popen', side_effect=popen), patch.object(gitenv, '_wait_for_exit', wait):
                try:
                    outcome = common.run_process(['/bin/sh', '-c', script.replace('@TEMP@', temp)], Path(temp), None, timeout)
                except KeyboardInterrupt as error:
                    outcome = error
            marker = Path(temp) / 'pid'
            leftover = int(marker.read_text().strip()) if marker.exists() and marker.read_text().strip() else None
            collected = [process.returncode is not None for process in created]
            del created[:]
            gc.collect()
        return outcome, collected, leftover, [str(item.message) for item in caught if issubclass(item.category, ResourceWarning)]

    def _gone(self, pid):
        for _ in range(80):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        os.kill(pid, 9)
        return False

    def test_every_path_ends_the_group_collects_the_child_and_leaks_nothing(self):
        leftover = 'sleep 30 >/dev/null 2>&1 & echo $! > @TEMP@/pid; '
        for name, script, timeout, interrupt in (('normal', leftover + 'echo out; echo err >&2; exit 3', 10, False),
                                                 ('timeout', leftover + 'echo out; sleep 30', 1, False),
                                                 ('interrupt', leftover + 'echo out; sleep 30', 10, True)):
            outcome, collected, pid, warned = self._run(script, timeout, interrupt)
            self.assertEqual(collected, [True], name)
            self.assertEqual(warned, [], name)
            self.assertIsNotNone(pid, name)
            self.assertTrue(self._gone(pid), name)
            if name == 'normal':
                self.assertEqual((outcome['exit_code'], outcome['timed_out'], outcome['stdout'], outcome['stderr']), (3, False, b'out\n', b'err\n'))
            elif name == 'timeout':
                self.assertEqual((outcome['exit_code'], outcome['timed_out'], outcome['stdout']), (None, True, b'out\n'))
            else:
                self.assertIsInstance(outcome, KeyboardInterrupt)

    def test_a_collected_child_is_never_signalled_again(self):
        # After the child is collected its id may belong to an unrelated process: nothing may be sent to it.
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.Popen(['/bin/sh', '-c', 'exit 0'], cwd=temp, env=gitenv.safe_environment(), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            process.wait(timeout=10)
            with patch.object(gitenv.os, 'killpg', side_effect=AssertionError('signalled a collected child')), \
                    patch.object(process, 'kill', side_effect=AssertionError('signalled a collected child')):
                gitenv.end_process_group(process)
                gitenv.end_process_group(process)
            # In the runner the group is signalled exactly once, and at that moment the child has not been collected.
            states = []
            real_killpg = os.killpg

            def killpg(pid, sig):
                states.append((pid, created[0].returncode))
                return real_killpg(pid, sig)
            created = []
            real_popen = subprocess.Popen

            def popen(*args, **kwargs):
                created.append(real_popen(*args, **kwargs))
                return created[-1]
            with patch.object(gitenv.subprocess, 'Popen', side_effect=popen), patch.object(gitenv.os, 'killpg', killpg):
                result = gitenv.run_group(['/bin/sh', '-c', 'exit 5'], temp, None, 10)
            self.assertEqual(result['exit_code'], 5)
            self.assertEqual(states, [(created[0].pid, None)])
            self.assertEqual(created[0].returncode, 5)
            # Internal audit N-06: if something else collected the child, its id is no longer held and nothing is signalled.
            with patch.object(gitenv, '_peek_exit', lambda: (lambda pid: (_ for _ in ()).throw(ChildProcessError(10, 'No child processes')))), \
                    patch.object(gitenv.os, 'killpg', side_effect=AssertionError('signalled an id that is no longer held')):
                gone = subprocess.Popen(['/bin/sh', '-c', 'exit 0'], cwd=temp, env=gitenv.safe_environment(), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                with self.assertRaises(gitenv.ChildLost):
                    gitenv._wait_for_exit(gone, 5)
                gitenv.end_process_group(gone)
                os.waitpid(gone.pid, 0)
                # Second internal review R3B-04: the unknown exit status is a failure for every caller, never a success.
                lost = gitenv.run_group(['/bin/sh', '-c', 'exit 0'], temp, None, 5)
                self.assertEqual((lost['exit_code'], lost['timed_out']), (None, False))
                self.assertIn('collected elsewhere', lost['launch_error'])
                self.assertEqual(gitenv.run_bounded(['/bin/sh', '-c', 'exit 0'], temp, None, 5).returncode, 127)
                self.assertEqual(release_check._run(Path(temp), 'step', ['/bin/sh', '-c', 'exit 0']).status, 'FAIL')
                self.assertTrue(common.run_process(['/bin/sh', '-c', 'exit 0'], Path(temp), None, 5)['launch_error'])
            self.assertIsNotNone(gone.returncode)
            # Final-diff check R3C-01: collected by something else after the exit was seen and before this module
            # collects it. The standard library would call that exit status 0.
            real_wait = gitenv._wait_for_exit

            def stolen(process, limit):
                seen = real_wait(process, limit)
                os.waitpid(process.pid, 0)
                return seen
            with patch.object(gitenv, '_wait_for_exit', stolen):
                for command in ('/usr/bin/false', '/usr/bin/true'):
                    taken = gitenv.run_group([command], temp, None, 5)
                    self.assertEqual((taken['exit_code'], taken['timed_out']), (None, False), command)
                    self.assertIn('collected elsewhere', taken['launch_error'])
                    self.assertEqual(gitenv.run_bounded([command], temp, None, 5).returncode, 127)
                self.assertEqual(release_check._run(Path(temp), 'step', ['/usr/bin/true']).status, 'FAIL')
            # Collected normally, the real status comes back, also for a signal.
            self.assertEqual(gitenv.run_group(['/bin/sh', '-c', 'kill -9 $$'], temp, None, 5)['exit_code'], -9)
            self.assertEqual(gitenv.run_group(['/bin/sh', '-c', 'exit 7'], temp, None, 5)['exit_code'], 7)
            # An evidence file whose kind is not a string is skipped, not a crash (R3C-02).
            odd = Path(temp) / 'odd'
            odd.mkdir()
            (odd / 'x.json').write_text(json.dumps({'evaluation_type': ['live'], 'schema_version': 1, 'case_id': 'L01', 'agent': {'name': 'codex'}}))
            self.assertEqual(coverage.load_evidence_records([odd]), {})
            self.assertEqual(common.evidence_index([odd / 'x.json']), {})

    def test_exit_is_observed_without_collecting_on_this_platform(self):
        peek = gitenv._peek_exit()
        if peek is None:
            self.skipTest('this platform cannot report an exit without collecting the child')
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.Popen(['/bin/sh', '-c', 'sleep 0.3'], cwd=temp, env=gitenv.safe_environment(), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                self.assertFalse(peek(process.pid))
                self.assertTrue(gitenv._wait_for_exit(process, 10))
                self.assertTrue(peek(process.pid))
                # Still not collected: the id is held, and collecting it now returns the real status.
                self.assertIsNone(process.returncode)
                os.kill(process.pid, 0)
            finally:
                gitenv.end_process_group(process)
            self.assertEqual(process.returncode, 0)
            slow = subprocess.Popen(['/bin/sh', '-c', 'sleep 30'], cwd=temp, env=gitenv.safe_environment(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                started = time.monotonic()
                self.assertFalse(gitenv._wait_for_exit(slow, 0.3))
                self.assertLess(time.monotonic() - started, 2)
            finally:
                gitenv.end_process_group(slow)
            self.assertIsNotNone(slow.returncode)

    def test_a_process_that_leaves_the_group_cannot_hold_the_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            holder = 'import os,time; os.setsid(); time.sleep(4)'
            started = time.monotonic()
            result = common.run_process(['/bin/sh', '-c', '%s -c "%s" & echo done' % (sys.executable, holder)], Path(temp), None, 10)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual((result['exit_code'], result['timed_out'], result['stdout']), (0, False, b'done\n'))

    def test_release_check_and_bounded_runner_results(self):
        with tempfile.TemporaryDirectory() as temp, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.assertEqual(release_check._execute(Path(temp), ['/bin/sh', '-c', 'echo out; exit 4'], 5).stdout, 'out\n')
            self.assertEqual(release_check._execute(Path(temp), ['/bin/sh', '-c', 'sleep 30'], 1).returncode, 124)
            self.assertEqual(gitenv.run_bounded(['/nonexistent/program'], Path(temp), None, 5).returncode, 127)
            self.assertEqual(gitenv.run_bounded([], Path(temp), None, 5).returncode, 127)
            self.assertEqual(gitenv.run_bounded(['/bin/sh', '-c', "printf '\\377'"], Path(temp), None, 5).returncode, 0)
            self.assertTrue(common.run_process(['/nonexistent/program'], Path(temp), None, 5)['launch_error'])
            gc.collect()
        self.assertEqual([str(item.message) for item in caught if issubclass(item.category, ResourceWarning)], [])


if __name__ == '__main__':
    unittest.main()
