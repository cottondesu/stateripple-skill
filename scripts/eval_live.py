#!/usr/bin/env python3

from __future__ import annotations

import argparse
import contextlib
import stat
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

if not __package__:
    from gitenv import open_directory, remove_tree
    from writeguard import (CODEX_HOME_NAME, HOST_STATE, profile_path, agent_environment, guard_prefix, prepare_agent_state,
                            probe as write_guard_probe, return_agent_state, session_directory)
    from eval_common import (
        EXPECTED_CLI_VERSION,
        TOOL_LOG_VERSION,
        EvaluationError,
        agent_command,
        case_map,
        create_git_observer,
        create_observing_wrapper,
        extract_agent_events,
        final_status,
        git_state,
        grade_evidence,
        isolated_git_environment,
        load_invocations,
        prepare_workspace,
        read_skill_files,
        read_catalogs,
        render_prompt,
        run_process,
        safe_environment,
        sanitize_value,
        seal_record,
        sha256_bytes,
        sha256_file,
        sha256_tree,
        utc_now,
        write_json_exclusive,
        workspace_inventory,
    )
else:
    from .gitenv import open_directory, remove_tree
    from .writeguard import (CODEX_HOME_NAME, HOST_STATE, profile_path, agent_environment, guard_prefix, prepare_agent_state,
                             probe as write_guard_probe, return_agent_state, session_directory)
    from .eval_common import (
        EXPECTED_CLI_VERSION,
        TOOL_LOG_VERSION,
        EvaluationError,
        agent_command,
        case_map,
        create_git_observer,
        create_observing_wrapper,
        extract_agent_events,
        final_status,
        git_state,
        grade_evidence,
        isolated_git_environment,
        load_invocations,
        prepare_workspace,
        read_skill_files,
        read_catalogs,
        render_prompt,
        run_process,
        safe_environment,
        sanitize_value,
        seal_record,
        sha256_bytes,
        sha256_file,
        sha256_tree,
        utc_now,
        write_json_exclusive,
        workspace_inventory,
    )


