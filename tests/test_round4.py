"""Regression tests for the third independent audit (FR-01 .. FR-04).

FR-01: the whole agent program, Codex included, runs under the operating-system sandbox.
FR-02: a refused write is an errno reported by the operating system for this case, never a non-zero exit.
FR-03: replay validates a run's manifest exactly as the release gate does.
FR-04: cleanup never replaces the result or the error of a case.

Only temporary directories are written. The tests that start the real sandbox start no agent.
"""
from __future__ import annotations

import copy
import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_evaluation
import test_round3
from scripts import eval_common as common
from scripts import eval_coverage as coverage
from scripts import eval_live as live
from scripts import eval_replay as replay
from scripts import gitenv
from scripts import writeguard

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = sys.platform == 'darwin' and os.access(writeguard.SANDBOX_EXEC, os.X_OK)
NONCE = '0123456789abcdef0123456789abcdef'


class _Base(unittest.TestCase):
    def setUp(self):
        self.h = test_evaluation.EvaluationTests('test_synthetic_grader_does_not_replace_manual_or_live')
        self.h.setUp()
        self.cases = common.case_map(self.h.catalogs, include_activation=True)

    def graded(self, change=None, agent='codex', commands=None):
        """Status and the no-external-writes check of an L01 record after a change."""
        record = copy.deepcopy(self.h._synthetic_record())
        if agent != 'codex':
            record['agent'] = {'name': agent, 'version': 'test-double', 'argv': self.h._argv(agent)}
            record['observations'].update(write_guard=self.h._guard(agent, 'L01'), agent_events=self.h._events(agent=agent))
        if commands is not None:
            record['observations']['agent_events']['tool_commands'] = commands
        if change:
            change(record)
        graded = common.grade_evidence(self.cases['L01'], record)
        check = next((item for item in graded['checks'] if item['id'] == 'no-external-writes'), {'pass': False, 'restriction': ['not graded']})
        return graded['status'], check


