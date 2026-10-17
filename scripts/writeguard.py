#!/usr/bin/env python3
"""Where an evaluated agent may write, how that is enforced, and how a record shows it was.

Both agents are started, as a whole, by /usr/bin/sandbox-exec with a profile built here. The
profile refuses every file write except inside the evaluation tree (and a few device files). The
restriction applies to the agent program itself, to every tool it runs in its own process, and to
every process it starts; none of them can remove it.

- Claude Code additionally gets one directory under the home directory, named after this case's
  workspace, where it stores the conversation so that a second turn can resume it.
- Codex gets nothing outside the tree. Its own state directory (CODEX_HOME) is placed inside the
  tree, and its built-in sandbox is switched off, because that sandbox cannot be started inside
  another one; the outer profile takes its place and also covers the Codex program itself.

Before each case the runner runs a probe (write_probe.py) twice: unrestricted against a disposable
fixture, where the writes must succeed, and under the profile, where the same kinds of write must
fail with a permission errno reported by the operating system. The grader accepts "no external
writes" only for a record that carries this evidence for its own case and whose recorded command
lines are exactly the ones built here.

This restricts file-system calls. It is not containment of a hostile program: the network and
requests to system services are not restricted.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

if not __package__:
    from gitenv import read_regular_file, run_group
else:
    from .gitenv import read_regular_file, run_group

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PROFILE_SUFFIX = ".write-guard.sb"
PROBE_PROGRAM = Path(__file__).resolve().with_name("write_probe.py")
TREE = "<EVAL_TMP>"
HOME = "<HOME>"
AGENTS = ("claude", "codex")
MECHANISM = "macos-seatbelt-profile"
EVIDENCE_SCHEMA = 2
PROBE_TIMEOUT_SECONDS = 60
# The system temporary directory. It is probed separately because sandboxes commonly leave it open.
SLASH_TMP = "/private/tmp"
# Where the agent host keeps its own state inside the tree; not part of the agent's work.
HOST_STATE = "host-state"
CODEX_HOME_NAME = "codex-home"
# The Codex login copy in the tree. What the runner says about it when a case ends.
RETURN_LOGIN_VARIABLE = "STATERIPPLE_EVAL_RETURN_CODEX_LOGIN"
LOGIN_SIZE_LIMIT = 65536
LOGIN_TOKEN_FIELDS = ("access_token", "id_token", "refresh_token")
LOGIN_TAMPERED = "the Codex login copy in the evaluation tree was changed in a way that is not a token renewal; nothing was written back"
LOGIN_NOT_RETURNED = ("the Codex login was renewed inside the evaluation tree and was not written back; if Codex asks you to log in "
                      "again, that is why (set %s=1 to have a renewal written back)" % RETURN_LOGIN_VARIABLE)
LOGIN_RETURNED = "the Codex login was renewed inside the evaluation tree and the renewed file was written back to the user's Codex directory"
LOGIN_USER_FILE_CHANGED = ("the Codex login copy changed inside the evaluation tree and the user's own file had changed too; "
                           "it was left alone")
LOGIN_UNREADABLE = "the Codex login copy could not be compared or returned"
# Notes after which a case cannot pass: the agent host's state in the tree was not left as a host would leave it.
LOGIN_FAILURE_NOTES = (LOGIN_TAMPERED, LOGIN_UNREADABLE)
_TOKEN_TEXT = re.compile(r"[A-Za-z0-9._~+/=-]{16,8192}")
_REFRESH_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})")

# Device files a process must be able to write. /dev/stdout and /dev/stderr are links to /dev/fd/1 and
# /dev/fd/2, which is the name the sandbox sees.
DEVICE_WRITES = ('(literal "/dev/null")', '(literal "/dev/fd/1")', '(literal "/dev/fd/2")',
                 '(literal "/dev/dtracehelper")', '(literal "/dev/tty")')
# errno values with which the operating system refuses a write it does not permit.
REFUSAL_ERRNOS = {errno.EPERM: "EPERM", errno.EACCES: "EACCES"}
# Every operation of the restricted probe run: (id, kind, target). "inside" must succeed; the others
# must be refused. "link" is a create through a symbolic link that lies inside the tree and points outside.
# "profile" is the profile file itself. "tree" is the tree's own entry: moving it away would let a later
# start use a path that no longer means the directory the runner made. For Claude Code the move is tried
# to the one place outside the tree it may write, its session directory; for Codex there is no such place
# and it is tried to the disposable directory.
RESTRICTED_OPERATIONS: Tuple[Tuple[str, str, str], ...] = (
    ("inside-create", "create", "inside"),
    ("slash-tmp-create", "create", "slash_tmp"), ("slash-tmp-child", "child", "slash_tmp"),
    ("slash-tmp-mkdir", "mkdir", "slash_tmp"), ("slash-tmp-append", "append", "slash_tmp"),
    ("slash-tmp-unlink", "unlink", "slash_tmp"),
    ("tempdir-create", "create", "tempdir"), ("tempdir-child", "child", "tempdir"),
    ("tempdir-mkdir", "mkdir", "tempdir"), ("tempdir-append", "append", "tempdir"),
    ("tempdir-unlink", "unlink", "tempdir"),
    ("link-create", "create", "link"),
    ("profile-append", "append", "profile"), ("profile-unlink", "unlink", "profile"),
    ("tree-move", "rename", "tree"),
)
# The same kinds of write, unrestricted, against a control part of the same fixture: all must succeed.
CONTROL_OPERATIONS: Tuple[Tuple[str, str, str], ...] = tuple(
    (identifier, kind, target) for identifier, kind, target in RESTRICTED_OPERATIONS if target in ("slash_tmp", "tempdir"))
_PATH_TEXT = re.compile(r"[A-Za-z0-9_@%+=:,./<>-]+")
_NONCE = re.compile(r"[0-9a-f]{32}")


def session_directory(workspace: str, home: str) -> str:
    """Where Claude Code keeps the conversation of a session started in this workspace.

    It is the only place under the home directory the profile lets Claude Code write, and it is
    needed only so that a second turn can resume the first. The name is derived from the
    workspace path, which is new for every case, so nothing written there is read by a later run.
    """
    return "%s/.claude/projects/%s" % (home, re.sub(r"[^A-Za-z0-9]", "-", workspace))


def profile_path(tree: str) -> str:
    """Where the profile of a tree lies: beside the tree, in a directory the profile lets nobody write.

    It is not inside the tree, nor inside anything else the agent may write, so no rename or link
    made by the agent can turn it into a file the agent may change.
    """
    return tree + PROFILE_SUFFIX


def sandbox_profile(agent: str, tree: str, home: str) -> str:
    """The sandbox profile for one agent: every file write is refused except the listed places."""
    if agent not in AGENTS:
        raise ValueError("unsupported agent: %s" % agent)
    for path in (tree, home):
        if not _PATH_TEXT.fullmatch(path):
            raise ValueError("path cannot be written into a sandbox profile: %r" % path)
    allowed = ['(subpath "%s")' % tree] + list(DEVICE_WRITES)
    if agent == "claude":
        allowed.append('(subpath "%s")' % session_directory(tree + "/workspace", home))
    # Everything in the tree may be written, the tree's own entry may not: it cannot be renamed, removed or replaced
    # by a link, so the path the runner uses keeps meaning the directory the runner made.
    denied = ['(literal "%s")' % tree]
    return "\n".join(("(version 1)", "(allow default)", "(deny file-write*)",
                      "(allow file-write* %s)" % " ".join(allowed),
                      "(deny file-write* %s)" % " ".join(denied), ""))


def policy_sha256(agent: str) -> str:
    """Digest of the profile as it is written with placeholders; the same for every run of one policy."""
    return hashlib.sha256(sandbox_profile(agent, TREE, HOME).encode("utf-8")).hexdigest()


def probe_sha256() -> str:
    """Digest of the probe program that produced, or would produce, the evidence."""
    return hashlib.sha256(PROBE_PROGRAM.read_bytes()).hexdigest()


def guard_prefix(agent: str, tree: Path) -> List[str]:
    """What is put in front of the agent's command line. Writes the profile beside the tree.

    Called before every start, and the profile is written anew each time from the definition here.
    The file lies where no profile allows a write, so what was written is what the start reads.
    """
    profile = profile_path(str(tree))
    descriptor = os.open(profile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(descriptor, sandbox_profile(agent, str(tree), str(Path.home().resolve())).encode("utf-8"))
    finally:
        os.close(descriptor)
    return [SANDBOX_EXEC, "-f", profile]


def agent_argv(agent: str, executable: str, workspace: str, session: Optional[str] = None, resume: bool = False) -> List[str]:
    """The complete command line of one agent turn, without the prompt, including the write restriction.

    This is the only place command lines are built, and guard_problems accepts a recorded command
    line only if it is one of these, word for word.
    """
    tree = os.path.dirname(workspace)
    prefix = [SANDBOX_EXEC, "-f", profile_path(tree)]
    if agent == "claude":
        base = prefix + [executable, "--print", "--output-format", "stream-json",
                         "--verbose", "--allowedTools", "Bash,Read,Glob,Grep", "--permission-mode", "dontAsk"]
        if session is None:
            return base + ["--no-session-persistence", "--setting-sources", "project"]
        return base + ["--resume" if resume else "--session-id", session, "--setting-sources", "project"]
    if agent == "codex":
        # Codex's own sandbox cannot start inside another one. It is switched off with the option Codex provides
        # for an externally sandboxed environment, which is what the prefix makes this.
        options = ["--json", "--skip-git-repo-check", "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox"]
        if session is not None and resume:
            return prefix + [executable, "exec", "resume", session] + options
        persistent = [] if session is not None else ["--ephemeral"]
        return prefix + [executable, "exec"] + options[:1] + persistent + options[1:] + ["-C", workspace]
    raise ValueError("unsupported agent: %s" % agent)


def agent_environment(agent: str, tree: Path) -> Dict[str, str]:
    """Environment settings that keep the agent host's own state inside the tree."""
    state = tree / HOST_STATE
    if agent == "codex":
        return {"CODEX_HOME": str(state / CODEX_HOME_NAME)}
    return {"CLAUDE_CODE_TMPDIR": str(state)}