MISMATCH_SOURCE = "\n".join((
    '#!' + os.path.realpath(sys.executable),
    "import json",
    "import os",
    "import sys",
    'entry = {"argv": sys.argv[1:], "exit_code": 0 if sys.argv[1:] == ["version"] else 97, "report": None, "stdout": "stateripple v9.9.9\\n" if sys.argv[1:] == ["version"] else ""}',
    'with open(os.environ["STATERIPPLE_OBSERVATION_LOG"], "a", encoding="utf-8") as handle:',
    '    handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\\n")',
    'if sys.argv[1:] in (["version"], ["--version"]):',
    '    print("stateripple v9.9.9")',
    "    sys.exit(0)",
    'print("incompatible StateRipple boundary probe", file=sys.stderr)',
    "sys.exit(97)",
    "",
))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run explicit StateRipple skill live evaluations")
    parser.add_argument("--agent", required=True, choices=("codex", "claude"))
    parser.add_argument("--suite", default="release")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--skill-source", type=Path)
    parser.add_argument("--eval-dir", type=Path)
    parser.add_argument("--fixtures-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=240)
    return parser.parse_args(argv)


def stateripple_version(executable: Path, cwd: Path) -> Tuple[Optional[str], Dict[str, Any]]:
    result = run_process([str(executable), "version"], cwd, None, 15)
    stdout = result.pop("stdout")
    stderr = result.pop("stderr")
    valid = result['exit_code'] == 0 and stdout == b'stateripple v0.1.0\n' and not stderr
    result["stdout_sha256"] = sha256_bytes(stdout)
    result["stderr_sha256"] = sha256_bytes(stderr)
    return ('v0.1.0' if valid else None), result


def resolve_real_cli(project_root: Path) -> Tuple[Optional[Path], Optional[str], Dict[str, Any]]:
    configured = os.environ.get("STATERIPPLE")
    found = configured if configured else shutil.which("stateripple")
    if not found:
        return None, None, {"reason": "StateRipple executable was not found"}
    path = Path(found).expanduser().resolve()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        return path, None, {"reason": "StateRipple path is not an executable file"}
    version, proof = stateripple_version(path, project_root)
    return path, version, proof


def create_mismatch_probe(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "stateripple"
    path.write_text(MISMATCH_SOURCE, encoding="utf-8")
    path.chmod(0o700)
    return path


def suite_case_ids(suite: str, catalogs: Mapping[str, Any]) -> List[str]:
    suites = catalogs["live_suites"]["suites"]
    if suite == 'activation':
        return [item['id'] for group in ('positive','negative') for item in catalogs['activation'][group]]
    if suite not in suites:
        raise EvaluationError("unknown live suite: %s" % suite)
    return list(suites[suite]["cases"])


def select_case_ids(args: argparse.Namespace, catalogs: Mapping[str, Any]) -> List[str]:
    selected = list(args.case_ids) if args.case_ids else suite_case_ids(args.suite, catalogs)
    known = case_map(catalogs,include_activation=True)
    unknown = [case_id for case_id in selected if case_id not in known]
    if unknown:
        raise EvaluationError("unknown live cases: %s" % ", ".join(unknown))
    return selected


def precondition_for_case(case: Mapping[str, Any], cli_path: Optional[Path], cli_version: Optional[str]) -> Tuple[str, List[str]]:
    case_id = case["id"]
    if case_id in {"L03", "L04", "L12"}:
        return "PASS", []
    if cli_path is None:
        return "BLOCKED", ["StateRipple v0.1.0 is required for this live case"]
    if cli_version != EXPECTED_CLI_VERSION:
        return "BLOCKED", ["StateRipple version %r does not equal %s" % (cli_version, EXPECTED_CLI_VERSION)]
    return "PASS", []


def build_environment(
    case_id: str,
    temp_root: Path,
    observation_log: Path,
    real_cli: Optional[Path],
    git_log: Optional[Path] = None,
) -> Tuple[Dict[str, str], str]:
    # Agents inherit the caller's environment except for Git state: no injected configuration, no host Git
    # configuration, no hooks, monitors, pagers or credential helpers reach Git started by the agent or the CLI.
    env = safe_environment()
    if git_log is not None:
        # Record every Git command and keep the host's Git identity, helpers and prompts out of reach.
        real_git = shutil.which('git')
        if real_git is None:
            raise EvaluationError('git is required for this live case')
        create_git_observer(temp_root / 'bin', real_git, git_log)
        # Login shells rebuild PATH from the user's profile; a private zsh startup directory keeps the observer first.
        startup = temp_root / 'zdot'
        startup.mkdir(exist_ok=True)
        for name in ('.zshenv', '.zprofile'):
            (startup / name).write_text('export PATH=%s:"$PATH"\n' % shlex.quote(str(temp_root / 'bin')), encoding='utf-8')
        env['ZDOTDIR'] = str(startup)
    for variable, directory in (('GOCACHE','go-cache'),('GOMODCACHE','go-modcache'),('GOTMPDIR','go-tmp')):
        cache = temp_root / directory
        cache.mkdir(exist_ok=True)
        env[variable] = str(cache)
    scratch = temp_root / 'agent-tmp'
    scratch.mkdir(exist_ok=True)
    env['TMPDIR'] = str(scratch)
    env["STATERIPPLE_OBSERVATION_LOG"] = str(observation_log)
    bin_dir = temp_root / "bin"
    if case_id == "L03":
        bin_dir.mkdir(parents=True, exist_ok=True)
        node = shutil.which('node')
        if node:
            (bin_dir / 'node').symlink_to(node)
        env["PATH"] = os.pathsep.join((str(bin_dir), "/usr/bin", "/bin", "/usr/sbin", "/sbin"))
        env.pop("STATERIPPLE_REAL", None)
        env.pop('STATERIPPLE', None)
        return env, "absent"
    if case_id == "L04":
        create_mismatch_probe(bin_dir)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env.pop("STATERIPPLE_REAL", None)
        return env, "incompatible-version-boundary-probe"
    if real_cli is not None:
        create_observing_wrapper(bin_dir)
        env["STATERIPPLE_REAL"] = str(real_cli)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        return env, "real-v0.1.0-through-observer"
    env.pop("STATERIPPLE_REAL", None)
    return env, "absent"


def blocked_evidence(
    case: Mapping[str, Any],
    agent: str,
    agent_version: Optional[str],
    skill_revision: str,
    reasons: List[str],
    cli_path: Optional[Path],
    cli_version: Optional[str],
) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {
        "schema_version": 1,
        "evaluation_type": "live",
        "case_id": case["id"],
        "recorded_at": utc_now(),
        "agent": {"name": agent, "version": agent_version},
        "skill_revision": skill_revision,
        "fixture_id": case.get("fixture_id"),
        "prompt": case["prompt"],
        "cli": {"path": '<CONFIGURED_STATERIPPLE>' if cli_path else None, "version": cli_version, "expected_version": EXPECTED_CLI_VERSION},
        "precondition_status": "BLOCKED",
        "precondition_reasons": reasons,
        "process": {"exit_code": None, "timed_out": False, "duration_ms": 0},
        "observations": {"cli_invocations": [], "agent_events": {"final_response": "", "tool_commands": [], "json_parse_errors": 0}},
        "automatic": {"status": "BLOCKED", "checks": [], "reasons": reasons},
        "manual": {"status": "NOT_RUN", "reviewer": None, "reviewed_at": None, "notes": None},
        "status": "BLOCKED",
        'unverified': ['Agent behavior was not observed; this record is a blocker declaration, not execution evidence.'],
    }
    return seal_record(evidence)


LIVE_TREE_PREFIX = "stateripple-live-"


@contextlib.contextmanager
def evaluation_tree(prefix: str):
    """A new directory for one case, removed afterwards without following links and without ever raising.

    The agent owns what is inside by the time it is removed: it may have left read-only directories,
    links that point outside, or entries that cannot be deleted. Removal never opens, changes, or
    deletes anything a link points to (gitenv.remove_tree). A tree that could not be removed
    completely is reported on standard error instead of replacing the result of the case; it may
    still hold the copy of the Codex login. The profile beside the tree is removed with it.
    """
    root = tempfile.mkdtemp(prefix=prefix)
    profile = profile_path(os.path.realpath(root))
    try:
        yield root
    finally:
        try:
            if not remove_tree(root):
                print("eval-live: the evaluation tree could not be removed completely: %s" % root, file=sys.stderr)
        except Exception as error:  # noqa: BLE001 - removal reports, it does not decide the outcome
            print("eval-live: the evaluation tree could not be removed: %s" % type(error).__name__, file=sys.stderr)
        try:
            os.unlink(profile)
        except OSError:
            pass


def tree_identity(tree: Path) -> Tuple[int, int]:
    """Which directory the tree is, taken when the runner has just made it."""
    status = os.lstat(str(tree))
    return status.st_dev, status.st_ino


def require_same_tree(tree: Path, identity: Tuple[int, int]) -> None:
    """Stop the case unless the tree's path still names the directory the runner made.

    The profile refuses the agent any change to that entry, and the probe showed it; this is the
    runner's own check on top, made before it starts anything else by that path.
    """
    try:
        status = os.lstat(str(tree))
    except OSError as error:
        raise EvaluationError("the evaluation tree is gone: %s" % type(error).__name__) from error
    if not stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) != tuple(identity):
        raise EvaluationError("the evaluation tree was replaced during the case")


def replace_fixture(source: Path, target: Path, workspace: Path) -> bool:
    """Put the follow-up snapshot in place of the first one, without writing through anything the agent left there.

    The workspace is opened as a directory without following a link, and everything else is done
    relative to that open directory: the target must be a regular file in it, the new content goes
    into a new file beside it, and that file is moved over the target. So no link is followed, and
    nothing changes if the workspace is swapped for something else while this runs. Returns False,
    and leaves the target as it is, when the place is not what the runner made or anything fails.
    """
    if target.parent != workspace:
        return False
    temporary = ".stateripple-fixture-%s" % uuid.uuid4().hex
    directory = None
    created = False
    try:
        content = source.read_bytes()
        directory = open_directory(workspace)
        if not stat.S_ISREG(os.stat(target.name, dir_fd=directory, follow_symlinks=False).st_mode):
            return False
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=directory)
        created = True
        try:
            os.write(descriptor, content)
        finally:
            os.close(descriptor)
        os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        return True
    except OSError:
        return False
    finally:
        if directory is not None:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            os.close(directory)