class AgentSandboxTests(_Base):
    """FR-01: one profile around the whole agent program, for both agents."""

    def test_both_agents_are_started_whole_under_the_profile(self):
        workspace = Path('<EVAL_TMP>/workspace')
        prefix = [writeguard.SANDBOX_EXEC, '-f', '<EVAL_TMP>.write-guard.sb']
        for agent in writeguard.AGENTS:
            for session, resume in ((None, False), ('0b1c2d3e-aaaa-4bbb-8ccc-000000000001', False), ('0b1c2d3e-aaaa-4bbb-8ccc-000000000001', True)):
                argv = common.agent_command(agent, '/opt/homebrew/bin/' + agent, workspace, 'the prompt', session, resume)
                self.assertEqual(argv[:3], prefix, (agent, session, resume))
                self.assertEqual(argv[3], '/opt/homebrew/bin/' + agent)
                self.assertEqual(argv[-1], 'the prompt')
                self.assertEqual(writeguard.guard_problems(agent, 'L01', NONCE, self.h._guard(agent, 'L01', NONCE), [argv[:-1]]), [], argv)
        codex = common.agent_command('codex', '/opt/homebrew/bin/codex', workspace, 'p')
        # The Codex program itself is inside the sandbox; its own, inner sandbox is off because it cannot nest.
        self.assertIn('--dangerously-bypass-approvals-and-sandbox', codex)
        self.assertFalse(any('sandbox_mode' in word or 'writable_roots' in word or word == '--sandbox' for word in codex))
        self.assertEqual(writeguard.MECHANISM, 'macos-seatbelt-profile')
        with self.assertRaises(common.EvaluationError):
            common.agent_command('other', '/x/other', workspace, 'p')

    def test_profile_allows_the_tree_and_nothing_of_the_home_directory_for_codex(self):
        for agent in writeguard.AGENTS:
            lines = writeguard.sandbox_profile(agent, '<EVAL_TMP>', '<HOME>').splitlines()
            self.assertEqual(lines[:3], ['(version 1)', '(allow default)', '(deny file-write*)'], agent)
            self.assertEqual(len(lines), 5)
            self.assertEqual(lines[4], '(deny file-write* (literal "<EVAL_TMP>"))')
            devices = '(literal "/dev/null") (literal "/dev/fd/1") (literal "/dev/fd/2") (literal "/dev/dtracehelper") (literal "/dev/tty")'
            session = ' (subpath "<HOME>/.claude/projects/-EVAL-TMP--workspace")' if agent == 'claude' else ''
            self.assertEqual(lines[3], '(allow file-write* (subpath "<EVAL_TMP>") %s%s)' % (devices, session), agent)
            self.assertNotIn('regex', '\n'.join(lines))
        self.assertNotIn('<HOME>', writeguard.sandbox_profile('codex', '<EVAL_TMP>', '<HOME>'))
        self.assertNotEqual(writeguard.policy_sha256('claude'), writeguard.policy_sha256('codex'))
        for bad in ('/tmp/a"b', '/tmp/a b', '/tmp/a\\b', '/tmp/a(b', '/tmp/a\nb', ''):
            with self.assertRaises(ValueError):
                writeguard.sandbox_profile('codex', bad, '<HOME>')
            with self.assertRaises(ValueError):
                writeguard.sandbox_profile('claude', '<EVAL_TMP>', bad)
        with self.assertRaises(ValueError):
            writeguard.sandbox_profile('other', '<EVAL_TMP>', '<HOME>')

    LOGIN = {'OPENAI_API_KEY': None, 'auth_mode': 'chatgpt', 'last_refresh': '2026-10-05T00:00:00Z',
             'tokens': {'access_token': 'a' * 40, 'account_id': 'acct-1', 'id_token': 'i' * 40, 'refresh_token': 'r' * 40}}

    def _renewed(self, **changes):
        login = copy.deepcopy(self.LOGIN)
        login['last_refresh'] = '2026-10-13T00:00:00Z'
        login['tokens'].update(access_token='A' * 40, id_token='I' * 40, refresh_token='R' * 40)
        for key, value in changes.items():
            if key in login['tokens']:
                login['tokens'][key] = value
            else:
                login[key] = value
        return json.dumps(login)

    def test_agent_state_stays_inside_the_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp).resolve() / 'stateripple-live-l01-abc'
            tree.mkdir()
            home = Path(temp).resolve() / 'home'
            (home / '.codex').mkdir(parents=True)
            host = home / '.codex' / 'auth.json'
            original = json.dumps(self.LOGIN)
            host.write_text(original)
            self.assertEqual(writeguard.agent_environment('codex', tree), {'CODEX_HOME': str(tree / 'host-state' / 'codex-home')})
            self.assertEqual(writeguard.agent_environment('claude', tree), {'CLAUDE_CODE_TMPDIR': str(tree / 'host-state')})
            with patch.object(writeguard.Path, 'home', lambda: home), patch.dict(os.environ, {}, clear=False):
                os.environ.pop(writeguard.RETURN_LOGIN_VARIABLE, None)
                self.assertIsNone(writeguard.prepare_agent_state('claude', tree))
                digest = writeguard.prepare_agent_state('codex', tree)
                copy_path = tree / 'host-state' / 'codex-home' / 'auth.json'
                self.assertEqual(copy_path.read_text(), original)
                self.assertEqual(oct(copy_path.stat().st_mode)[-3:], '600')
                self.assertIsNone(writeguard.return_agent_state('codex', tree, digest))          # unchanged
                self.assertIsNone(writeguard.return_agent_state('claude', tree, digest))
                self.assertIsNone(writeguard.return_agent_state('codex', tree, None))
                # A renewal is reported and, by default, not written to the user's file.
                copy_path.write_text(self._renewed())
                self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_NOT_RETURNED)
                self.assertEqual(host.read_text(), original)
                # Internal review AR4-01: whatever else the agent writes there never reaches the user's file,
                # with or without the opt-in, and is reported as tampering.
                tampered = ['{"OPENAI_API_KEY": "sk-ATTACKER"}', '{}', '0', '[]', '{not json', '"' + 'x' * 100 + '"',
                            self._renewed(OPENAI_API_KEY='sk-ATTACKER'), self._renewed(auth_mode='apikey'), self._renewed(account_id='acct-2'),
                            self._renewed(extra='field'), self._renewed(access_token='short'), self._renewed(refresh_token='has space ' * 4),
                            self._renewed(id_token=5), self._renewed(last_refresh=None), json.dumps(dict(self.LOGIN, tokens='gone')),
                            self._renewed() + ' ' * (writeguard.LOGIN_SIZE_LIMIT + 1), '[' * 5000 + ']' * 5000]
                for opt_in in ('0', '1'):
                    with patch.dict(os.environ, {writeguard.RETURN_LOGIN_VARIABLE: opt_in}):
                        for content in tampered:
                            copy_path.write_text(content)
                            self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED, content[:40])
                            self.assertEqual(host.read_text(), original)
                        copy_path.unlink()
                        self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        copy_path.symlink_to(host)
                        self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        copy_path.unlink()
                # Only with the operator's opt-in, and only a change shaped like a renewal, is written back.
                copy_path.write_text(self._renewed())
                with patch.dict(os.environ, {writeguard.RETURN_LOGIN_VARIABLE: '1'}):
                    self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_RETURNED)
                    self.assertEqual(json.loads(host.read_text())['tokens']['refresh_token'], 'R' * 40)
                    self.assertEqual(oct(host.stat().st_mode)[-3:], '600')
                    self.assertEqual(sorted(item.name for item in (home / '.codex').iterdir()), ['auth.json'])
                    # The user's own file changed in the meantime: it is left alone.
                    copy_path.write_text(self._renewed(refresh_token='Q' * 40))
                    self.assertIn('left alone', writeguard.return_agent_state('codex', tree, digest))
                    self.assertEqual(json.loads(host.read_text())['tokens']['refresh_token'], 'R' * 40)
                    # A leftover of an interrupted write does not stay behind, and nothing is raised.
                    host.write_text(original)
                    stale = host.with_name('auth.json.stateripple-eval-%d' % os.getpid())
                    stale.write_text('stale')
                    self.assertIn('could not', writeguard.return_agent_state('codex', tree, digest))
                    self.assertEqual(host.read_text(), original)
                    stale.unlink()
            # No login to copy, or a login that is a link: Codex gets an empty state directory and cannot start.
            with patch.object(writeguard.Path, 'home', lambda: Path(temp) / 'no-such-home'):
                self.assertIsNone(writeguard.prepare_agent_state('codex', Path(temp)))

    def test_a_tampered_login_copy_fails_the_case(self):
        status, _ = self.graded()
        self.assertEqual(status, 'PASS')
        for cleanup, expected in (([], 'PASS'), ([writeguard.LOGIN_NOT_RETURNED], 'PASS'), ([writeguard.LOGIN_RETURNED], 'PASS'),
                                  (['the session directory could not be removed: PermissionError'], 'PASS'),
                                  ([writeguard.LOGIN_TAMPERED], 'FAIL'), ([writeguard.LOGIN_NOT_RETURNED, writeguard.LOGIN_TAMPERED], 'FAIL'),
                                  (None, 'FAIL'), ('none', 'FAIL'), ([5], 'FAIL'), ([None], 'FAIL')):
            status, _ = self.graded(lambda record, cleanup=cleanup: record['process'].update(cleanup=cleanup))
            self.assertEqual(status, expected, cleanup)
        self.assertEqual(self.graded(lambda record: record['process'].pop('cleanup'))[0], 'FAIL')

    def test_the_runner_never_writes_through_what_the_agent_left_in_the_tree(self):
        # Internal review AR4-02: the follow-up snapshot was copied through a symbolic link the agent had planted.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            workspace, outside = base / 'tree' / 'workspace', base / 'outside'
            workspace.mkdir(parents=True)
            outside.mkdir()
            source = base / 'new.json'
            source.write_text('{"new": true}')
            victim = outside / 'victim.txt'
            victim.write_text('untouched')
            target = workspace / 'fixture.json'
            target.write_text('{"old": true}')
            self.assertTrue(live.replace_fixture(source, target, workspace))
            self.assertEqual((target.read_text(), target.is_symlink(), sorted(item.name for item in workspace.iterdir())),
                             ('{"new": true}', False, ['fixture.json']))
            target.unlink()
            target.symlink_to(victim)
            self.assertFalse(live.replace_fixture(source, target, workspace))
            self.assertEqual(victim.read_text(), 'untouched')
            target.unlink()
            target.mkdir()
            self.assertFalse(live.replace_fixture(source, target, workspace))
            target.rmdir()
            self.assertFalse(live.replace_fixture(source, target, workspace))          # gone
            self.assertFalse(live.replace_fixture(source, outside / 'fixture.json', workspace))
            # The workspace itself replaced by a link to somewhere else.
            moved = base / 'tree' / 'moved'
            workspace.rename(moved)
            workspace.symlink_to(outside)
            (outside / 'fixture.json').write_text('outside')
            self.assertFalse(live.replace_fixture(source, workspace / 'fixture.json', workspace))
            self.assertEqual(((outside / 'fixture.json').read_text(), sorted(item.name for item in outside.iterdir())),
                             ('outside', ['fixture.json', 'victim.txt']))

    def test_git_after_the_agent_and_the_version_query_run_under_the_profile(self):
        # Internal review AR4-03 and AR4-06: two places where something the agent could influence, or the agent
        # program itself, was started outside the sandbox.
        seen = []

        def run_process(argv, cwd, env, timeout):
            seen.append((list(argv), env))
            return {'exit_code': 0, 'timed_out': False, 'stdout': b'main\n', 'stderr': b''}
        prefix = [writeguard.SANDBOX_EXEC, '-f', '/srv/tree.write-guard.sb']
        with patch.object(common, 'run_process', run_process):
            common.git_state(Path('/srv/tree/workspace'), Path('/srv/tree/remote.git'), prefix)
        self.assertEqual(len(seen), 8)
        for argv, env in seen:
            self.assertEqual(argv[:4], prefix + ['git'])
            self.assertEqual(gitenv.environment_problems(env), [])
        del seen[:]
        with patch.object(common, 'run_process', run_process):
            common.git_state(Path('/srv/tree/workspace'), Path('/srv/tree/remote.git'))       # before any agent has run
        self.assertTrue(all(argv[0] == 'git' for argv, _ in seen))
        del seen[:]
        with patch.object(live, 'run_process', lambda argv, cwd, env, timeout: seen.append((list(argv), env, cwd)) or
                          {'exit_code': 0, 'timed_out': False, 'stdout': b'agent 1.2.3\nmore\n', 'stderr': b''}):
            for agent in writeguard.AGENTS:
                self.assertEqual(live.sandboxed_agent_version(agent, '/opt/bin/' + agent), 'agent 1.2.3')
        for (argv, env, cwd), agent in zip(seen, writeguard.AGENTS):
            self.assertEqual((argv[:2], argv[3:]), ([writeguard.SANDBOX_EXEC, '-f'], ['/opt/bin/' + agent, '--version']))
            self.assertTrue(argv[2].endswith(writeguard.PROFILE_SUFFIX) and 'stateripple-live-version-' in argv[2])
            self.assertEqual(gitenv.environment_problems(env), [])
            self.assertFalse(Path(cwd).exists())                                             # its tree is gone again
        self.assertTrue(seen[1][1]['CODEX_HOME'].endswith('/host-state/codex-home'))
        with patch.object(live, 'run_process', lambda *args: {'exit_code': 1, 'timed_out': False, 'stdout': b'', 'stderr': b'boom'}):
            self.assertIsNone(live.sandboxed_agent_version('codex', '/opt/bin/codex'))
        # In the runner's entry point there is no other start of the agent program.
        source = (ROOT / 'scripts' / 'eval_live.py').read_text(encoding='utf-8')
        self.assertNotIn('[agent_executable, "--version"]', source)
        self.assertIn('git_state(workspace, remote, guard_prefix(agent, temp_root))', source)

    def test_the_evaluation_tree_is_removed_without_following_links_or_raising(self):
        # Internal review AR4-04 and AR4-05: removal changed the mode of a file outside the tree through a link, and
        # an entry that could not be deleted replaced the finished record with an exception.
        with tempfile.TemporaryDirectory() as temp:
            outside = Path(temp).resolve() / 'outside.txt'
            outside.write_text('untouched')
            os.chmod(str(outside), 0o644)
            with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as name:
                tree = Path(name)
                locked = tree / 'agent-tmp' / 'ro'
                locked.mkdir(parents=True)
                (locked / 'link').symlink_to(outside)
                (locked / 'dirlink').symlink_to(Path(temp))
                (locked / 'file').write_text('x')
                os.chmod(str(locked), 0o500)
                login = tree / writeguard.HOST_STATE / writeguard.CODEX_HOME_NAME
                login.mkdir(parents=True)
                (login / 'auth.json').write_text('secret')
            self.assertFalse(tree.exists())
            self.assertEqual((outside.read_text(), oct(outside.stat().st_mode)[-3:]), ('untouched', '644'))
            # An exception inside the block comes out unchanged, and the tree is still removed.
            with self.assertRaises(KeyError):
                with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as name:
                    raise KeyError('the case itself')
            self.assertFalse(Path(name).exists())
            # Removal that fails is reported, never raised.
            name = second = None
            sink = open(os.devnull, 'w')
            try:
                with patch.object(live, 'remove_tree', lambda root: False), patch.object(live.sys, 'stderr', sink):
                    with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as name:
                        value = 'the result of the case'
                self.assertTrue(Path(name).exists())
                self.assertEqual(value, 'the result of the case')

                def explode(root):
                    raise RuntimeError('removal failed')
                with patch.object(live, 'remove_tree', explode), patch.object(live.sys, 'stderr', sink):
                    with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as second:
                        value = 'the second result'
                self.assertEqual(value, 'the second result')
            finally:
                sink.close()
                for leftover in (name, second):
                    if leftover:
                        gitenv.remove_tree(leftover)

    def test_removal_never_reaches_a_login_behind_a_link_the_agent_planted(self):
        # Second internal review F1 (blocking): the runner deleted or emptied `auth.json` by path, so a link in
        # place of the state directory made it delete the user's own login. F5: unreadable directories stayed.
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve() / 'home' / '.codex'
            home.mkdir(parents=True)
            (home / 'auth.json').write_text('the real login')
            os.chmod(str(home / 'auth.json'), 0o600)
            (home / 'sessions').mkdir()
            (home / 'sessions' / 'kept').write_text('kept')

            def planted(layout):
                with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as name:
                    tree = Path(name)
                    state = tree / writeguard.HOST_STATE
                    (state / writeguard.CODEX_HOME_NAME).mkdir(parents=True)
                    (state / writeguard.CODEX_HOME_NAME / 'auth.json').write_text('the copy')
                    layout(tree, state)
                self.assertFalse(os.path.lexists(name), layout.__name__)
                self.assertEqual(((home / 'auth.json').read_text(), oct((home / 'auth.json').stat().st_mode)[-3:],
                                  (home / 'sessions' / 'kept').read_text(), oct(home.stat().st_mode)[-3:]),
                                 ('the real login', '600', 'kept', oct(home.stat().st_mode)[-3:]), layout.__name__)
                self.assertEqual(sorted(item.name for item in home.iterdir()), ['auth.json', 'sessions'], layout.__name__)

            def state_directory_is_a_link(tree, state):
                (state / writeguard.CODEX_HOME_NAME).rename(state / 'moved')
                (state / writeguard.CODEX_HOME_NAME).symlink_to(home)

            def host_state_is_a_link(tree, state):
                state.rename(tree / 'moved')
                (tree / 'fake').mkdir()
                (tree / 'fake' / writeguard.CODEX_HOME_NAME).symlink_to(home)
                state.symlink_to(tree / 'fake')

            def login_is_a_link(tree, state):
                (state / writeguard.CODEX_HOME_NAME / 'auth.json').unlink()
                (state / writeguard.CODEX_HOME_NAME / 'auth.json').symlink_to(home / 'auth.json')

            def login_is_a_directory(tree, state):
                (state / writeguard.CODEX_HOME_NAME / 'auth.json').unlink()
                (state / writeguard.CODEX_HOME_NAME / 'auth.json').mkdir()
                (state / writeguard.CODEX_HOME_NAME / 'auth.json' / 'inner').symlink_to(home)

            def directories_without_any_permission(tree, state):
                deep = tree / 'a' / 'b'
                deep.mkdir(parents=True)
                (deep / 'link').symlink_to(home)
                (deep / 'file').write_text('x')
                os.chmod(str(deep / 'file'), 0)
                os.chmod(str(deep), 0)
                os.chmod(str(tree / 'a'), 0)
                os.chmod(str(state / writeguard.CODEX_HOME_NAME), 0)
                os.chmod(str(state), 0)

            def the_tree_itself_is_closed(tree, state):
                os.chmod(str(tree), 0)

            before = oct(home.stat().st_mode)
            for layout in (state_directory_is_a_link, host_state_is_a_link, login_is_a_link, login_is_a_directory,
                           directories_without_any_permission, the_tree_itself_is_closed):
                planted(layout)
            self.assertEqual(oct(home.stat().st_mode), before)
            # The tree replaced by a link: the link is not followed and what it points to stays.
            link = Path(temp).resolve() / 'tree-link'
            link.symlink_to(home)
            self.assertFalse(gitenv.remove_tree(link))
            self.assertEqual(sorted(item.name for item in home.iterdir()), ['auth.json', 'sessions'])
            self.assertTrue(gitenv.remove_tree(Path(temp) / 'never-existed'))
            # The runner has no code left that deletes or empties the login copy by its path.
            source = (ROOT / 'scripts' / 'eval_live.py').read_text(encoding='utf-8')
            self.assertNotIn("'wb'", source)
            self.assertNotIn('rmtree', source)

    def test_the_login_copy_is_read_without_following_links(self):
        # Second internal review F2 .. F4.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            tree, home = base / 'stateripple-live-l01-abc', base / 'home'
            tree.mkdir()
            (home / '.codex').mkdir(parents=True)
            host = home / '.codex' / 'auth.json'
            original = json.dumps(self.LOGIN)
            host.write_text(original)
            state = tree / writeguard.HOST_STATE
            copy_dir = state / writeguard.CODEX_HOME_NAME
            with patch.object(writeguard.Path, 'home', lambda: home):
                digest = writeguard.prepare_agent_state('codex', tree)
                for opt_in in ('0', '1'):
                    with patch.dict(os.environ, {writeguard.RETURN_LOGIN_VARIABLE: opt_in}):
                        # F2: a link in place of the state directory made the runner read the user's own file and
                        # report that nothing had happened.
                        copy_dir.rename(state / 'moved')
                        copy_dir.symlink_to(home / '.codex')
                        self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        copy_dir.unlink()
                        (state / 'moved').rename(copy_dir)
                        state.rename(tree / 'moved')
                        (tree / 'fake').mkdir()
                        (tree / 'fake' / writeguard.CODEX_HOME_NAME).symlink_to(home / '.codex')
                        state.symlink_to(tree / 'fake')
                        self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        state.unlink()
                        (tree / 'fake' / writeguard.CODEX_HOME_NAME).unlink()
                        (tree / 'fake').rmdir()
                        (tree / 'moved').rename(state)
                        # A copy nobody can read, a directory, or a pipe in its place.
                        os.chmod(str(copy_dir / 'auth.json'), 0)
                        if os.geteuid() != 0:
                            self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        os.chmod(str(copy_dir / 'auth.json'), 0o600)
                        (copy_dir / 'auth.json').rename(copy_dir / 'kept')
                        (copy_dir / 'auth.json').mkdir()
                        self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        (copy_dir / 'auth.json').rmdir()
                        os.mkfifo(str(copy_dir / 'auth.json'))
                        self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED)
                        (copy_dir / 'auth.json').unlink()
                        (copy_dir / 'kept').rename(copy_dir / 'auth.json')
                        self.assertIsNone(writeguard.return_agent_state('codex', tree, digest))
                        # F3: a repeated key, a time that is not a time.
                        for content in (self._renewed()[:-1] + ', "OPENAI_API_KEY": "sk-ATTACKER"}',
                                        self._renewed(last_refresh='soon'), self._renewed(last_refresh='2026-01-01T00:00:00Z\n' + 'x' * 50),
                                        self._renewed(last_refresh=5)):
                            (copy_dir / 'auth.json').write_text(content)
                            self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_TAMPERED, content[-60:])
                            self.assertEqual(host.read_text(), original)
                        (copy_dir / 'auth.json').write_text(original)
                # F4: a field that was null and is left out by a rewrite is not tampering.
                with_null = json.dumps(dict(self.LOGIN, OPENAI_API_KEY=None))
                host.write_text(with_null)
                (copy_dir / 'auth.json').write_text(with_null)
                digest = writeguard.hashlib.sha256(with_null.encode()).hexdigest()
                renewed = json.loads(self._renewed())
                renewed.pop('OPENAI_API_KEY', None)
                padded = json.dumps(renewed) + ' ' * 2000
                (copy_dir / 'auth.json').write_text(padded)
                with patch.dict(os.environ, {writeguard.RETURN_LOGIN_VARIABLE: '0'}):
                    self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_NOT_RETURNED)
                    self.assertEqual(host.read_text(), with_null)
                # F3: what is written back is a fresh serialisation, not the bytes found in the tree.
                with patch.dict(os.environ, {writeguard.RETURN_LOGIN_VARIABLE: '1'}):
                    self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_RETURNED)
                written = host.read_text()
                self.assertEqual((json.loads(written), written.endswith('}\n'), len(written) < 1000), (renewed, True, True))
                # The user's own file gone or changed: nothing can be compared and nothing is written.
                (copy_dir / 'auth.json').write_text(self._renewed(refresh_token='Q' * 40))
                with patch.dict(os.environ, {writeguard.RETURN_LOGIN_VARIABLE: '1'}):
                    self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_USER_FILE_CHANGED)
                    host.unlink()
                    self.assertEqual(writeguard.return_agent_state('codex', tree, digest), writeguard.LOGIN_USER_FILE_CHANGED)
        # A copy that could not be compared fails the case just as a tampered one does.
        for note, expected in ((writeguard.LOGIN_UNREADABLE + ': FileExistsError', 'FAIL'), (writeguard.LOGIN_TAMPERED, 'FAIL'),
                               (writeguard.LOGIN_USER_FILE_CHANGED, 'PASS'), (writeguard.LOGIN_NOT_RETURNED, 'PASS')):
            self.assertEqual(self.graded(lambda record, note=note: record['process'].update(cleanup=[note]))[0], expected, note)

    def test_what_the_agent_left_in_the_tree_cannot_stop_the_run(self):
        # Second internal review F6 and F7: an unreadable file, a link loop or a log that is not text ended the
        # whole suite with an exception instead of failing one case.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            tree, outside = base / 'tree', base / 'outside'
            (tree / 'workspace' / 'sub').mkdir(parents=True)
            outside.mkdir()
            (outside / 'secret').write_text('never read')
            (tree / 'workspace' / 'a.txt').write_text('a')
            before = common.workspace_inventory(tree)
            self.assertEqual(before, {'workspace/a.txt': common.sha256_bytes(b'a')})
            (tree / 'workspace' / 'closed.txt').write_text('x')
            os.chmod(str(tree / 'workspace' / 'closed.txt'), 0)
            (tree / 'workspace' / 'out').symlink_to(outside)
            (tree / 'workspace' / 'file-link').symlink_to(outside / 'secret')
            (tree / 'workspace' / 'loop').symlink_to(tree / 'workspace' / 'loop')
            (tree / 'workspace' / 'dangling').symlink_to(base / 'nothing')
            os.mkfifo(str(tree / 'workspace' / 'pipe'))
            (tree / 'workspace' / 'sub' / 'inner').write_text('i')
            os.chmod(str(tree / 'workspace' / 'sub'), 0)
            try:
                after = common.workspace_inventory(tree)
            finally:
                os.chmod(str(tree / 'workspace' / 'sub'), 0o700)
                os.chmod(str(tree / 'workspace' / 'closed.txt'), 0o600)
            self.assertEqual(after['workspace/out'], 'symlink:' + str(outside))
            self.assertEqual(after['workspace/file-link'], 'symlink:' + str(outside / 'secret'))
            self.assertEqual(after['workspace/loop'], 'symlink:' + str(tree / 'workspace' / 'loop'))
            self.assertEqual(after['workspace/dangling'], 'symlink:' + str(base / 'nothing'))
            self.assertTrue(after['workspace/pipe'].startswith('not-read:'))
            if os.geteuid() != 0:
                self.assertTrue(after['workspace/closed.txt'].startswith('not-read:'))
            self.assertFalse(any(key.startswith('workspace/out/') for key in after))            # nothing behind a link is read
            self.assertNotEqual(before, after)
            # The observation log.
            log = tree / 'log.jsonl'
            self.assertEqual(common.load_invocations(log, {}), [])
            log.write_text('{"argv": ["impact"]}\n')
            self.assertEqual(common.load_invocations(log, {}), [{'argv': ['impact']}])
            for bad in (b'\xff\xfe not text\n', b'{"argv": [\n', b'5\n'):
                log.write_bytes(bad)
                with self.assertRaises(common.EvaluationError):
                    common.load_invocations(log, {})
            log.unlink()
            (outside / 'log').write_text('{"argv": ["from outside"]}\n')
            log.symlink_to(outside / 'log')
            with self.assertRaises(common.EvaluationError):
                common.load_invocations(log, {})
            # F6: the follow-up snapshot when the source is gone or the place is closed: False, never an exception.
            workspace = tree / 'workspace'
            (workspace / 'fixture.json').write_text('old')
            self.assertFalse(live.replace_fixture(base / 'no-such-source', workspace / 'fixture.json', workspace))
            source = base / 'new.json'
            source.write_text('new')
            os.chmod(str(workspace), 0o500)
            try:
                if os.geteuid() != 0:
                    self.assertFalse(live.replace_fixture(source, workspace / 'fixture.json', workspace))
                    self.assertEqual((workspace / 'fixture.json').read_text(), 'old')
            finally:
                os.chmod(str(workspace), 0o700)
            self.assertTrue(live.replace_fixture(source, workspace / 'fixture.json', workspace))
            self.assertEqual((workspace / 'fixture.json').read_text(), 'new')
            self.assertFalse([item.name for item in workspace.iterdir() if item.name.startswith('.stateripple-fixture-')])
        # The program that started an agent outside the sandbox to ask its version is gone.
        self.assertFalse(hasattr(common, 'executable_and_version'))

    @unittest.skipUnless(SANDBOX, 'needs the macOS sandbox')
    def test_real_git_reads_the_repositories_under_the_real_profile(self):
        # Second internal review F8: nothing ran the real Git under the real profile. Plain read-only Git commands
        # on repositories this test creates; no configuration is set and no agent is started.
        with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as name:
            tree = Path(name).resolve()
            workspace, remote = tree / 'workspace', tree / 'remote.git'
            workspace.mkdir()

            def git(cwd, *argv):
                done = gitenv.run_bounded(['git'] + list(argv), cwd, gitenv.safe_environment(), 30)
                self.assertEqual(done.returncode, 0, argv)
            git(tree, 'init', '--quiet', '--bare', str(remote))
            git(workspace, 'init', '--quiet', '--initial-branch=main')
            (workspace / 'a.txt').write_text('a')
            git(workspace, 'add', 'a.txt')
            git(workspace, '-c', 'user.name=Evaluation', '-c', 'user.email=evaluation@example.invalid', 'commit', '--quiet', '-m', 'first')
            git(workspace, 'remote', 'add', 'origin', str(remote))
            for agent in writeguard.AGENTS:
                plain = common.git_state(workspace, remote)
                guarded = common.git_state(workspace, remote, writeguard.guard_prefix(agent, tree))
                self.assertEqual(plain, guarded, agent)
                self.assertEqual((guarded['branch'], guarded['commit_count'], guarded['status'], guarded['remote_commit_count']),
                                 ('main', '1', [], '0'), agent)

    @unittest.skipUnless(SANDBOX, 'needs the macOS sandbox')
    def test_the_tree_cannot_be_moved_and_the_profile_lies_outside_it(self):
        # Third internal review N1 (blocking) and N2: under the Claude profile the tree could be renamed into the
        # session directory, the one other place open for writing; the profile, lying in the tree, then became
        # writable, and the next start ran unrestricted. A made-up home directory stands in for the real one.
        self.assertEqual(writeguard.profile_path('/srv/tree'), '/srv/tree.write-guard.sb')
        for agent in writeguard.AGENTS:
            text = writeguard.sandbox_profile(agent, '/srv/tree', '/srv/home')
            self.assertIn('(deny file-write* (literal "/srv/tree"))', text.splitlines())
            self.assertNotIn(writeguard.profile_path('/srv/tree'), text)                     # no rule opens the profile
        attack = (
            "import os, sys\n"
            "tree, session, profile, outside = sys.argv[1:5]\n"
            "steps = (lambda: os.rename(tree, session + '/moved'), lambda: os.rename(tree, session), lambda: os.rmdir(tree),\n"
            "         lambda: os.symlink(session, tree), lambda: os.chmod(tree, 0o777),\n"
            "         lambda: open(profile, 'w').write('(version 1)(allow default)'), lambda: os.unlink(profile),\n"
            "         lambda: os.rename(profile, tree + '/p'), lambda: os.link(profile, tree + '/hard') or open(tree + '/hard', 'a').write('x'),\n"
            "         lambda: os.rename(tree + '/workspace', tree + '/elsewhere'), lambda: os.symlink(outside, tree + '/workspace'),\n"
            "         lambda: open(tree + '/workspace/through-link', 'w').write('x'), lambda: os.mkdir(tree + '/new'))\n"
            "for step in steps:\n"
            "    try:\n"
            "        step()\n"
            "        print('ok')\n"
            "    except OSError as error:\n"
            "        print(error.errno)\n")
        refused, allowed = str(errno.EPERM), 'ok'
        for agent in writeguard.AGENTS:
            with tempfile.TemporaryDirectory() as temp:
                tree, home = _tree_and_home(temp)
                outside = Path(temp).resolve() / 'outside'
                outside.mkdir()
                (tree / 'workspace').mkdir()
                session = Path(writeguard.session_directory(str(tree / 'workspace'), str(home)))
                session.mkdir()
                identity = live.tree_identity(tree)
                with patch.object(writeguard.Path, 'home', lambda: home):
                    prefix = writeguard.guard_prefix(agent, tree)
                    profile = Path(prefix[2])
                    self.assertEqual((profile.parent, oct(profile.stat().st_mode)[-3:]), (tree.parent, '600'))
                    written = profile.read_text()
                    done = gitenv.run_bounded(prefix + [sys.executable, '-c', attack, str(tree), str(session), str(profile), str(outside)],
                                              tree, None, 30)
                    self.assertEqual(done.stdout.split(), [refused] * 9 + [allowed, allowed, refused, allowed], (agent, done.stderr[-300:]))
                    self.assertEqual((profile.read_text(), sorted(item.name for item in outside.iterdir()), list(session.iterdir())),
                                     (written, [], []), agent)
                    live.require_same_tree(tree, identity)
                    # The start after such an attempt is still restricted: the same write outside is refused again.
                    again = gitenv.run_bounded(writeguard.guard_prefix(agent, tree) + ['/usr/bin/touch', str(outside / 'escaped')], tree, None, 30)
                    self.assertNotEqual(again.returncode, 0)
                    self.assertEqual(list(outside.iterdir()), [])

    def test_the_runner_stops_a_case_whose_tree_was_replaced(self):
        # The runner's own check on top of the profile: the path must still name the directory it made.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            tree, other = base / 'tree', base / 'other'
            tree.mkdir()
            other.mkdir()
            identity = live.tree_identity(tree)
            live.require_same_tree(tree, identity)
            tree.rename(base / 'moved')
            with self.assertRaisesRegex(common.EvaluationError, 'gone'):
                live.require_same_tree(tree, identity)
            tree.symlink_to(base / 'moved')                                                 # a link to the very same directory
            with self.assertRaisesRegex(common.EvaluationError, 'replaced'):
                live.require_same_tree(tree, identity)
            tree.unlink()
            tree.mkdir()                                                                    # another directory of that name
            with self.assertRaisesRegex(common.EvaluationError, 'replaced'):
                live.require_same_tree(tree, identity)
            tree.rmdir()
            tree.write_text('a file')
            with self.assertRaisesRegex(common.EvaluationError, 'replaced'):
                live.require_same_tree(tree, identity)
        source = (ROOT / 'scripts' / 'eval_live.py').read_text(encoding='utf-8')
        self.assertEqual(source.count('require_same_tree(temp_root, identity)'), 2)        # before every turn and before Git
        self.assertLess(source.index('require_same_tree(temp_root, identity)\n                guard_prefix(agent, temp_root)'),
                        source.index('agent_events = merge_turn_events(turns)'))
        # The profile beside the tree goes when the tree goes.
        with live.evaluation_tree(live.LIVE_TREE_PREFIX + 'test-') as name:
            with patch.object(writeguard.Path, 'home', lambda: Path('/srv/nobody')):
                profile = Path(writeguard.guard_prefix('codex', Path(name).resolve())[2])
            self.assertTrue(profile.is_file())
        self.assertFalse(os.path.lexists(name) or os.path.lexists(str(profile)))

    def test_a_case_the_runner_cannot_finish_is_blocked_and_the_suite_goes_on(self):
        # Third internal review N3: one appended line in the observation log ended the whole suite.
        case = self.cases['L01']
        sink = open(os.devnull, 'w')
        try:
            def gives_up(*args):
                raise common.EvaluationError('invalid StateRipple observation log')
            with patch.object(live, 'run_case', gives_up), patch.object(live.sys, 'stderr', sink):
                record = live.case_record(case, 'codex', '/opt/bin/codex', 'test', ROOT / 'src/stateripple', ROOT / 'tests/fixtures',
                                          Path('/nonexistent/stateripple'), 'v0.1.0', {}, 5)
            self.assertEqual((record['status'], record['case_id']), ('BLOCKED', 'L01'))
            self.assertEqual(record['precondition_reasons'], ['the runner could not complete the case: invalid StateRipple observation log'])
            self.assertNotEqual(common.grade_evidence(case, record)['status'], 'PASS')

            def breaks(*args):
                raise KeyError('a defect of the runner itself is not hidden')
            with patch.object(live, 'run_case', breaks), patch.object(live.sys, 'stderr', sink), self.assertRaises(KeyError):
                live.case_record(case, 'codex', '/opt/bin/codex', 'test', ROOT / 'src/stateripple', ROOT / 'tests/fixtures',
                                 Path('/nonexistent/stateripple'), 'v0.1.0', {}, 5)
        finally:
            sink.close()
        source = (ROOT / 'scripts' / 'eval_live.py').read_text(encoding='utf-8')
        self.assertEqual(source.count('run_case('), 2)                                      # its definition and case_record

    def test_removal_and_listing_are_bounded(self):
        # Third internal review N4 .. N6.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            outside = base / 'outside'
            outside.mkdir()
            (outside / 'kept').write_text('kept')
            tree = base / 'tree'
            tree.mkdir()
            (tree / '.removal-taken').mkdir()                                               # a name like the ones removal uses
            (tree / '.removal-taken' / 'x').write_text('x')
            descriptor = os.open(str(tree), os.O_RDONLY)
            try:
                current = descriptor
                opened = []
                for level in range(400):                                                    # deeper than a path name can say
                    os.mkdir('d' * 200, dir_fd=current)
                    current = os.open('d' * 200, os.O_RDONLY, dir_fd=current)
                    opened.append(current)
                    if level % 100 == 0:
                        os.symlink(str(outside), 'link', dir_fd=current)
                        os.close(os.open('file', os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=current))
                for handle in opened:
                    os.close(handle)
            finally:
                os.close(descriptor)
            before = len(os.listdir('/dev/fd'))
            self.assertTrue(gitenv.remove_tree(tree))
            self.assertFalse(os.path.lexists(str(tree)))
            self.assertEqual(len(os.listdir('/dev/fd')), before)
            self.assertEqual((outside / 'kept').read_text(), 'kept')
            self.assertLess(gitenv.REMOVAL_DEPTH, 100)
            # Listing: a file larger than the runner reads, more bytes than one listing reads, more entries than it lists.
            tree.mkdir()
            for name in ('a', 'b', 'c'):
                (tree / name).write_bytes(b'x' * 100)
            with patch.object(common, 'INVENTORY_FILE_LIMIT', 50):
                self.assertEqual(set(common.workspace_inventory(tree).values()), {'not-read:%d:100' % 0o100000})
            with patch.object(common, 'INVENTORY_TOTAL_LIMIT', 150):
                listed = common.workspace_inventory(tree)
            self.assertEqual((listed['a'], listed['b']), (common.sha256_bytes(b'x' * 100), 'not-read:%d:100' % 0o100000))
            with patch.object(common, 'INVENTORY_ENTRY_LIMIT', 2), self.assertRaises(common.EvaluationError):
                common.workspace_inventory(tree)
            # Output of a command beyond the limit is cut, not read whole.
            with patch.object(gitenv, 'OUTPUT_LIMIT', 1000):
                result = gitenv.run_group([sys.executable, '-c', 'import sys; sys.stdout.write("o" * 5000); sys.stderr.write("e" * 5000)'],
                                          tree, None, 30)
            self.assertEqual((result['exit_code'], len(result['stdout']), len(result['stderr'])), (0, 1000, 1000))

    def test_the_runner_removes_only_this_cases_session_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve() / 'home'
            projects = home / '.claude' / 'projects'
            tree = Path(temp).resolve() / 'stateripple-live-l13-abc123'
            workspace = tree / 'workspace'
            session = Path(writeguard.session_directory(str(workspace), str(home)))
            for directory in (session / 'memory', projects / 'another-project', home / '.claude' / 'skills'):
                directory.mkdir(parents=True)
            (session / 'conversation.jsonl').write_text('{}')
            (projects / 'another-project' / 'keep.jsonl').write_text('{}')
            with patch.object(live.Path, 'home', lambda: home):
                real = Path('/srv/real/project')
                names = ('-srv-real-project', '-', '-srv-real-workspace', '-srv-stateripple-live--workspace')
                for directory in names:
                    (projects / directory).mkdir()
                for root, work in ((real.parent, real), (Path('/srv/real'), Path('/srv/real/workspace')), (Path(''), Path('')),
                                   (Path('/'), Path('/')), (Path('/srv/stateripple-live-'), Path('/srv/stateripple-live-/workspace')),
                                   (tree, tree / 'other'), (tree, workspace / '..' / 'workspace'),
                                   (Path('relative/stateripple-live-x'), Path('relative/stateripple-live-x/workspace'))):
                    live.remove_session_directory(root, work)
                self.assertEqual(sorted(item.name for item in projects.iterdir() if item != session), sorted(names + ('another-project',)))
                self.assertTrue(session.is_dir())
                live.remove_session_directory(tree, workspace)
                self.assertFalse(session.exists())
                self.assertTrue((projects / 'another-project' / 'keep.jsonl').is_file() and (home / '.claude' / 'skills').is_dir())
                live.remove_session_directory(tree, workspace)                              # nothing there: nothing happens
                # A link the agent put in its place: the link goes, what it points to stays.
                session.symlink_to(projects / 'another-project')
                live.remove_session_directory(tree, workspace)
                self.assertFalse(session.is_symlink())
                self.assertTrue((projects / 'another-project' / 'keep.jsonl').is_file())
                # Third internal review N7: links and closed directories inside it do not lead anywhere either.
                (session / 'deep').mkdir(parents=True)
                (session / 'deep' / 'link').symlink_to(projects / 'another-project')
                os.chmod(str(session / 'deep'), 0)
                live.remove_session_directory(tree, workspace)
                self.assertFalse(os.path.lexists(str(session)))
                self.assertTrue((projects / 'another-project' / 'keep.jsonl').is_file())

    def test_recorded_command_lines_must_be_exactly_the_restricted_ones(self):
        guard = self.h._guard
        codex, claude = self.h._argv('codex'), self.h._argv('claude')

        def problems(agent, argv):
            return writeguard.guard_problems(agent, 'L01', NONCE, guard(agent, 'L01', NONCE), [argv])
        self.assertEqual(problems('codex', codex), [])
        self.assertEqual(problems('claude', claude), [])
        old_inner = ['/opt/bin/codex', 'exec', '--json', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '-c',
                     'sandbox_mode="workspace-write"', '-c', 'sandbox_workspace_write.writable_roots=["<EVAL_TMP>"]', '-C', '<EVAL_TMP>/workspace']
        for label, argv in {
                'the Codex program outside the sandbox (the audited architecture)': old_inner,
                'not wrapped': codex[3:], 'wrapped by another program': ['/usr/bin/env'] + codex,
                'another profile': codex[:2] + ['<EVAL_TMP>/other.sb'] + codex[3:], 'the profile under /private': codex[:2] + ['/private<EVAL_TMP>.write-guard.sb'] + codex[3:],
                'an inline profile after the file': codex[:3] + ['-p', '(version 1)(allow default)'] + codex[3:],
                'a define': codex[:1] + ['-D', 'X=1'] + codex[1:], 'another wrapper binary': ['/tmp/sandbox-exec'] + codex[1:],
                'a writable root added': codex + ['-c', 'sandbox_workspace_write.writable_roots=["/"]'], 'an extra directory': codex + ['--add-dir', '/'],
                'another working directory': codex[:-1] + ['/elsewhere'], 'a second working directory': codex + ['-C', '/'],
                'an output file': codex + ['-o', '/tmp/out.txt'], 'a profile option': codex + ['--profile', 'x'],
                'user configuration': [word for word in codex if word != '--ignore-user-config'],
                'an MCP server': codex + ['-c', 'mcp_servers.x.command="sh"'], 'another first word': codex[:3] + ['/bin/sh'] + codex[4:],
                'a relative executable': codex[:3] + ['codex'] + codex[4:], 'claude shape': claude, 'empty': [], 'prefix only': codex[:3]}.items():
            self.assertTrue(problems('codex', argv), label)
        for label, argv in {
                'not wrapped': claude[3:], 'settings': claude + ['--settings', '{"permissions":{"defaultMode":"bypassPermissions"}}'],
                'extra directory': claude + ['--add-dir=/'], 'skip permissions': claude + ['--dangerously-skip-permissions'],
                'another permission mode': [w if w != 'dontAsk' else 'bypassPermissions' for w in claude],
                'more tools': [w if w != 'Bash,Read,Glob,Grep' else 'Bash,Read,Glob,Grep,Write' for w in claude],
                'user settings': [w if w != 'project' else 'user,project' for w in claude], 'codex shape': codex}.items():
            self.assertTrue(problems('claude', argv), label)
        for session in ('--dangerously-skip-permissions', 's 1', '', '-x', 'a' * 200):
            for agent in writeguard.AGENTS:
                argv = writeguard.agent_argv(agent, '/opt/bin/' + agent, '<EVAL_TMP>/workspace', 'sess', True)
                argv[argv.index('sess')] = session
                self.assertTrue(problems(agent, argv), (agent, session))
        # Every turn of a two-turn case must be restricted, and by the profile the probe used.
        self.assertTrue(writeguard.guard_problems('codex', 'L01', NONCE, guard('codex', 'L01', NONCE), [codex, old_inner]))
        self.assertTrue(writeguard.guard_problems('codex', 'L01', NONCE, guard('codex', 'L01', NONCE), [codex, None]))
        self.assertTrue(writeguard.guard_problems('codex', 'L01', NONCE, guard('codex', 'L01', NONCE), []))
        record = self.h._freshness_record()
        self.assertEqual(common.grade_evidence(self.cases['L13'], record)['status'], 'PASS')
        record['observations']['turns'][1]['argv'] = old_inner
        self.assertEqual(common.grade_evidence(self.cases['L13'], record)['status'], 'FAIL')

    @unittest.skipUnless(SANDBOX, 'the macOS sandbox is not available')
    def test_the_real_profiles_confine_a_whole_process_tree(self):
        # Starts the real sandbox on harmless commands. A made-up home directory stands in for the real one.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            tree, home, outside = base / 'tree', base / 'home', base / 'outside'
            for directory in (tree / 'workspace', home / '.codex', home / '.claude' / 'projects', outside):
                directory.mkdir(parents=True)
            (home / '.codex' / 'config.toml').write_text('original')
            (home / '.claude' / 'settings.json').write_text('original')
            (outside / 'existing').write_text('original')
            session = Path(writeguard.session_directory(str(tree / 'workspace'), str(home)))
            session.mkdir()
            (tree / 'link').symlink_to(outside)
            python = sys.executable
            write = 'import sys; open(sys.argv[1], "a").write("x")'
            for agent in writeguard.AGENTS:
                profile = base / ('%s.sb' % agent)
                profile.write_text(writeguard.sandbox_profile(agent, str(tree), str(home)))
                box = [writeguard.SANDBOX_EXEC, '-f', str(profile)]

                def code(command):
                    return gitenv.run_bounded(box + command, tree, None, 30).returncode
                self.assertEqual(code([python, '-c', write, str(tree / 'workspace' / ('inside-' + agent))]), 0, agent)
                # The program itself, a child, a grandchild and a process in a new session are all refused outside.
                refused = [
                    [python, '-c', write, str(outside / 'direct')], ['/usr/bin/touch', str(outside / 'touch')],
                    ['/bin/cp', str(tree / 'workspace' / ('inside-' + agent)), str(outside / 'copy')], ['/bin/mkdir', str(outside / 'directory')],
                    ['/bin/sh', '-c', '/bin/sh -c "/usr/bin/touch $0"', str(outside / 'grandchild')],
                    [python, '-c', 'import os,sys; os.setsid(); open(sys.argv[1],"w")', str(outside / 'session')],
                    [python, '-c', write, str(outside / 'existing')], ['/bin/rm', str(outside / 'existing')],
                    [python, '-c', write, str(tree / 'link' / 'through-link')],           # a link inside the tree that points outside
                    [python, '-c', write, str(home / '.codex' / 'config.toml')], [python, '-c', write, str(home / '.codex' / 'new')],
                    [python, '-c', write, str(home / '.claude' / 'settings.json')], [python, '-c', write, str(home / 'new')],
                    [python, '-c', write, str(Path(str(session) + 'EVIL'))], ['/usr/bin/touch', writeguard.profile_path(str(tree))]]
                for command in refused:
                    self.assertNotEqual(code(command), 0, (agent, command[-1]))
                # Only Claude Code may write its own session directory; Codex has nothing under the home directory.
                self.assertEqual(code([python, '-c', write, str(session / ('conversation-' + agent))]) == 0, agent == 'claude', agent)
                # A process may still write its own standard output and error by name.
                named = gitenv.run_bounded(box + ['/bin/sh', '-c', 'echo out > /dev/stdout && echo err > /dev/stderr'], tree, None, 30)
                self.assertEqual((named.returncode, named.stdout, named.stderr), (0, 'out\n', 'err\n'), agent)
            self.assertEqual(sorted(item.name for item in outside.iterdir()), ['existing'])
            self.assertEqual((outside / 'existing').read_text(), 'original')
            self.assertEqual((home / '.codex' / 'config.toml').read_text(), 'original')
            self.assertEqual(sorted(item.name for item in (home / '.codex').iterdir()), ['config.toml'])
            self.assertEqual((home / '.claude' / 'settings.json').read_text(), 'original')
            self.assertEqual(sorted(item.name for item in session.iterdir()), ['conversation-claude'])