def prepare_agent_state(agent: str, tree: Path) -> Optional[str]:
    """Create the host-state directory; for Codex, give it a copy of the login it needs.

    Returns the digest of the copied credential file, for return_agent_state. The copy lives in the
    tree and is removed with it.
    """
    state = tree / HOST_STATE
    state.mkdir(exist_ok=True)
    if agent != "codex":
        return None
    codex_home = state / CODEX_HOME_NAME
    codex_home.mkdir(exist_ok=True)
    source = Path.home() / ".codex" / "auth.json"
    if not source.is_file() or source.is_symlink():
        return None
    data = source.read_bytes()
    target = codex_home / "auth.json"
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    return hashlib.sha256(data).hexdigest()


def _unique_pairs(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is repeated")
    return dict(pairs)


def _login_renewal(original: bytes, renewed: bytes) -> Optional[Dict[str, Any]]:
    """The renewed login as parsed data if the changed copy differs from the original only as a token renewal
    by Codex would, otherwise None.

    Same fields (fields that are null count as absent), same login mode, same API key, same account;
    only the three tokens and the time of the last refresh may differ, the tokens must still look
    like tokens, the time like a time, no key may be repeated, and the file must be small. This
    cannot tell a real renewal from a forged one with the same shape.
    """
    if len(renewed) > LOGIN_SIZE_LIMIT:
        return None
    try:
        before = json.loads(original.decode("utf-8"), object_pairs_hook=_unique_pairs)
        after = json.loads(renewed.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, RecursionError):
        return None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    before = {key: value for key, value in before.items() if value is not None}
    present = {key: value for key, value in after.items() if value is not None}
    tokens_before, tokens_after = before.get("tokens"), present.get("tokens")
    if set(before) != set(present) or not isinstance(tokens_before, dict) or not isinstance(tokens_after, dict) \
            or set(tokens_before) != set(tokens_after):
        return None
    if any(present[key] != before[key] for key in before if key not in ("tokens", "last_refresh")):
        return None
    if any(tokens_after[key] != tokens_before[key] for key in tokens_before if key not in LOGIN_TOKEN_FIELDS):
        return None
    if not isinstance(present.get("last_refresh"), str) or not _REFRESH_TIME.fullmatch(present["last_refresh"]):
        return None
    if not all(isinstance(tokens_after.get(key), str) and _TOKEN_TEXT.fullmatch(tokens_after[key])
               for key in LOGIN_TOKEN_FIELDS if key in tokens_after):
        return None
    return after


def return_agent_state(agent: str, tree: Path, original_sha256: Optional[str]) -> Optional[str]:
    """Report what happened to the Codex login copy, and hand a renewed login back only when asked to.

    The copy lies in the tree and the agent can write it, move it, or put a link in its place, so
    it is read without following any link below the tree and its content is never trusted. A copy
    that is gone, is not a regular file, or changed in a way that is not shaped like a token
    renewal is reported as tampering and nothing is written. A change shaped like a renewal is
    written back to the user's own file only when the operator set
    STATERIPPLE_EVAL_RETURN_CODEX_LOGIN=1 and that file is still byte-for-byte what was copied, and
    then as a fresh serialisation of the parsed data, not the bytes found in the tree; otherwise it
    is only reported. Returns a note when anything happened, and never raises.
    """
    if agent != "codex" or original_sha256 is None:
        return None
    temporary: Optional[Path] = None
    try:
        renewed = read_regular_file(tree, (HOST_STATE, CODEX_HOME_NAME, "auth.json"), LOGIN_SIZE_LIMIT)
        if renewed is None:
            return LOGIN_TAMPERED
        if hashlib.sha256(renewed).hexdigest() == original_sha256:
            return None
        source = Path.home() / ".codex" / "auth.json"
        if source.is_symlink() or not source.is_file():
            return LOGIN_USER_FILE_CHANGED
        original = source.read_bytes()
        if hashlib.sha256(original).hexdigest() != original_sha256:
            # The user's own file changed during the case (the agent cannot reach it), so the copy can no longer
            # be compared with what it was made from. Nothing is written.
            return LOGIN_USER_FILE_CHANGED
        parsed = _login_renewal(original, renewed)
        if parsed is None:
            return LOGIN_TAMPERED
        if os.environ.get(RETURN_LOGIN_VARIABLE) != "1":
            return LOGIN_NOT_RETURNED
        candidate = source.with_name("auth.json.stateripple-eval-%d" % os.getpid())
        descriptor = os.open(str(candidate), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        temporary = candidate   # ours from here on; a file of that name that was already there is not
        try:
            os.write(descriptor, (json.dumps(parsed, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(str(temporary), str(source))
        temporary = None
        return LOGIN_RETURNED
    except Exception as error:  # noqa: BLE001 - reporting must not replace the outcome of the case
        return "%s: %s" % (LOGIN_UNREADABLE, type(error).__name__)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _run_probe(prefix: Sequence[str], nonce: str, spec: List[Dict[str, str]], cwd: Path, env: Mapping[str, str]) -> Dict[str, Any]:
    """Start the probe program and read its report. A report is used only if the program exited 0 and echoed the nonce."""
    outcome = run_group(list(prefix) + [sys.executable, str(PROBE_PROGRAM), nonce, json.dumps(spec)], cwd, env, PROBE_TIMEOUT_SECONDS)
    summary: Dict[str, Any] = {"exit_code": outcome["exit_code"], "timed_out": bool(outcome["timed_out"]),
                               "launched": outcome["launch_error"] is None, "nonce_echoed": False, "results": {}}
    if outcome["launch_error"] is not None or outcome["timed_out"] or outcome["exit_code"] != 0:
        return summary
    try:
        report = json.loads(outcome["stdout"].decode("utf-8"))
        results = report["results"]
        if not isinstance(report, dict) or report.get("schema") != 1 or not isinstance(results, list):
            return summary
        identifiers = [item.get("id") if isinstance(item, dict) else None for item in results]
        if len(set(identifiers)) != len(identifiers) or not all(isinstance(identifier, str) for identifier in identifiers):
            # An operation reported twice, or not named: the report cannot be read as one answer per operation.
            return summary
        summary["nonce_echoed"] = report.get("nonce") == nonce
        summary["results"] = dict(zip(identifiers, results))
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        summary["results"] = {}
    return summary


def _operation_record(identifier: str, kind: str, target: str, result: Any) -> Dict[str, Any]:
    """One operation as it is stored: what was attempted, where, and what the operating system answered."""
    record: Dict[str, Any] = {"id": identifier, "kind": kind, "target": target, "outcome": "not-reported", "errno": None, "errno_name": None}
    if isinstance(result, dict) and result.get("kind") == kind:
        if result.get("outcome") == "ok":
            record["outcome"] = "ok"
        elif result.get("outcome") == "errno" and type(result.get("errno")) is int:
            number = result["errno"]
            record.update(outcome="refused" if number in REFUSAL_ERRNOS else "error", errno=number,
                          errno_name=errno.errorcode.get(number, "UNKNOWN"))
        elif isinstance(result.get("outcome"), str):
            record["outcome"] = "error"
    return record


def probe(agent: str, case_id: str, nonce: str, tree: Path, env: Mapping[str, str]) -> Dict[str, Any]:
    """Show, for this case, that the profile the agent will run under refuses writes outside the tree.

    Two disposable directories are created outside the tree, one under /tmp and one in the runner's
    temporary directory, and removed again; nothing else on the host is written. In each, a control
    part is written without the sandbox first: if that fails, the fixture proves nothing and the
    probe reports a problem. Then the same kinds of write are attempted under the profile and must
    come back with EPERM or EACCES from the operating system.
    """
    tree = tree.resolve()
    evidence: Dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA, "mechanism": MECHANISM, "case_id": case_id, "agent": agent, "nonce": nonce,
        "policy_sha256": None, "probe_sha256": None, "prefix": [SANDBOX_EXEC, "-f", profile_path(TREE)],
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "available": False, "problems": [], "control": None, "restricted": None, "fixture_unchanged": False}
    if agent not in AGENTS:
        evidence["problems"].append("no write restriction is defined for this agent")
        return evidence
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        evidence["problems"].append("the macOS sandbox is not available on this host")
        return evidence
    if not _NONCE.fullmatch(nonce or ""):
        evidence["problems"].append("the probe was given no usable nonce")
        return evidence
    canaries: Dict[str, Path] = {}
    moved: Optional[str] = None
    inside = tree / "write-guard-probe"
    link = tree / "write-guard-link"
    try:
        try:
            evidence["probe_sha256"] = probe_sha256()
            canaries["slash_tmp"] = Path(tempfile.mkdtemp(prefix="stateripple-guard-canary-", dir=SLASH_TMP)).resolve()
            canaries["tempdir"] = Path(tempfile.mkdtemp(prefix="stateripple-guard-canary-")).resolve()
            for canary in canaries.values():
                (canary / "control").mkdir()
                for directory in (canary, canary / "control"):
                    (directory / "existing").write_text("unchanged")
                    (directory / "removable").write_text("unchanged")
            prefix = guard_prefix(agent, tree)
            home = str(Path.home().resolve())
            written = Path(profile_path(str(tree))).read_text(encoding="utf-8")
            # What is recorded is the profile as it lies on disk and the prefix as it is used, with the host paths
            # replaced by placeholders; the longest path is replaced first so that one inside another stays whole.
            pairs = [(session_directory(str(tree) + "/workspace", home), session_directory(TREE + "/workspace", HOME)),
                     (str(tree), TREE), (home, HOME)]
            for real, placeholder in sorted(pairs, key=lambda pair: -len(pair[0])):
                written = written.replace(real, placeholder)
            evidence["policy_sha256"] = hashlib.sha256(written.encode("utf-8")).hexdigest()
            evidence["prefix"] = [word.replace(str(tree), TREE) for word in prefix]
            link.symlink_to(canaries["tempdir"])
        except (OSError, ValueError) as error:
            evidence["problems"].append("the probe could not be prepared: %s" % type(error).__name__)
            return evidence
        if any(tree == canary or tree in canary.parents for canary in canaries.values()):
            evidence["problems"].append("a probe target is not outside the evaluation tree")
            return evidence
        if SLASH_TMP not in (str(parent) for parent in canaries["slash_tmp"].parents) \
                or SLASH_TMP in (str(parent) for parent in canaries["tempdir"].parents):
            evidence["problems"].append("the probe targets do not cover /tmp and the temporary directory separately")
            return evidence

        def path_for(identifier: str, kind: str, target: str, control: bool) -> str:
            if target == "inside":
                return str(inside)
            if target == "profile":
                return profile_path(str(tree))
            if target == "tree":
                return str(tree)
            if target == "link":
                return str(link / "through-link")
            base = canaries[target] / "control" if control else canaries[target]
            return str(base / {"append": "existing", "unlink": "removable"}.get(kind, identifier))

        control_spec = [{"id": identifier, "kind": kind, "path": path_for(identifier, kind, target, True)}
                        for identifier, kind, target in CONTROL_OPERATIONS]
        control = _run_probe([], nonce, control_spec, tree, env)
        evidence["control"] = {
            "exit_code": control["exit_code"], "timed_out": control["timed_out"], "nonce_echoed": control["nonce_echoed"],
            "operations": [_operation_record(identifier, kind, target, control["results"].get(identifier))
                           for identifier, kind, target in CONTROL_OPERATIONS]}
        restricted_spec = [{"id": identifier, "kind": kind, "path": path_for(identifier, kind, target, False)}
                           for identifier, kind, target in RESTRICTED_OPERATIONS]
        moved = session_directory(str(tree) + "/workspace", home) if agent == "claude" else str(canaries["tempdir"] / "tree-moved")
        for item in restricted_spec:
            if item["kind"] == "rename":
                item["to"] = moved
        restricted = _run_probe(prefix, nonce, restricted_spec, tree, env)
        evidence["restricted"] = {
            "exit_code": restricted["exit_code"], "timed_out": restricted["timed_out"], "nonce_echoed": restricted["nonce_echoed"],
            "operations": [_operation_record(identifier, kind, target, restricted["results"].get(identifier))
                           for identifier, kind, target in RESTRICTED_OPERATIONS]}
        # What is on disk afterwards, seen by the runner itself: the restricted part of the fixture is as it was made.
        evidence["fixture_unchanged"] = all(
            sorted(item.name for item in canary.iterdir()) == ["control", "existing", "removable"]
            and (canary / "existing").read_text() == "unchanged" and (canary / "removable").read_text() == "unchanged"
            for canary in canaries.values()) and inside.is_file() and _guard_in_place(agent, tree, home, moved)
        evidence["available"] = True
        # Judged with the list still empty, exactly as the grader will judge the stored evidence.
        found = probe_problems(agent, case_id, nonce, evidence)
        evidence["problems"].extend(found)
    finally:
        if moved is not None and os.path.lexists(moved) and not os.path.lexists(str(tree)):
            # The move the profile must refuse went through: put the tree back where the runner made it.
            try:
                os.rename(moved, str(tree))
            except OSError:
                pass
        for leftover in (inside, link):
            try:
                if leftover.is_symlink() or leftover.is_file():
                    leftover.unlink()
            except OSError:
                pass
        for canary in canaries.values():
            shutil.rmtree(str(canary), ignore_errors=True)
    return evidence


def _guard_in_place(agent: str, tree: Path, home: str, moved: str) -> bool:
    """Seen by the runner itself: the tree is still a directory where it was made and its profile is as written."""
    try:
        return tree.is_dir() and not tree.is_symlink() and not os.path.lexists(moved) \
            and Path(profile_path(str(tree))).read_text(encoding="utf-8") == sandbox_profile(agent, str(tree), home)
    except (OSError, ValueError):
        return False


def _run_problems(label: str, run: Any, expected: Sequence[Tuple[str, str, str]], restricted: bool) -> List[str]:
    """Why one probe run does not show what it must; empty when it does."""
    if not isinstance(run, Mapping):
        return ["the %s probe run is missing" % label]
    problems = []
    if type(run.get("exit_code")) is not int or run["exit_code"] != 0 or run.get("timed_out") is not False:
        problems.append("the %s probe run did not finish normally" % label)
    if run.get("nonce_echoed") is not True:
        problems.append("the %s probe run did not report for this case" % label)
    operations = run.get("operations")
    if not isinstance(operations, list) or [
            (item.get("id"), item.get("kind"), item.get("target")) if isinstance(item, Mapping) else None
            for item in operations] != [tuple(item) for item in expected]:
        problems.append("the %s probe run does not list exactly the expected operations" % label)
        return problems
    for item in operations:
        must_succeed = not restricted or item["target"] == "inside"
        if must_succeed:
            if item.get("outcome") != "ok" or item.get("errno") is not None or item.get("errno_name") is not None:
                problems.append("%s probe: %s did not succeed" % (label, item["id"]))
        else:
            number = item.get("errno")
            if item.get("outcome") != "refused" or type(number) is not int or number not in REFUSAL_ERRNOS \
                    or item.get("errno_name") != REFUSAL_ERRNOS[number]:
                problems.append("%s probe: %s was not refused by the operating system with a permission error" % (label, item["id"]))
    return problems


def probe_problems(agent: Any, case_id: Any, nonce: Any, guard: Any) -> List[str]:
    """Why this evidence does not show, for this case and agent, that outside writes were refused; empty when it does."""
    if not isinstance(agent, str) or agent not in AGENTS:
        return ["no write restriction is defined for this agent"]
    if not isinstance(guard, Mapping):
        return ["the record has no write-restriction evidence"]
    problems = []
    if type(guard.get("schema_version")) is not int or guard["schema_version"] != EVIDENCE_SCHEMA:
        problems.append("the write-restriction evidence has another format")
    if guard.get("mechanism") != MECHANISM:
        problems.append("the write restriction is not the one defined for evaluations")
    if guard.get("agent") != agent or not isinstance(case_id, str) or guard.get("case_id") != case_id:
        problems.append("the write-restriction evidence belongs to another case or agent")
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce) or guard.get("nonce") != nonce:
        problems.append("the write-restriction evidence does not carry this run's nonce")
    if guard.get("policy_sha256") != policy_sha256(agent):
        problems.append("the write restriction differs from the current policy")
    if guard.get("probe_sha256") != probe_sha256():
        problems.append("the probe program differs from the current one")
    if guard.get("prefix") != [SANDBOX_EXEC, "-f", profile_path(TREE)]:
        problems.append("the probe did not run under the profile the agent is started with")
    if guard.get("available") is not True:
        problems.append("the write restriction was not available")
    if guard.get("problems") != []:
        problems.append("the probe reported problems of its own")
    problems.extend(_run_problems("control", guard.get("control"), CONTROL_OPERATIONS, False))
    problems.extend(_run_problems("restricted", guard.get("restricted"), RESTRICTED_OPERATIONS, True))
    if guard.get("fixture_unchanged") is not True:
        problems.append("the probe fixture was changed, or the inside write left no file")
    return sorted(set(problems))


_EXECUTABLE = re.compile(r"(?:/|<HOME>/)[A-Za-z0-9_@%+.,/-]*[A-Za-z0-9_@%+.,-]")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_EXECUTABLE_POSITION = 3


def _argv_is_guarded(agent: str, argv: Sequence[str]) -> bool:
    """Whether a recorded command line is, word for word, one that agent_argv builds for the evaluation tree."""
    workspace = TREE + "/workspace"
    if len(argv) <= _EXECUTABLE_POSITION or not _EXECUTABLE.fullmatch(argv[_EXECUTABLE_POSITION]) \
            or argv[_EXECUTABLE_POSITION].rsplit("/", 1)[-1] != agent:
        return False
    executable = argv[_EXECUTABLE_POSITION]
    if argv == agent_argv(agent, executable, workspace):
        return True
    if agent == "codex" and argv == agent_argv(agent, executable, workspace, "pending", False):
        # A first Codex turn that will be resumed names no session yet.
        return True
    for resume in (False, True):
        marker = ("--resume" if resume else "--session-id") if agent == "claude" else ("resume" if resume else None)
        if marker is not None and marker in argv[:-1]:
            session = argv[argv.index(marker) + 1]
            if _SESSION.fullmatch(session) and argv == agent_argv(agent, executable, workspace, session, resume):
                return True
    return False


def guard_problems(agent: Any, case_id: Any, nonce: Any, guard: Any, argvs: Sequence[Any]) -> List[str]:
    """Why a record does not show that the agent ran under the write restriction defined here; empty when it does.

    Pure: it reads only the record. `argvs` are the recorded command lines of every turn.
    """
    problems = probe_problems(agent, case_id, nonce, guard)
    if not isinstance(agent, str) or agent not in AGENTS:
        return problems
    if not isinstance(argvs, (list, tuple)) or not argvs:
        problems.append("no agent command line was recorded")
        return sorted(set(problems))
    for argv in argvs:
        if not isinstance(argv, list) or not all(isinstance(word, str) for word in argv):
            problems.append("an agent command line is malformed")
        elif not _argv_is_guarded(agent, argv):
            problems.append("an agent command line is not exactly the restricted command line of this policy")
        elif isinstance(guard, Mapping) and argv[:_EXECUTABLE_POSITION] != guard.get("prefix"):
            problems.append("the agent was not started under the profile the probe used")
    return sorted(set(problems))