def sandboxed_agent_version(agent: str, executable: str) -> Optional[str]:
    """Ask the agent program for its version, under the same kind of profile as a case: it is never started unrestricted."""
    with evaluation_tree(LIVE_TREE_PREFIX + "version-") as name:
        tree = Path(name).resolve()
        try:
            prepare_agent_state("claude", tree)   # the host-state directory only; no login is needed to print a version
            environment = dict(safe_environment(), **agent_environment(agent, tree))
            (tree / HOST_STATE / CODEX_HOME_NAME).mkdir(exist_ok=True)
            result = run_process(guard_prefix(agent, tree) + [executable, "--version"], tree, environment, 15)
        except (OSError, ValueError):
            return None
    text = (result.pop("stdout") + result.pop("stderr")).decode("utf-8", "replace").strip()
    return text.splitlines()[0] if text and result.get("exit_code") == 0 else None


def remove_session_directory(temp_root: Path, workspace: Path) -> None:
    """Remove the conversation directory Claude Code may have created for this case's workspace, and nothing else.

    Only the workspace of an evaluation tree this runner created is accepted, so a path that happens
    to be a real project of the user can never be turned into a directory to delete.
    """
    if not (temp_root.is_absolute() and temp_root.name.startswith(LIVE_TREE_PREFIX) and len(temp_root.name) > len(LIVE_TREE_PREFIX)
            and workspace == temp_root / 'workspace' and '..' not in temp_root.parts):
        return
    home = Path.home().resolve()
    target = Path(session_directory(str(workspace), str(home)))
    projects = home / '.claude' / 'projects'
    if target.parent != projects or LIVE_TREE_PREFIX not in target.name:
        return
    # A real directory is emptied and removed without following links; a link in its place is not followed by
    # that and is then removed as a link, never what it points to.
    remove_tree(target)
    if target.is_symlink():
        target.unlink()