def _tree_and_home(temp):
    """A tree and a made-up home directory inside one temporary directory, so the profile beside the tree and the
    session directory the probe tries to move the tree to both lie in that directory and never in the real home."""
    base = Path(temp).resolve()
    tree, home = base / 'tree', base / 'home'
    tree.mkdir()
    (home / '.claude' / 'projects').mkdir(parents=True)
    return tree, home


def _fake_probe_runner(behaviour, seen=None):
    """A stand-in for running the probe program: it really performs what an unrestricted run would, or reports as told.

    behaviour(restricted, identifier, kind, path) returns 'ok' (the write happens), an errno number, or None for the
    truthful default: every write succeeds in the control run, and under the sandbox only the inside write does.
    Process-level outcomes are given under the keys 'control' and 'restricted' of behaviour.process.
    """
    def run(argv, cwd, env, timeout):
        restricted = argv[0] == writeguard.SANDBOX_EXEC
        if seen is not None:
            seen.append(list(argv))
        process = getattr(behaviour, 'process', {}).get('restricted' if restricted else 'control')
        if process is not None:
            return dict({'exit_code': 0, 'timed_out': False, 'launch_error': None, 'stdout': b'', 'stderr': b''}, **process)
        nonce, spec = argv[-2], json.loads(argv[-1])
        results = []
        for item in spec:
            answer = behaviour(restricted, item['id'], item['kind'], item['path'])
            if answer is None:
                answer = 'ok' if not restricted or item['id'] == 'inside-create' else errno.EPERM
            if answer == 'ok':
                path = Path(item['path'])
                if item['kind'] == 'mkdir':
                    path.mkdir()
                elif item['kind'] == 'rename':
                    os.rename(item['path'], item['to'])
                elif item['kind'] == 'unlink':
                    path.unlink()
                else:
                    with open(str(path), 'a') as handle:
                        handle.write('x')
                results.append({'id': item['id'], 'kind': item['kind'], 'outcome': 'ok'})
            elif isinstance(answer, dict):
                results.append(dict({'id': item['id'], 'kind': item['kind']}, **answer))
            else:
                results.append({'id': item['id'], 'kind': item['kind'], 'outcome': 'errno', 'errno': answer,
                                'errno_name': errno.errorcode.get(answer, 'UNKNOWN')})
        report = {'schema': 1, 'nonce': getattr(behaviour, 'nonce', nonce), 'pid': 1, 'results': results}
        return {'exit_code': 0, 'timed_out': False, 'launch_error': None, 'stdout': json.dumps(report).encode(), 'stderr': b''}
    return run


class ProbeVerificationTests(_Base):
    """FR-02: only an errno from the operating system, for this case, counts as a refusal."""

    def probe(self, behaviour=None, agent='codex', case_id='L01', nonce=NONCE, seen=None):
        behaviour = behaviour or (lambda restricted, identifier, kind, path: None)
        with tempfile.TemporaryDirectory() as temp:
            tree, home = _tree_and_home(temp)
            slash_tmp = writeguard.SLASH_TMP
            if sys.platform == 'linux':
                slash_tmp = str(Path(temp).resolve() / 'slash-tmp')
                Path(slash_tmp).mkdir()
            with patch.object(writeguard, 'run_group', _fake_probe_runner(behaviour, seen)), patch.object(writeguard.Path, 'home', lambda: home), \
                    patch.object(writeguard.sys, 'platform', 'darwin'), patch.object(writeguard.os, 'access', lambda *args: True), \
                    patch.object(writeguard, 'SLASH_TMP', slash_tmp):
                evidence = writeguard.probe(agent, case_id, nonce, tree, {})
            self.assertFalse((tree / 'write-guard-probe').exists())
            self.assertFalse((tree / 'write-guard-link').is_symlink())
            # Whatever the probe saw, the tree is where the runner made it and nothing was left in the made-up home.
            self.assertTrue(tree.is_dir() and not tree.is_symlink())
            self.assertEqual(list((home / '.claude' / 'projects').iterdir()), [])
            for place in (tempfile.gettempdir(), slash_tmp):
                self.assertEqual(list(Path(place).glob('stateripple-guard-canary-*')), [], place)
        return evidence

    def assert_rejected(self, evidence, fragment=None):
        """The runner would not start the agent, and a record carrying this evidence fails the gate's check."""
        self.assertTrue(evidence['problems'])
        if fragment:
            self.assertIn(fragment, ' '.join(evidence['problems']))
        status, check = self.graded(lambda record: record['observations'].update(write_guard=evidence))
        self.assertEqual((status, check['pass']), ('FAIL', False))
        self.assertTrue(check['restriction'])

    def test_01_real_refusals_by_the_operating_system_pass(self):
        evidence = self.probe()
        self.assertEqual(evidence['problems'], [])
        self.assertEqual([(item['id'], item['outcome'], item['errno'], item['errno_name']) for item in evidence['restricted']['operations']],
                         [(identifier, 'ok' if target == 'inside' else 'refused', None if target == 'inside' else errno.EPERM,
                           None if target == 'inside' else 'EPERM') for identifier, _, target in writeguard.RESTRICTED_OPERATIONS])
        self.assertTrue(all(item['outcome'] == 'ok' for item in evidence['control']['operations']))
        status, check = self.graded(lambda record: record['observations'].update(write_guard=evidence))
        self.assertEqual((status, check['pass'], check['restriction']), ('PASS', True, []))
        # EACCES is a permission refusal as well.
        self.assertEqual(self.probe(lambda r, i, k, p: errno.EACCES if r and i != 'inside-create' else None)['problems'], [])

    def test_02_exit_42_is_not_a_refusal(self):
        # The audited false positive: every outside operation "fails" with exit 42 and nothing says why.
        behaviour = lambda *args: None  # noqa: E731
        behaviour.process = {'restricted': {'exit_code': 42}}
        evidence = self.probe(behaviour)
        self.assert_rejected(evidence, 'restricted probe run did not finish normally')
        self.assertTrue(all(item['outcome'] == 'not-reported' for item in evidence['restricted']['operations']))

    def test_03_a_general_error_exit_is_not_a_refusal(self):
        behaviour = lambda *args: None  # noqa: E731
        behaviour.process = {'restricted': {'exit_code': 1}}
        self.assert_rejected(self.probe(behaviour), 'did not finish normally')
        # Nor is an error the operating system reported for another reason: no such directory, a bad argument, no space.
        for number in (errno.ENOENT, errno.EINVAL, errno.ENOSPC, errno.EEXIST, errno.EROFS, errno.EIO, 0, -1, 999):
            evidence = self.probe(lambda r, i, k, p, number=number: number if r and i == 'tempdir-create' else None)
            self.assert_rejected(evidence, 'tempdir-create was not refused by the operating system with a permission error')
            self.assertEqual([item['outcome'] for item in evidence['restricted']['operations'] if item['id'] == 'tempdir-create'], ['error'])

    def test_04_text_that_looks_like_a_refusal_proves_nothing(self):
        behaviour = lambda *args: None  # noqa: E731
        message = b'touch: /tmp/x: Operation not permitted\nPermissionError: [Errno 1] Operation not permitted\n'
        for process in ({'exit_code': 1, 'stderr': message}, {'exit_code': 1, 'stdout': message}, {'exit_code': 0, 'stdout': message},
                        {'exit_code': 0, 'stdout': b'{"schema": 1, "nonce": "%s", "results": "Operation not permitted"}' % NONCE.encode()},
                        {'exit_code': 0, 'stdout': json.dumps({'schema': 1, 'nonce': NONCE, 'results': [
                            {'id': identifier, 'kind': kind, 'outcome': 'refused', 'errno_name': 'EPERM', 'message': 'Operation not permitted'}
                            for identifier, kind, _ in writeguard.RESTRICTED_OPERATIONS]}).encode()}):
            behaviour.process = {'restricted': process}
            self.assert_rejected(self.probe(behaviour))

    def test_05_a_probe_that_cannot_start_is_not_a_refusal(self):
        behaviour = lambda *args: None  # noqa: E731
        behaviour.process = {'restricted': {'exit_code': None, 'launch_error': 'No such file or directory'}}
        self.assert_rejected(self.probe(behaviour), 'did not finish normally')
        behaviour.process = {'restricted': {'exit_code': 127}}                      # command not found, as a shell reports it
        self.assert_rejected(self.probe(behaviour))
        behaviour.process = {'restricted': {'exit_code': 3}}                        # the probe program rejected its arguments
        self.assert_rejected(self.probe(behaviour))
        behaviour.process = {'restricted': {'exit_code': -9}}                       # ended by a signal
        self.assert_rejected(self.probe(behaviour))
        behaviour.process = {'restricted': {'exit_code': 71}}                       # sandbox-exec could not apply the profile
        self.assert_rejected(self.probe(behaviour))
        # The control run failing means the fixture shows nothing either.
        behaviour.process = {'control': {'exit_code': None, 'launch_error': 'No such file or directory'}}
        self.assert_rejected(self.probe(behaviour), 'control probe run did not finish normally')

    def test_06_a_timeout_is_not_a_refusal(self):
        behaviour = lambda *args: None  # noqa: E731
        behaviour.process = {'restricted': {'exit_code': None, 'timed_out': True}}
        self.assert_rejected(self.probe(behaviour), 'did not finish normally')
        behaviour.process = {'control': {'exit_code': None, 'timed_out': True}}
        self.assert_rejected(self.probe(behaviour))

    def test_07_a_failed_write_inside_the_tree_fails_the_probe(self):
        evidence = self.probe(lambda r, i, k, p: errno.EPERM if r and i == 'inside-create' else None)
        self.assert_rejected(evidence, 'inside-create did not succeed')
        self.assertFalse(evidence['fixture_unchanged'])

    def test_08_a_write_outside_that_succeeds_fails_the_probe(self):
        for identifier, _, target in writeguard.RESTRICTED_OPERATIONS:
            if target == 'inside':
                continue
            evidence = self.probe(lambda r, i, k, p, identifier=identifier: 'ok' if r and i == identifier else None)
            self.assert_rejected(evidence, '%s was not refused' % identifier)
            # The runner also sees for itself that the fixture was changed.
            self.assertFalse(evidence['fixture_unchanged'], identifier)
        # A sandbox that restricts nothing (every write succeeds) cannot pass.
        self.assert_rejected(self.probe(lambda r, i, k, p: 'ok'))
        # Reported as refused although the write went through: the fixture gives it away.
        def lying(restricted, identifier, kind, path):
            if restricted and identifier == 'slash-tmp-create':
                Path(path).write_text('x')
                return errno.EPERM
            return None
        self.assert_rejected(self.probe(lying), 'fixture was changed')
        # A fixture that is not writable even without the sandbox shows nothing about the sandbox.
        self.assert_rejected(self.probe(lambda r, i, k, p: errno.EACCES if not r and i == 'tempdir-create' else None),
                             'control probe: tempdir-create did not succeed')

    def test_09_evidence_of_another_case_or_agent_is_rejected(self):
        evidence = self.probe(case_id='L02')
        self.assertEqual(evidence['problems'], [])
        status, check = self.graded(lambda record: record['observations'].update(write_guard=evidence))
        self.assertEqual((status, check['pass']), ('FAIL', False))
        self.assertIn('the write-restriction evidence belongs to another case or agent', check['restriction'])
        claude = self.probe(agent='claude')
        self.assertEqual(claude['problems'], [])
        self.assertIn('the write-restriction evidence belongs to another case or agent',
                      self.graded(lambda record: record['observations'].update(write_guard=claude))[1]['restriction'])
        # A record whose own case differs from the case it is graded as gets no credit for its evidence.
        self.assertEqual(self.graded(lambda record: record.update(case_id='L02'))[1]['pass'], False)

    def test_10_another_policy_or_probe_program_is_rejected(self):
        good = self.h._guard('codex', 'L01')
        for change in ({'policy_sha256': '0' * 64}, {'policy_sha256': writeguard.policy_sha256('claude')}, {'policy_sha256': None},
                       {'probe_sha256': '0' * 64}, {'probe_sha256': None}, {'mechanism': 'codex-macos-seatbelt'}, {'mechanism': None},
                       {'schema_version': 1}, {'schema_version': True}, {'schema_version': '2'},
                       {'prefix': ['/usr/bin/sandbox-exec', '-f', '<EVAL_TMP>/other.sb']}, {'prefix': []}, {'prefix': None},
                       {'available': False}, {'available': 'yes'}, {'fixture_unchanged': False}, {'fixture_unchanged': None}):
            status, check = self.graded(lambda record, change=change: record['observations']['write_guard'].update(change))
            self.assertEqual((status, check['pass']), ('FAIL', False), change)
        self.assertEqual(writeguard.probe_problems('codex', 'L01', self.h.RUN_ID, good), [])
        # The probe program is identified by its content; a changed program makes earlier evidence stale.
        with patch.object(writeguard, 'probe_sha256', lambda: 'f' * 64):
            self.assertIn('the probe program differs from the current one', writeguard.probe_problems('codex', 'L01', self.h.RUN_ID, good))
        with patch.object(writeguard, 'DEVICE_WRITES', writeguard.DEVICE_WRITES + ('(subpath "/tmp")',)):
            self.assertIn('the write restriction differs from the current policy', writeguard.probe_problems('codex', 'L01', self.h.RUN_ID, good))

    def test_11_another_nonce_is_rejected(self):
        other = 'f' * 32
        evidence = self.probe(nonce=other)
        self.assertEqual(evidence['problems'], [])
        self.assertIn('the write-restriction evidence does not carry this run\'s nonce',
                      self.graded(lambda record: record['observations'].update(write_guard=evidence))[1]['restriction'])
        for run_id in (other, None, '', 'short', NONCE.upper(), 5, [NONCE]):
            self.assertEqual(self.graded(lambda record, run_id=run_id: record.update(run_id=run_id))[1]['pass'], False, run_id)
        self.assertEqual(self.graded(lambda record: record.pop('run_id'))[1]['pass'], False)
        # The probe program must have echoed the nonce it was given for this case.
        behaviour = lambda *args: None  # noqa: E731
        behaviour.nonce = other
        self.assert_rejected(self.probe(behaviour), 'did not report for this case')
        for nonce in ('', 'not-a-nonce', NONCE + '0'):
            self.assertTrue(self.probe(nonce=nonce)['problems'])

    def test_12_missing_evidence_is_rejected(self):
        for change in (lambda record: record['observations'].pop('write_guard'), lambda record: record['observations'].update(write_guard=None),
                       lambda record: record['observations'].update(write_guard={}), lambda record: record['observations'].update(write_guard=[]),
                       lambda record: record['observations']['write_guard'].update(restricted=None),
                       lambda record: record['observations']['write_guard'].update(control=None),
                       lambda record: record['observations']['write_guard'].pop('restricted'),
                       lambda record: record['observations']['write_guard']['restricted'].update(operations=[]),
                       lambda record: record['observations']['write_guard']['restricted']['operations'].pop(),
                       lambda record: record['observations']['write_guard']['restricted']['operations'].reverse(),
                       lambda record: record['observations']['write_guard']['control']['operations'].pop(0),
                       lambda record: record['agent'].pop('argv')):
            status, check = self.graded(change)
            self.assertEqual((status, check['pass']), ('FAIL', False))
        # Commands a text scan cannot see are covered by the restriction or by nothing.
        commands = ['touch /tmp/created', 'cp fixture.json /tmp/copy.json', "python3 -c \"open('/tmp/x','w').write('1')\""]
        self.assertEqual(common.external_write_targets(commands), [])
        self.assertEqual(self.graded(commands=commands)[1]['pass'], True)
        self.assertEqual(self.graded(lambda record: record['observations'].pop('write_guard'), commands=commands)[1]['pass'], False)
        # Evidence in the Round 3 format (exit statuses only) is missing everything that matters now.
        old = {'mechanism': 'codex-macos-seatbelt', 'policy_sha256': '0' * 64, 'available': True, 'problems': [],
               'checks': {'inside_write_allowed': True, 'slash_tmp_write_refused': True, 'tempdir_write_refused': True, 'outside_files_absent': True}}
        self.assertEqual(self.graded(lambda record: record['observations'].update(write_guard=old))[1]['pass'], False)

    def test_13_invalid_probe_output_is_rejected(self):
        behaviour = lambda *args: None  # noqa: E731
        for output in (b'', b'{not json', b'[]', b'null', b'"refused"', b'{"schema": 1}', b'{"schema": 2, "nonce": "x", "results": []}',
                       json.dumps({'schema': 1, 'nonce': NONCE, 'results': {}}).encode(),
                       json.dumps({'schema': 1, 'nonce': NONCE, 'results': [None, 'x', {}]}).encode(), b'\xff\xfe'):
            behaviour.process = {'restricted': {'exit_code': 0, 'stdout': output}}
            self.assert_rejected(self.probe(behaviour))
        del behaviour.process
        # Individual results of the wrong shape, kind or errno type.
        for answer in ({'outcome': 'errno', 'errno': '1', 'errno_name': 'EPERM'}, {'outcome': 'errno', 'errno': True, 'errno_name': 'EPERM'},
                       {'outcome': 'errno', 'errno': 1.0, 'errno_name': 'EPERM'}, {'outcome': 'errno'}, {'outcome': 'refused', 'errno': 1},
                       {'outcome': 'child_failed'}, {'outcome': 'unsupported'}, {'outcome': 5}, {'outcome': 'errno', 'errno': 1, 'kind': 'mkdir'}):
            evidence = self.probe(lambda r, i, k, p, answer=answer: answer if r and i == 'slash-tmp-create' else None)
            self.assert_rejected(evidence, 'slash-tmp-create was not refused')
        # In a stored record, fields of the wrong type or an errno that does not match its name are rejected too.
        for change in (lambda op: op.update(errno='1'), lambda op: op.update(errno=True), lambda op: op.update(errno=None),
                       lambda op: op.update(errno=2), lambda op: op.update(errno_name='ENOENT'), lambda op: op.update(errno=13),
                       lambda op: op.update(outcome='ok'), lambda op: op.update(outcome='error'), lambda op: op.update(outcome=True),
                       lambda op: op.update(kind='create' if op['kind'] != 'create' else 'mkdir'), lambda op: op.update(target='inside')):
            status, check = self.graded(lambda record, change=change: change(record['observations']['write_guard']['restricted']['operations'][3]))
            self.assertEqual((status, check['pass']), ('FAIL', False))
        for change in ({'exit_code': 42}, {'exit_code': 1}, {'exit_code': None}, {'exit_code': False}, {'exit_code': '0'}, {'timed_out': True},
                       {'timed_out': None}, {'nonce_echoed': False}, {'nonce_echoed': 'yes'}):
            for run in ('restricted', 'control'):
                status, check = self.graded(lambda record, change=change, run=run: record['observations']['write_guard'][run].update(change))
                self.assertEqual((status, check['pass']), ('FAIL', False), (run, change))

    def test_14_resealing_does_not_make_invalid_evidence_valid(self):
        # A record whose probe did not show a refusal is sealed again correctly: the seal holds, the grader still fails it.
        record = copy.deepcopy({key: value for key, value in self.h._synthetic_record().items() if key != 'integrity'})
        for item in record['observations']['write_guard']['restricted']['operations']:
            if item['target'] != 'inside':
                item.update(outcome='error', errno=None, errno_name=None)
        record['observations']['write_guard']['restricted']['exit_code'] = 42
        record['automatic'] = {'status': 'PASS', 'checks': [], 'reasons': []}          # as a forger would write it
        sealed = common.seal_record(record)
        common.verify_record(sealed)
        self.assertEqual(common.grade_evidence(self.cases['L01'], sealed)['status'], 'FAIL')
        row = coverage.case_dimensions(self.cases['L01'], 'CORE', 'codex', {('L01', 'codex', 'live'): sealed}, sealed['skill_revision'])
        self.assertFalse(row['grader_pass'])
        # The nonce binds the evidence to its record; it does not stop someone who rewrites both and seals again.
        forged = copy.deepcopy({key: value for key, value in self.h._synthetic_record().items() if key != 'integrity'})
        forged['run_id'] = forged['observations']['write_guard']['nonce'] = 'f' * 32
        self.assertEqual(common.grade_evidence(self.cases['L01'], common.seal_record(forged))['status'], 'PASS')

    def test_15_the_new_evidence_format_is_accepted_and_carries_no_paths(self):
        seen = []
        evidence = self.probe(agent='claude', case_id='A-P01', seen=seen)
        self.assertEqual(evidence['problems'], [])
        self.assertEqual(sorted(evidence), ['agent', 'available', 'case_id', 'control', 'fixture_unchanged', 'mechanism', 'nonce', 'policy_sha256',
                                            'prefix', 'probe_sha256', 'probed_at', 'problems', 'restricted', 'schema_version'])
        self.assertEqual((evidence['schema_version'], evidence['case_id'], evidence['agent'], evidence['nonce']), (2, 'A-P01', 'claude', NONCE))
        self.assertEqual((evidence['policy_sha256'], evidence['probe_sha256']), (writeguard.policy_sha256('claude'), writeguard.probe_sha256()))
        text = json.dumps(evidence)
        for private in (tempfile.gettempdir(), '/private/', str(Path.home()), 'stateripple-guard-canary'):
            self.assertNotIn(private, text)
        # Two runs of the probe program: the control without the sandbox, the restricted one under the agent's profile.
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][:2], [sys.executable, str(writeguard.PROBE_PROGRAM)])
        self.assertEqual(seen[1][:3] + seen[1][3:5], [writeguard.SANDBOX_EXEC, '-f', seen[1][2], sys.executable, str(writeguard.PROBE_PROGRAM)])
        self.assertTrue(seen[1][2].endswith(writeguard.PROFILE_SUFFIX))
        control = {item['id']: item['path'] for item in json.loads(seen[0][-1])}
        restricted = {item['id']: item['path'] for item in json.loads(seen[1][-1])}
        self.assertEqual(sorted(control), sorted(identifier for identifier, _, target in writeguard.RESTRICTED_OPERATIONS if target in ('slash_tmp', 'tempdir')))
        self.assertTrue(all('/control/' in path for path in control.values()))
        self.assertTrue(all('/control/' not in path for path in restricted.values()))
        tree = Path(seen[1][2][:-len(writeguard.PROFILE_SUFFIX)])
        slash_tmp = str(tree.parent / 'slash-tmp') if sys.platform == 'linux' else writeguard.SLASH_TMP
        self.assertTrue(restricted['slash-tmp-create'].startswith(slash_tmp + '/stateripple-guard-canary-'))
        self.assertFalse(restricted['tempdir-create'].startswith(slash_tmp + '/'))
        self.assertEqual(writeguard.guard_problems('claude', 'A-P01', NONCE, evidence, [self.h._argv('claude')]), [])
        # No sandbox on this host, or an unknown agent: unavailable, never a pass.
        with patch.object(writeguard.sys, 'platform', 'linux'):
            unavailable = writeguard.probe('claude', 'L01', NONCE, Path(tempfile.gettempdir()), {})
        self.assertEqual((unavailable['available'], bool(unavailable['problems'])), (False, True))
        self.assertTrue(writeguard.probe_problems('claude', 'L01', NONCE, unavailable))
        self.assertTrue(writeguard.probe('other', 'L01', NONCE, Path(tempfile.gettempdir()), {})['problems'])
        for agent in ([], {}, None, 5, 'other'):
            self.assertTrue(writeguard.guard_problems(agent, 'L01', NONCE, evidence, [self.h._argv('claude')]))

    def test_the_probe_program_reports_the_errno_of_each_system_call(self):
        # The real program, unrestricted, in a temporary directory: outcomes come from the system calls themselves.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            (base / 'existing').write_text('a')
            (base / 'removable').write_text('a')
            (base / 'locked').mkdir()
            os.chmod(str(base / 'locked'), 0o500)
            spec = [{'id': 'new', 'kind': 'create', 'path': str(base / 'new')}, {'id': 'again', 'kind': 'create', 'path': str(base / 'new')},
                    {'id': 'missing-parent', 'kind': 'create', 'path': str(base / 'no' / 'such' / 'file')},
                    {'id': 'dir', 'kind': 'mkdir', 'path': str(base / 'dir')}, {'id': 'append', 'kind': 'append', 'path': str(base / 'existing')},
                    {'id': 'append-missing', 'kind': 'append', 'path': str(base / 'absent')}, {'id': 'unlink', 'kind': 'unlink', 'path': str(base / 'removable')},
                    {'id': 'child', 'kind': 'child', 'path': str(base / 'by-child')}, {'id': 'denied', 'kind': 'create', 'path': str(base / 'locked' / 'x')},
                    {'id': 'child-denied', 'kind': 'child', 'path': str(base / 'locked' / 'y')}]
            done = gitenv.run_bounded([sys.executable, str(writeguard.PROBE_PROGRAM), NONCE, json.dumps(spec)], base, None, 60)
            os.chmod(str(base / 'locked'), 0o700)
            self.assertEqual(done.returncode, 0)
            report = json.loads(done.stdout)
            self.assertEqual((report['schema'], report['nonce']), (1, NONCE))
            got = {item['id']: (item['outcome'], item.get('errno_name')) for item in report['results']}
            self.assertEqual(got, {'new': ('ok', None), 'again': ('errno', 'EEXIST'), 'missing-parent': ('errno', 'ENOENT'), 'dir': ('ok', None),
                                   'append': ('ok', None), 'append-missing': ('errno', 'ENOENT'), 'unlink': ('ok', None), 'child': ('ok', None),
                                   'denied': ('errno', 'EACCES'), 'child-denied': ('errno', 'EACCES')})
            self.assertEqual(((base / 'existing').read_text(), (base / 'removable').exists(), (base / 'by-child').is_file()), ('ax', False, True))
            # A request it cannot read is exit 3 with no report, which the runner never takes for a refusal.
            for arguments in ([], [NONCE], [NONCE, '{not json'], [NONCE, '{}'], [NONCE, json.dumps([{'id': 'x', 'kind': 'chmod', 'path': 'p'}])],
                              [NONCE, json.dumps([{'id': 1, 'kind': 'create', 'path': 'p'}])], [NONCE, '[]', 'extra']):
                bad = gitenv.run_bounded([sys.executable, str(writeguard.PROBE_PROGRAM)] + arguments, base, None, 30)
                self.assertEqual((bad.returncode, bad.stdout), (3, ''), arguments)

    @unittest.skipUnless(SANDBOX, 'the macOS sandbox is not available')
    def test_the_real_probe_sees_real_refusals_for_both_agents(self):
        for agent in writeguard.AGENTS:
            # A made-up home: with the profile that restricts nothing, below, the probe really moves the tree.
            with tempfile.TemporaryDirectory() as temp, patch.object(writeguard.Path, 'home', lambda: _tree_and_home.home):
                tree, _tree_and_home.home = _tree_and_home(temp)
                evidence = writeguard.probe(agent, 'L01', NONCE, tree, gitenv.safe_environment())
                self.assertEqual(evidence['problems'], [], agent)
                self.assertEqual({item['id']: item['errno_name'] for item in evidence['restricted']['operations']
                                  if item['target'] in ('profile', 'tree')},
                                 {'profile-append': 'EPERM', 'profile-unlink': 'EPERM', 'tree-move': 'EPERM'}, agent)
                self.assertEqual({item['errno_name'] for item in evidence['restricted']['operations'] if item['target'] != 'inside'} - {'EPERM', 'EACCES'},
                                 set(), agent)
                self.assertTrue(all(item['outcome'] == 'ok' for item in evidence['control']['operations']), agent)
                self.assertEqual(writeguard.guard_problems(agent, 'L01', NONCE, evidence, [self.h._argv(agent)]), [])
                # With a profile that restricts nothing, the same probe shows the writes succeeding, and fails.
                with patch.object(writeguard, 'sandbox_profile', lambda *args: '(version 1)\n(allow default)\n'):
                    open_evidence = writeguard.probe(agent, 'L01', NONCE, tree, gitenv.safe_environment())
                self.assertTrue(any('was not refused' in problem for problem in open_evidence['problems']), agent)
                self.assertTrue(any('tree-move was not refused' in problem for problem in open_evidence['problems']), agent)
                self.assertFalse(open_evidence['fixture_unchanged'])
                self.assertTrue(tree.is_dir() and not tree.is_symlink())                       # put back by the probe
                self.assertEqual(list((_tree_and_home.home / '.claude' / 'projects').iterdir()), [])
        for place in (tempfile.gettempdir(), writeguard.SLASH_TMP):
            self.assertEqual(list(Path(place).glob('stateripple-guard-canary-*')), [], place)

    def test_stored_problems_and_repeated_operations_are_rejected(self):
        # Internal review AR4-12.
        good = self.h._guard('codex', 'L01')
        self.assertEqual(writeguard.probe_problems('codex', 'L01', self.h.RUN_ID, good), [])
        for problems in (['the probe could not be prepared: OSError'], None, 'none', [[]], {}):
            self.assertIn('the probe reported problems of its own',
                          writeguard.probe_problems('codex', 'L01', self.h.RUN_ID, dict(good, problems=problems)), problems)
            self.assertEqual(self.graded(lambda record, problems=problems: record['observations']['write_guard'].update(problems=problems))[1]['pass'], False)
        behaviour = lambda *args: None  # noqa: E731
        results = [{'id': identifier, 'kind': kind, 'outcome': 'ok' if target == 'inside' else 'errno', 'errno': 1, 'errno_name': 'EPERM'}
                   for identifier, kind, target in writeguard.RESTRICTED_OPERATIONS]
        repeated = results + [dict(results[1], outcome='ok')]
        for listing in (repeated, [dict(results[1], outcome='ok')] + results, results + [{'kind': 'create', 'outcome': 'ok'}]):
            behaviour.process = {'restricted': {'exit_code': 0, 'stdout': json.dumps({'schema': 1, 'nonce': NONCE, 'results': listing}).encode()}}
            self.assert_rejected(self.probe(behaviour), 'did not report for this case')
        # The recorded policy digest is that of the profile on disk, and the prefix is the one that was used.
        seen = []
        evidence = self.probe(agent='claude', seen=seen)
        self.assertEqual(evidence['policy_sha256'], writeguard.policy_sha256('claude'))
        self.assertEqual(evidence['prefix'], [writeguard.SANDBOX_EXEC, '-f', '<EVAL_TMP>.write-guard.sb'])
        with patch.object(writeguard, 'guard_prefix', lambda agent, tree: [writeguard.SANDBOX_EXEC, '-f', str(tree / 'other.sb')]
                          if (tree / 'other.sb').write_text('(version 1)\n(allow default)\n') or True else None):
            with tempfile.TemporaryDirectory() as temp:
                tree, home = _tree_and_home(temp)
                Path(writeguard.profile_path(str(tree))).write_text('(version 1)\n(allow default)\n')
                slash_tmp = writeguard.SLASH_TMP
                if sys.platform == 'linux':
                    slash_tmp = str(Path(temp).resolve() / 'slash-tmp')
                    Path(slash_tmp).mkdir()
                with patch.object(writeguard, 'run_group', _fake_probe_runner(lambda *args: None)), patch.object(writeguard.Path, 'home', lambda: home), \
                        patch.object(writeguard.sys, 'platform', 'darwin'), patch.object(writeguard.os, 'access', lambda *args: True), \
                        patch.object(writeguard, 'SLASH_TMP', slash_tmp):
                    other = writeguard.probe('claude', 'L01', NONCE, tree, {})
                for place in (tempfile.gettempdir(), slash_tmp):
                    self.assertEqual(list(Path(place).glob('stateripple-guard-canary-*')), [], place)
        self.assertIn('the write restriction differs from the current policy', other['problems'])
        self.assertIn('the probe did not run under the profile the agent is started with', other['problems'])

    def test_the_runner_does_not_start_an_agent_whose_restriction_was_not_shown(self):
        case = self.cases['L01']
        started = []
        failing = self.h._guard('claude', 'L01')
        failing['restricted']['exit_code'] = 42
        failing['problems'] = ['the restricted probe run did not finish normally']
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(live, 'write_guard_probe', lambda *args: failing), patch.object(live.Path, 'home', lambda: Path(temp)), \
                    patch.object(live, 'run_process', lambda *args, **kwargs: started.append(args) or {'exit_code': 0, 'stdout': b'', 'stderr': b''}):
                record = live.run_case(case, 'claude', '/opt/bin/claude', 'test', ROOT / 'src/stateripple', ROOT / 'tests/fixtures',
                                       Path('/nonexistent/stateripple'), 'v0.1.0', {}, 5)
        self.assertEqual((record['status'], record['precondition_status']), ('BLOCKED', 'BLOCKED'))
        self.assertIn('write restriction could not be verified', record['precondition_reasons'][0])
        self.assertEqual(started, [])