def cleanup_case(agent: str, temp_root: Path, workspace: Path, login_digest: Optional[str]) -> List[str]:
    """What the runner undoes or hands back outside the tree when a case ends. Never raises.

    Returns one line for each thing that happened or failed, for the record or for the operator. A
    failure here must not replace the result or the error of the case itself.
    """
    notes: List[str] = []
    if agent == 'claude':
        try:
            # The one directory outside the tree the profile lets Claude Code write: this case's own conversation.
            remove_session_directory(temp_root, workspace)
        except Exception as error:  # noqa: BLE001 - cleanup reports, it does not decide the outcome
            notes.append('the session directory could not be removed: %s' % type(error).__name__)
    try:
        returned = return_agent_state(agent, temp_root, login_digest)
        if returned:
            notes.append(returned)
    except Exception as error:  # noqa: BLE001
        notes.append('the agent state could not be handed back: %s' % type(error).__name__)
    return notes


def merge_turn_events(turns: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """One event summary for the whole case: the last answer, and everything every turn did."""
    def collected(key: str) -> List[Any]:
        return [item for turn in turns for item in turn[key]]
    return {'final_response': turns[-1]['final_response'],
            'tool_commands': collected('tool_commands'), 'read_paths': collected('read_paths'),
            'skill_invocations': collected('skill_invocations'), 'tool_uses': collected('tool_uses'),
            'searches': collected('searches'), 'tool_calls': collected('tool_calls'),
            'skill_reference': turns[-1]['skill_reference'], 'errors': collected('errors'),
            'json_parse_errors': sum(turn['json_parse_errors'] for turn in turns),
            'tool_log_version': TOOL_LOG_VERSION}


def run_case(
    case: Mapping[str, Any],
    agent: str,
    agent_executable: str,
    agent_version: str,
    skill_source: Path,
    fixtures_dir: Path,
    cli_path: Optional[Path],
    cli_version: Optional[str],
    cli_version_proof: Mapping[str, Any],
    timeout: int,
) -> Dict[str, Any]:
    skill_revision = sha256_tree(skill_source)
    skill_files = read_skill_files(skill_source)
    precondition, reasons = precondition_for_case(case, cli_path, cli_version)
    if precondition == "BLOCKED":
        return blocked_evidence(case, agent, agent_version, skill_revision, reasons, cli_path, cli_version)

    with evaluation_tree("%s%s-" % (LIVE_TREE_PREFIX, case["id"].lower())) as temp_name:
        # The sandbox names real paths, so the tree is addressed by its resolved location throughout.
        temp_root = Path(temp_name).resolve()
        identity = tree_identity(temp_root)
        workspace = temp_root / "workspace"
        cleaned = False
        login_digest: Optional[str] = None
        try:
            values = prepare_workspace(workspace, agent, skill_source, case, fixtures_dir)
            fixture = {'id':case.get('fixture_id'), 'raw_sha256':sha256_file(Path(values['fixture'])) if Path(values['fixture']).is_file() else None}
            revisions = None
            if (workspace / '.git').is_dir():
                revision_process = run_process(['git','--no-lazy-fetch','rev-parse','HEAD~1','HEAD'],workspace,isolated_git_environment(),15)
                if revision_process['exit_code'] != 0:
                    raise EvaluationError('could not resolve isolated fixture revisions')
                base,head = revision_process['stdout'].decode().splitlines()
                revisions = {'base':base,'head':head}
            boundary = '\nEvaluation boundary: keep report output on stdout unless this case requests --out. Use read-only checks and do not create scratch files, install tools, mutate fixtures, commit, push, or create files elsewhere.'
            followup = case.get('followup') if isinstance(case.get('followup'), dict) else None
            turn_specs = [{'prompt': case['prompt'], 'fixture_id': None}] + ([followup] if followup else [])
            session = str(uuid.uuid4()) if followup else None
            observation_log = temp_root / "stateripple-observations.jsonl"
            git_guard = bool(case['expected'].get('git_guard'))
            git_log = temp_root / 'git-observations.jsonl' if git_guard else None
            remote = temp_root / 'remote.git'
            git_before = git_state(workspace, remote) if git_guard else None
            env, cli_source = build_environment(case["id"], temp_root, observation_log, cli_path, git_log)
            # The agent host keeps its own state (scratch files, and for Codex its whole state directory with a copy
            # of the login) inside the tree. It is the host program's state, not the agent's work, so it is counted
            # with the caches.
            login_digest = prepare_agent_state(agent, temp_root)
            env.update(agent_environment(agent, temp_root))
            # Nothing is run unless, for this case, a write outside the tree has just been refused by the operating
            # system under the profile the agent will be started with.
            run_id = uuid.uuid4().hex
            guard = write_guard_probe(agent, case['id'], run_id, temp_root, env)
            if guard['problems']:
                return blocked_evidence(case, agent, agent_version, skill_revision,
                                        ['the write restriction could not be verified: %s' % '; '.join(guard['problems'])],
                                        cli_path, cli_version)
            replacements = {str(temp_root): "<EVAL_TMP>", str(temp_root.resolve()): '<EVAL_TMP>',
                            str(Path.home()): "<HOME>", str(Path.home().resolve()): '<HOME>'}
            host_tmp = Path(tempfile.gettempdir())
            for candidate in (str(host_tmp), str(host_tmp.resolve())):
                if len(candidate) > 4:
                    replacements.setdefault(candidate, '<HOST_TMP>')
            # agent-tmp/xcrun_db is written by the macOS developer-tools shim whenever python3/git start with a new TMPDIR.
            cache_prefixes = ('go-cache/','go-modcache/','go-tmp/','host-state/')
            toolchain_files = ('agent-tmp/xcrun_db',)
            harness_files = ('stateripple-observations.jsonl', 'git-observations.jsonl')

            def observed_tree() -> Dict[str, str]:
                return {key: value for key, value in workspace_inventory(temp_root).items()
                        if key not in harness_files and key not in toolchain_files and not key.startswith(cache_prefixes)}

            def cache_tree() -> Dict[str, str]:
                return {key: value for key, value in workspace_inventory(temp_root).items()
                        if key.startswith(cache_prefixes) or key in toolchain_files}

            started_at = utc_now()
            turns: List[Dict[str, Any]] = []
            after: Dict[str, str] = {}
            changed_files: List[str] = []
            cache_before = cache_tree()
            stdout = stderr = b''
            process_result: Dict[str, Any] = {}
            command: List[str] = []
            prompt = ''
            for index, spec in enumerate(turn_specs):
                if index:
                    # Nothing may change between turns either: a process that outlived the previous turn is not the harness.
                    between = observed_tree()
                    changed_files.extend(key for key in set(after) | set(between) if after.get(key) != between.get(key))
                    # The harness, not the agent, replaces the snapshot between turns.
                    replacement = fixtures_dir / spec['fixture_id']
                    if not replacement.is_file():
                        raise EvaluationError('follow-up fixture does not exist: %s' % replacement)
                    if not replace_fixture(replacement, Path(values['fixture']), workspace):
                        # The agent replaced the snapshot or the workspace with something else. Nothing is written
                        # through it; the case ends here as a failure.
                        changed_files.append('workspace/fixture.json (not a regular file in the workspace; follow-up not started)')
                        break
                    fixture['followup_id'] = spec['fixture_id']
                    fixture['followup_raw_sha256'] = sha256_file(replacement)
                prompt = render_prompt({'id': case['id'], 'prompt': spec['prompt']}, values) + boundary
                # The profile is written again before every turn; the command line that names it comes from one place.
                require_same_tree(temp_root, identity)
                guard_prefix(agent, temp_root)
                command = agent_command(agent, agent_executable, workspace, prompt, session, bool(index))
                before = observed_tree()
                turn_process = run_process(command, workspace, env, timeout)
                turn_stdout = turn_process.pop("stdout")
                turn_stderr = turn_process.pop("stderr")
                after = observed_tree()
                changed_files.extend(key for key in set(before) | set(after) if before.get(key) != after.get(key))
                turn_events = extract_agent_events(agent, turn_stdout, replacements, skill_files)
                if agent == 'codex' and followup and not index:
                    session = turn_events.get('session_id')
                stdout += turn_stdout
                stderr += turn_stderr
                if not process_result:
                    process_result = turn_process
                else:
                    process_result = {
                        **turn_process,
                        'exit_code': turn_process.get('exit_code') if process_result.get('exit_code') == 0 else process_result.get('exit_code'),
                        'timed_out': bool(process_result.get('timed_out') or turn_process.get('timed_out')),
                        'duration_ms': process_result.get('duration_ms', 0) + turn_process.get('duration_ms', 0)}
                turns.append({'prompt': sanitize_value(prompt, replacements), 'final_response': turn_events['final_response'],
                              'tool_commands': turn_events['tool_commands'], 'read_paths': turn_events['read_paths'],
                              'skill_invocations': turn_events['skill_invocations'], 'errors': turn_events['errors'],
                              'tool_uses': turn_events['tool_uses'], 'searches': turn_events['searches'],
                              'tool_calls': turn_events['tool_calls'], 'skill_reference': turn_events['skill_reference'],
                              'json_parse_errors': turn_events['json_parse_errors'],
                              'exit_code': turn_process.get('exit_code'),
                              'argv': sanitize_value(command[:-1], replacements),
                              'session': session if agent == 'claude' else turn_events.get('session_id') if not index else session,
                              'host_session_id': turn_events.get('session_id'),
                              'invocation_count': len(load_invocations(observation_log, replacements))})
                if turn_process.get('exit_code') != 0 or turn_process.get('timed_out') or (followup and session is None):
                    break
            agent_events = merge_turn_events(turns)
            invocations = load_invocations(observation_log, replacements)
            for invocation in invocations:
                for stream in ('stdout', 'stderr'):
                    if isinstance(invocation.get(stream), str):
                        invocation['original_' + stream + '_sha256'] = invocation.get(stream + '_sha256')
                        invocation[stream + '_sha256'] = sha256_bytes(invocation[stream].encode('utf-8'))
            cache_after = cache_tree()
            cache_changes = sorted(key for key in set(cache_before) | set(cache_after) if cache_before.get(key) != cache_after.get(key))
            process = {
                **process_result,
                "stdout_sha256": sha256_bytes(stdout),
                "stderr_sha256": sha256_bytes(stderr),
            }
            sanitized_prompt = sanitize_value(prompt, replacements)
            # Done before the record is made, so that what happened is in the record and cannot replace it.
            cleanup_notes = cleanup_case(agent, temp_root, workspace, login_digest)
            cleaned = True
            process['cleanup'] = cleanup_notes
            evidence: Dict[str, Any] = {
                "schema_version": 1,
                "evaluation_type": "live",
                "case_id": case["id"],
                "run_id": run_id,
                "recorded_at": started_at,
                "agent": {"name": agent, "version": agent_version, "argv": sanitize_value(command[:-1], replacements)},
                "skill_revision": skill_revision,
                "fixture_id": case.get("fixture_id"),
                "prompt": sanitized_prompt,
                "cli": {
                    "path": "<CONFIGURED_STATERIPPLE>" if cli_path else None,
                    "version": cli_version,
                    "expected_version": EXPECTED_CLI_VERSION,
                    "source": cli_source,
                    "version_proof": dict(cli_version_proof),
                },
                "precondition_status": "PASS",
                "precondition_reasons": [],
                "process": process,
                "observations": {"cli_invocations": invocations, "agent_events": agent_events,
                                 'fixture': fixture, 'revisions': revisions, 'write_guard': guard,
                                 'filesystem_unchanged': not changed_files,
                                 'allowed_cache_change_count': len(cache_changes),
                                 'changed_files': sorted(set(changed_files))},
                "automatic": {},
                "manual": {"status": "NOT_RUN", "reviewer": None, "reviewed_at": None, "notes": None},
                "status": "NOT_RUN",
                'unverified': ['Natural-language correctness and complete safety behavior require qualitative review.',
                               'Direct file writes outside the evaluation tree by the agent program and everything it starts are '
                               'restricted by the sandbox profile recorded under write_guard, as shown by its probe and not by watching '
                               'the agent. Refused attempts, the network, requests to system services and, for Claude Code, the one '
                               'session directory the profile allows are not restricted or not observed.',
                               'Observation hashes detect accidental alteration, not malicious resealing or agent tampering.'],
            }
            if followup:
                evidence['observations']['turns'] = turns
            if git_guard:
                # The agent could have rewritten the repository's configuration; this Git runs under its profile too.
                require_same_tree(temp_root, identity)
                evidence['observations']['git_state'] = sanitize_value(
                    {'before': git_before, 'after': git_state(workspace, remote, guard_prefix(agent, temp_root))}, replacements)
                evidence['observations']['git_invocations'] = load_invocations(git_log, replacements)
                evidence['unverified'].append('Network transfers that bypass the observed git executable are not detectable; the only configured remote is a local path.')
            diagnostic = sanitize_value(stderr.decode('utf-8', 'replace'), replacements)
            service_failed = process_result.get('exit_code') != 0 or bool(agent_events.get('errors'))
            combined_errors = diagnostic + '\n' + '\n'.join(agent_events.get('errors', []))
            earlier = turns[-2]['invocation_count'] if len(turns) > 1 else 0
            if turns[-1]['invocation_count'] == earlier and service_failed and re.search(r'usage limit|rate.limit|not logged in|authentication|unauthorized|connection|connect|network|api.error|api key|ENOTFOUND|EAI_AGAIN', combined_errors, re.I):
                evidence['environment_blocker'] = 'Agent could not access its authenticated service; no CLI analysis evidence was produced.'
            evidence['process']['diagnostic'] = diagnostic[-4000:]
            automatic = grade_evidence(case, evidence)
            evidence["automatic"] = automatic
            evidence["status"] = final_status(automatic["status"], "NOT_RUN")
            return seal_record(evidence)

        finally:
            if not cleaned:
                # A way out without a record (blocked, interrupted, failed): still clean up, and never let a problem
                # here replace the result or the error that is on its way out.
                for note in cleanup_case(agent, temp_root, workspace, login_digest):
                    print('eval-live: %s' % note, file=sys.stderr)


def case_record(case: Mapping[str, Any], agent: str, agent_executable: str, agent_version: str, skill_source: Path,
                fixtures_dir: Path, cli_path: Optional[Path], cli_version: Optional[str],
                cli_version_proof: Mapping[str, Any], timeout: int) -> Dict[str, Any]:
    """The record of one case. A case the runner could not finish is a blocked record, not the end of the suite.

    What an agent leaves in its tree (a log that is no longer a log, a repository Git cannot read,
    more files than the runner lists) can make the runner give up on that case. That is recorded
    as blocked, which is never a pass, and the remaining cases still run.
    """
    try:
        return run_case(case, agent, agent_executable, agent_version, skill_source, fixtures_dir,
                        cli_path, cli_version, cli_version_proof, timeout)
    except EvaluationError as error:
        print("eval-live: %s: %s" % (case["id"], error), file=sys.stderr)
        return blocked_evidence(case, agent, agent_version, sha256_tree(skill_source),
                                ["the runner could not complete the case: %s" % error], cli_path, cli_version)


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.resolve()
    eval_dir = (args.eval_dir or project_root / "tests/evals").resolve()
    fixtures_dir = (args.fixtures_dir or project_root / "tests/fixtures").resolve()
    skill_source = (args.skill_source or project_root / "dist/stateripple").resolve()
    output_parent = (args.output_dir or project_root / "artifacts/evaluations/live").resolve()
    if args.timeout < 1:
        raise EvaluationError("timeout must be positive")
    catalogs = read_catalogs(eval_dir)
    selected = select_case_ids(args, catalogs)
    cases = case_map(catalogs,include_activation=True)
    if not skill_source.is_dir():
        raise EvaluationError("built skill does not exist; run the build first: %s" % skill_source)

    agent_executable = shutil.which(args.agent)
    if agent_executable is None:
        agent_executable = ""
        agent_version = None
    else:
        agent_version = sandboxed_agent_version(args.agent, agent_executable)

    cli_path, cli_version, cli_version_proof = resolve_real_cli(project_root)
    lock = acquire_live_lock(output_parent)
    try:
        return _run_selected(args, catalogs, selected, cases, skill_source, fixtures_dir, output_parent,
                             agent_executable, agent_version, cli_path, cli_version, cli_version_proof)
    finally:
        release_live_lock(lock)


def acquire_live_lock(output_parent: Path) -> Path:
    """Allow one live evaluation per evidence directory, so agent processes never run in parallel by accident."""
    output_parent.mkdir(parents=True, exist_ok=True)
    lock = output_parent / '.live-evaluation.lock'
    for _ in range(2):
        try:
            descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                owner = int(lock.read_text(encoding='utf-8').strip() or '0')
                if owner <= 0:
                    raise ValueError('lock holds no process id')
                os.kill(owner, 0)
            except (ValueError, ProcessLookupError, OverflowError):
                lock.unlink()
                continue
            except (OSError, PermissionError):
                pass
            raise EvaluationError('another live evaluation is running (lock: %s); run suites one at a time' % lock)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(str(os.getpid()))
        return lock
    raise EvaluationError('could not take the live evaluation lock: %s' % lock)


def release_live_lock(lock: Path) -> None:
    try:
        if lock.read_text(encoding='utf-8').strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


def _run_selected(args, catalogs, selected, cases, skill_source, fixtures_dir, output_parent,
                  agent_executable, agent_version, cli_path, cli_version, cli_version_proof) -> int:
    run_dir = output_parent / (utc_now().replace(":", "").replace("-", "") + "-" + args.agent + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest_records = []
    statuses = []
    service_blocker = None
    for case_id in selected:
        case = cases[case_id]
        if not agent_executable or not agent_version or service_blocker:
            record = blocked_evidence(
                case,
                args.agent,
                agent_version,
                sha256_tree(skill_source),
                [service_blocker or "%s CLI is unavailable or its version cannot be read" % args.agent],
                cli_path,
                cli_version,
            )
        else:
            record = case_record(case, args.agent, agent_executable, agent_version, skill_source, fixtures_dir,
                                 cli_path, cli_version, cli_version_proof, args.timeout)
            service_blocker = record.get('environment_blocker')
        record_path = run_dir / (case_id + ".json")
        write_json_exclusive(record_path, record)
        manifest_records.append({"case_id": case_id, "file": record_path.name, "sha256": sha256_file(record_path), "status": record["status"]})
        statuses.append(record["status"])

    manifest = {
        "schema_version": 1,
        "evaluation_type": "live-manifest",
        "created_at": utc_now(),
        "agent": args.agent,
        "suite": args.suite,
        "selected_cases": list(selected),
        "suite_complete": set(selected) == set(suite_case_ids(args.suite, catalogs)),
        "records": manifest_records,
    }
    write_json_exclusive(run_dir / "manifest.json", seal_record(manifest))
    print(json.dumps({"run_dir": str(run_dir), "statuses": {status: statuses.count(status) for status in sorted(set(statuses))}}, sort_keys=True))
    if 'FAIL' in statuses:
        return 1
    return 2 if any(status in {'BLOCKED', 'NOT_RUN'} for status in statuses) else 0


def main() -> None:
    try:
        raise SystemExit(run())
    except EvaluationError as exc:
        print("eval-live: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