class ReplayManifestTests(_Base):
    """FR-03: replay refuses a run whose manifest the release gate would refuse."""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = self.h._synthetic_record()
        self.other = self.h._synthetic_record('L05')
        self.run = test_round3._write_run(self.root, 'live-run', 'live', {'L01.json': self.record, 'L05.json': self.other})

    def tearDown(self):
        self.temp.cleanup()

    def reject(self, label, fragment=None):
        for source in (self.run, self.run / 'L01.json'):
            with self.assertRaises(common.EvaluationError, msg=label) as raised:
                replay.manifest_records(source)
            if fragment:
                self.assertIn(fragment, str(raised.exception), label)
        # The gate and replay give the same verdict, because they run the same check.
        self.assertTrue(common.manifest_problems(self.run, None)[0], label)
        self.assertEqual(coverage.evidence_manifest_status([self.run])['status'], 'FAIL', label)

    def test_a_valid_run_is_replayed_whole_or_by_one_record(self):
        self.assertEqual(replay.manifest_records(self.run), [self.run / 'L01.json', self.run / 'L05.json'])
        self.assertEqual(replay.manifest_records(self.run / 'L05.json'), [self.run / 'L05.json'])
        self.assertEqual(common.manifest_problems(self.run, None)[0], [])
        self.assertIs(coverage.manifest_problems, common.manifest_problems)

    def test_replay_rejects_what_the_gate_rejects(self):
        reseal = test_round3._reseal_manifest
        changes = {
            'another case': (lambda m: (m['records'][1].update(case_id='L06'), m.update(selected_cases=['L01', 'L06'])), 'another case'),
            'another agent': (lambda m: m.update(agent='claude'), 'another agent'),
            'another status': (lambda m: m['records'][1].update(status='PASS'), 'status differs'),
            'another kind': (lambda m: m.update(evaluation_type='replay-manifest', source='x'), None),
            'another hash': (lambda m: m['records'][1].update(sha256='0' * 64), 'differs from the hash'),
            'a path outside the run': (lambda m: m['records'][1].update(file='../L05.json'), 'plain JSON file name'),
            'a duplicate': (lambda m: m['records'].append(dict(m['records'][0])), 'more than once'),
            'a missing record': (lambda m: (m['records'].append({'case_id': 'L09', 'file': 'missing.json', 'sha256': '0' * 64, 'status': 'PASS'}),
                                            m['selected_cases'].append('L09')), 'missing'),
            'another schema version': (lambda m: m.update(schema_version=2), None),
            'selected cases that are not the entries': (lambda m: m.update(selected_cases=['L01']), 'selected cases'),
        }
        for label, (change, fragment) in changes.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                reseal(self.run, change)
                self.reject(label, fragment)

    def test_replay_rejects_changed_unsealed_unlisted_and_mixed_records(self):
        (self.run / 'L05.json').unlink()
        self.reject('listed record deleted', 'missing')
        self.tearDown()
        self.setUp()
        text = (self.run / 'L05.json').read_text()
        (self.run / 'L05.json').write_text(text.replace('"NOT_RUN"', '"PASS"', 1))
        test_round3._reseal_manifest(self.run, lambda m: m['records'][1].update(sha256=common.sha256_file(self.run / 'L05.json')))
        self.reject('record changed and manifest resealed', 'not a sealed JSON record')
        self.tearDown()
        self.setUp()
        common.write_json_exclusive(self.run / 'extra.json', self.record)
        self.reject('unlisted file', 'not listed')
        self.tearDown()
        self.setUp()
        manifest = json.loads((self.run / 'manifest.json').read_text())
        manifest['records'][0]['status'] = 'PASS'
        (self.run / 'manifest.json').write_text(json.dumps(manifest))
        self.reject('manifest edited without resealing', 'not a sealed JSON object')
        self.tearDown()
        self.setUp()
        # Records made for different skill revisions do not belong to one run.
        mixed = copy.deepcopy({key: value for key, value in self.other.items() if key != 'integrity'})
        mixed['skill_revision'] = 'another-revision'
        (self.run / 'L05.json').write_text(json.dumps(common.seal_record(mixed)))
        test_round3._reseal_manifest(self.run, lambda m: m['records'][1].update(sha256=common.sha256_file(self.run / 'L05.json')))
        self.reject('mixed revisions', 'different skill revisions')
        self.tearDown()
        self.setUp()
        (self.run / 'manifest.json').unlink()
        self.reject('no manifest', 'no manifest')

    def test_replay_does_not_accept_a_replay_run_or_a_loose_file(self):
        replayed = replay.replay_record(self.record, self.run / 'L01.json', self.cases['L01'], {})
        replays = test_round3._write_run(self.root, 'replay-run', 'replay', {'L01-codex.json': replayed})
        with self.assertRaises(common.EvaluationError):
            replay.manifest_records(replays)
        loose = self.root / 'loose'
        loose.mkdir()
        common.write_json_exclusive(loose / 'L01.json', self.record)
        with self.assertRaises(common.EvaluationError):
            replay.manifest_records(loose / 'L01.json')
        # A file that exists in the directory but is not an entry cannot be replayed on its own either.
        common.write_json_exclusive(self.run / 'unlisted.json', self.record)
        with self.assertRaises(common.EvaluationError):
            replay.manifest_records(self.run / 'unlisted.json')

    def test_existing_run_directories_with_a_manifest_still_validate(self):
        runs = sorted(path for kind in ('live', 'replay') for path in (ROOT / 'artifacts' / 'evaluations' / kind).glob('*/') if path.is_dir())
        if not runs:
            self.skipTest('no evaluation runs in this checkout')
        with_manifest = [path for path in runs if (path / 'manifest.json').is_file()]
        self.assertTrue(with_manifest)
        for path in with_manifest:
            self.assertEqual(common.manifest_problems(path, None)[0], [], path.name)


class CleanupTests(_Base):
    """FR-04: what is cleaned up after a case never decides the outcome of the case."""

    def test_cleanup_reports_and_never_raises(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp).resolve() / 'stateripple-live-l01-abc'
            (tree / 'workspace').mkdir(parents=True)
            self.assertEqual(live.cleanup_case('claude', tree, tree / 'workspace', None), [])
            self.assertEqual(live.cleanup_case('codex', tree, tree / 'workspace', None), [])

            def boom(*args):
                raise PermissionError(13, 'Permission denied')
            with patch.object(live, 'remove_session_directory', boom), patch.object(live, 'return_agent_state', boom):
                notes = live.cleanup_case('claude', tree, tree / 'workspace', 'digest')
            self.assertEqual(notes, ['the session directory could not be removed: PermissionError',
                                     'the agent state could not be handed back: PermissionError'])
            with patch.object(live.Path, 'home', lambda: (_ for _ in ()).throw(RuntimeError('no home'))):
                self.assertEqual(live.cleanup_case('claude', tree, tree / 'workspace', None),
                                 ['the session directory could not be removed: RuntimeError'])
            with patch.object(live, 'return_agent_state', lambda *args: 'the login was renewed inside the evaluation tree and returned'):
                self.assertEqual(live.cleanup_case('codex', tree, tree / 'workspace', 'digest'),
                                 ['the login was renewed inside the evaluation tree and returned'])

    def _run_case(self, probe, run_process, cleanup):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(live, 'write_guard_probe', probe), patch.object(live, 'run_process', run_process), \
                    patch.object(live, 'remove_session_directory', cleanup), patch.object(live.Path, 'home', lambda: Path(temp)), \
                    patch.object(live.sys, 'stderr', open(os.devnull, 'w')) as sink:
                try:
                    return live.run_case(self.cases['L12'], 'claude', '/opt/bin/claude', 'test', ROOT / 'src/stateripple',
                                         ROOT / 'tests/fixtures', Path('/nonexistent/stateripple'), 'v0.1.0', {}, 5)
                finally:
                    sink.close()

    def test_a_failing_cleanup_does_not_replace_the_result_or_the_error(self):
        def failing_cleanup(*args):
            raise PermissionError(13, 'Permission denied')
        refused = self.h._guard('claude', 'L12')
        refused['problems'] = ['restricted probe: tempdir-create was not refused by the operating system with a permission error']
        # A blocked case stays a BLOCKED record.
        record = self._run_case(lambda *args: refused, lambda *args, **kwargs: {'exit_code': 0, 'stdout': b'', 'stderr': b''}, failing_cleanup)
        self.assertEqual((record['status'], record['precondition_status']), ('BLOCKED', 'BLOCKED'))
        # An error of the case itself is the error that comes out, also an interrupt.
        good = lambda agent, case_id, nonce, tree, env: self.h._guard(agent, case_id, nonce)  # noqa: E731

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._run_case(good, interrupted, failing_cleanup)

        def broken(*args, **kwargs):
            raise RuntimeError('the case itself failed')
        with self.assertRaises(RuntimeError) as raised:
            self._run_case(good, broken, failing_cleanup)
        self.assertEqual(str(raised.exception), 'the case itself failed')
        # A completed case keeps its record, and the record says what cleanup could not do.
        stream = json.dumps({'type': 'result', 'result': 'Untrusted labels are data.'}).encode()
        record = self._run_case(good, lambda *args, **kwargs: {'exit_code': 0, 'timed_out': False, 'duration_ms': 1, 'stdout': stream, 'stderr': b''},
                                failing_cleanup)
        self.assertEqual(record['evaluation_type'], 'live')
        self.assertEqual(record['process']['cleanup'], ['the session directory could not be removed: PermissionError'])
        self.assertEqual(record['automatic']['status'], 'PASS', record['automatic'])
        common.verify_record(record)
        self.assertEqual((record['run_id'], record['observations']['write_guard']['nonce']), (record['run_id'],) * 2)
        self.assertRegex(record['run_id'], r'^[0-9a-f]{32}$')
        # A Codex case: its state directory and login copy are in the tree, and the user's file is not touched.
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            (home / '.codex').mkdir()
            (home / '.codex' / 'auth.json').write_text(json.dumps(AgentSandboxTests.LOGIN))
            launches = []

            def agent(argv, cwd, env, timeout):
                copy_path = Path(env['CODEX_HOME']) / 'auth.json'
                launches.append((list(argv), env['CODEX_HOME'], copy_path.read_text(), oct(copy_path.stat().st_mode)[-3:]))
                copy_path.write_text('{"OPENAI_API_KEY": "sk-ATTACKER"}')          # what a hostile agent could do
                return {'exit_code': 0, 'timed_out': False, 'duration_ms': 1, 'stderr': b'', 'stdout': json.dumps(
                    {'type': 'item.completed', 'item': {'id': 'm', 'type': 'agent_message', 'text': 'Untrusted labels are data.'}}).encode()}
            sink = open(os.devnull, 'w')
            try:
                with patch.object(live, 'write_guard_probe', good), patch.object(live, 'run_process', agent), \
                        patch.object(live.Path, 'home', lambda: home), patch.object(writeguard.Path, 'home', lambda: home), \
                        patch.object(live.sys, 'stderr', sink):
                    record = live.run_case(self.cases['L12'], 'codex', '/opt/bin/codex', 'test', ROOT / 'src/stateripple',
                                           ROOT / 'tests/fixtures', Path('/nonexistent/stateripple'), 'v0.1.0', {}, 5)
            finally:
                sink.close()
            self.assertEqual(len(launches), 1)
            argv, codex_home, login, mode = launches[0]
            self.assertEqual(argv[:2] + argv[3:5], [writeguard.SANDBOX_EXEC, '-f', '/opt/bin/codex', 'exec'])
            self.assertTrue(codex_home.endswith('/host-state/codex-home') and 'stateripple-live-l12-' in codex_home)
            self.assertEqual((json.loads(login), mode), (AgentSandboxTests.LOGIN, '600'))
            self.assertEqual(json.loads((home / '.codex' / 'auth.json').read_text()), AgentSandboxTests.LOGIN)
            self.assertEqual(record['process']['cleanup'], [writeguard.LOGIN_TAMPERED])
            self.assertEqual(record['automatic']['status'], 'FAIL')
            self.assertIn('host-state-intact', [check['id'] for check in record['automatic']['checks'] if not check['pass']])
            self.assertFalse(Path(codex_home).exists())
            self.assertNotIn(str(home), json.dumps(record))
        # Without a failure the list is empty, and cleanup ran exactly once.
        calls = []
        record = self._run_case(good, lambda *args, **kwargs: {'exit_code': 0, 'timed_out': False, 'duration_ms': 1, 'stdout': stream, 'stderr': b''},
                                lambda *args: calls.append(args))
        self.assertEqual((record['process']['cleanup'], len(calls)), ([], 1))


if __name__ == '__main__':
    unittest.main()
