#!/usr/bin/env python3
"""One definition of how this project starts child processes: their environment and their cleanup.

Every child process started by the build, release-check, CLI compatibility and evaluation scripts
receives an environment built here, so that settings injected through the caller's environment
(GIT_CONFIG_COUNT, GIT_CONFIG_KEY_*, GIT_CONFIG_PARAMETERS, GIT_DIR, GIT_EXTERNAL_DIFF, ...),
the user's global and system configuration, hooks, file monitors, pagers and credential helpers
cannot run or change what a test observes. Children started through run_group or run_bounded
are also ended, with their whole process group, and collected within a bound.
"""

from __future__ import annotations

import errno
import os
import signal
import stat
import subprocess
import uuid
import sys
import tempfile
import time
from typing import Dict, Mapping, Optional, Tuple

# Variables that are never inherited. Every GIT_* variable is dropped as well; the ones this
# module needs are set again explicitly below.
STRIPPED_VARIABLES = frozenset((
    "SSH_ASKPASS", "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN",
    "PAGER", "EDITOR", "VISUAL", "LESSOPEN", "LESSCLOSE",
))

# Configuration applied to every Git process through the environment, so it also reaches Git
# started by grandchildren (unit tests, fixtures, the StateRipple CLI). Each value switches a
# program-running feature off.
SAFE_CONFIG: Tuple[Tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("core.sshCommand", "/usr/bin/false"),
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("protocol.ext.allow", "never"),
    ("submodule.recurse", "false"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("init.defaultBranch", "main"),
)

FIXED_VARIABLES = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GIT_ASKPASS": "/usr/bin/false",
    "GIT_SSH_COMMAND": "/usr/bin/false",
    "GIT_ALLOW_PROTOCOL": "file",
    "GIT_EDITOR": "true",
}

# Default bounds for child processes started with this environment.
GIT_TIMEOUT_SECONDS = 60
CHECK_TIMEOUT_SECONDS = 600
# How long cleanup waits for a killed child.
REAP_TIMEOUT_SECONDS = 5


# macOS values for waitid(2); Python offers os.waitid only on some platforms and versions.
_DARWIN_P_PID, _DARWIN_WEXITED, _DARWIN_WNOWAIT, _DARWIN_WNOHANG = 1, 0x4, 0x20, 0x1
_DARWIN_SI_PID_OFFSET = 12


def _peek_exit():
    """A function telling whether a child has exited without collecting it, or None where that cannot be asked."""
    if os.name != "posix":
        return None
    if hasattr(os, "waitid"):
        def peek(pid: int) -> bool:
            return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
        return peek
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        if not hasattr(libc, "waitid"):
            return None

        def peek(pid: int) -> bool:
            info = ctypes.create_string_buffer(256)
            if libc.waitid(_DARWIN_P_PID, pid, info, _DARWIN_WEXITED | _DARWIN_WNOWAIT | _DARWIN_WNOHANG) != 0:
                error = ctypes.get_errno()
                raise ChildProcessError(error, os.strerror(error)) if error == errno.ECHILD else OSError(error, os.strerror(error))
            return int.from_bytes(info.raw[_DARWIN_SI_PID_OFFSET:_DARWIN_SI_PID_OFFSET + 4], sys.byteorder) == pid
        return peek
    return None


# How deep remove_tree descends before it moves a directory up instead, and how much of a command's output is kept.
REMOVAL_DEPTH = 48
OUTPUT_LIMIT = 256 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


def open_directory(path, dir_fd: Optional[int] = None) -> int:
    """Open a directory itself, never something a link at that name points to. Raises OSError otherwise."""
    return os.open(str(path), os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)


def read_regular_file(root, parts, limit: int) -> Optional[bytes]:
    """The bytes of root/parts[0]/.../parts[-1], or None unless every step is a real directory and the last a
    regular file of at most `limit` bytes. No link is followed at any step below `root`."""
    descriptors = []
    try:
        descriptors.append(open_directory(root))
        for name in parts[:-1]:
            descriptors.append(open_directory(name, descriptors[-1]))
        handle = os.open(parts[-1], os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptors[-1])
        descriptors.append(handle)
        status = os.fstat(handle)
        if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
            return None
        data = os.read(handle, limit + 1)
        return data if len(data) <= limit else None
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def remove_tree(root) -> bool:
    """Remove a directory tree someone else may have filled, without following a link and without raising.

    Every directory is opened by name relative to its already opened parent with O_NOFOLLOW, so a
    link placed anywhere in the tree is removed as a link and what it points to is never opened,
    changed, or deleted. Modes are changed only on real directories inside the tree. A directory
    nested deeper than REMOVAL_DEPTH is first moved up to the top of the tree, so neither the
    number of open directories nor the recursion grows with the depth someone built. Returns
    whether the tree is gone; entries that cannot be removed (for example flagged immutable) stay.
    """
    try:
        # The tree's own mode, changed on the entry itself: on a link this changes the link, never its target.
        os.chmod(str(root), 0o700, follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass
    try:
        top = open_directory(root)
    except OSError:
        return not os.path.lexists(str(root))

    def clear(descriptor: int, depth: int) -> int:
        """Remove what can be removed below one open directory; returns how many entries were removed or moved up."""
        changed = 0
        for name in os.listdir(descriptor):
            try:
                status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(status.st_mode):
                try:
                    os.unlink(name, dir_fd=descriptor)
                    changed += 1
                except OSError:
                    pass
                continue
            try:
                os.chmod(name, 0o700, dir_fd=descriptor, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
            if depth >= REMOVAL_DEPTH:
                try:
                    os.rename(name, ".removal-%s" % uuid.uuid4().hex, src_dir_fd=descriptor, dst_dir_fd=top)
                    changed += 1
                except OSError:
                    pass
                continue
            try:
                child = open_directory(name, descriptor)
            except OSError:
                continue
            try:
                changed += clear(child, depth + 1)
            finally:
                os.close(child)
            try:
                os.rmdir(name, dir_fd=descriptor)
                changed += 1
            except OSError:
                pass
        return changed
    try:
        try:
            os.fchmod(top, 0o700)
        except OSError:
            pass
        # Each pass removes everything down to the depth limit and lifts what lies deeper; it ends when nothing moves.
        while clear(top, 0) and os.listdir(top):
            pass
    except (OSError, RecursionError):
        pass
    finally:
        os.close(top)
    try:
        os.rmdir(str(root))
    except OSError:
        pass
    return not os.path.lexists(str(root))


class ChildLost(Exception):
    """The child was collected by something other than this module."""


def _collect(process: "subprocess.Popen", timeout: float) -> str:
    """Collect the child within a bound: "exited", "timeout", or "lost" when something else collected it.

    subprocess reports a child it can no longer find as exit status 0. Here that case is kept apart:
    the status is unknown, and the caller must not take it for success.
    """
    if process.returncode is not None:
        return "lost" if getattr(process, "collected_elsewhere", False) else "exited"
    if os.name != "posix":
        try:
            process.wait(timeout=timeout)
            return "exited"
        except subprocess.TimeoutExpired:
            return "timeout"
    deadline = time.monotonic() + timeout
    pause = 0.005
    while True:
        try:
            pid, status = os.waitpid(process.pid, os.WNOHANG)
        except ChildProcessError:
            process.returncode = -1
            process.collected_elsewhere = True
            return "lost"
        if pid == process.pid:
            process.returncode = -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)
            return "exited"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        time.sleep(min(pause, remaining))
        pause = min(pause * 2, 0.05)


def _wait_for_exit(process: "subprocess.Popen", timeout: float) -> bool:
    """Wait until the child has exited, without collecting it; False when the time is up.

    The child is left as a zombie on purpose: until it is collected its process id, and therefore
    the id of the process group it leads, cannot be given to another process. Where the platform
    cannot report an exit without collecting the child, the child is collected here, and
    end_process_group then signals nothing.
    """
    peek = _peek_exit()
    if peek is None:
        outcome = _collect(process, timeout)
        if outcome == "lost":
            raise ChildLost("the child was collected elsewhere; its exit status is unknown")
        return outcome == "exited"
    deadline = time.monotonic() + timeout
    pause = 0.005
    while True:
        try:
            if peek(process.pid):
                return True
        except ChildProcessError:
            # Something else collected the child (for example because the caller ignores SIGCHLD). Its id is no
            # longer held, so nothing may be signalled later, and its exit status is unknown, never a success.
            process.returncode = -1
            process.collected_elsewhere = True
            raise ChildLost("the child was collected elsewhere; its exit status is unknown")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(pause, remaining))
        pause = min(pause * 2, 0.05)


def end_process_group(process: "subprocess.Popen") -> None:
    """Kill the child's process group and collect the child, within a bound. Never raises; safe to repeat.

    The group is signalled only while the child has not been collected (it is running or a zombie),
    so the group id still belongs to this child. Once the child is collected nothing is signalled:
    its id may already belong to an unrelated process.
    """
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        _collect(process, REAP_TIMEOUT_SECONDS)
    except OSError:
        pass


def run_group(arguments, cwd, env: Optional[Mapping[str, str]], timeout: float) -> Dict[str, object]:
    """Run one command as the leader of its own process group and end the whole group when it is done.

    Output goes to unnamed temporary files, not pipes, so a process that outlives the command cannot
    hold the runner. Returns exit_code (None after a timeout or a failed launch), timed_out,
    launch_error (None or text), stdout and stderr as bytes. The group is ended and the child
    collected on every path, including an interrupt, which is re-raised afterwards.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(list(arguments), cwd=str(cwd) if cwd is not None else None,
                                       env=dict(env) if env is not None else safe_environment(),
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       start_new_session=(os.name == "posix"))
        except (OSError, ValueError, TypeError, IndexError) as error:
            return {"exit_code": None, "timed_out": False, "launch_error": str(error) or type(error).__name__,
                    "stdout": b"", "stderr": b""}
        try:
            exited = _wait_for_exit(process, timeout)
        except ChildLost as error:
            return {"exit_code": None, "timed_out": False, "launch_error": str(error), "stdout": b"", "stderr": b""}
        finally:
            # Still running, or exited and not yet collected: either way the group id is still ours.
            end_process_group(process)
        if getattr(process, "collected_elsewhere", False):
            return {"exit_code": None, "timed_out": False, "stdout": b"", "stderr": b"",
                    "launch_error": "the child was collected elsewhere; its exit status is unknown"}
        out.seek(0)
        err.seek(0)
        return {"exit_code": process.returncode if exited else None, "timed_out": not exited, "launch_error": None,
                "stdout": out.read(OUTPUT_LIMIT), "stderr": err.read(OUTPUT_LIMIT)}


def run_bounded(arguments, cwd, env: Optional[Mapping[str, str]], timeout: int) -> "subprocess.CompletedProcess":
    """run_group for callers that want text: a launch failure is exit 127 and a timeout is exit 124; neither raises."""
    result = run_group(arguments, cwd, env, timeout)
    if result["launch_error"] is not None:
        return subprocess.CompletedProcess(list(arguments) if isinstance(arguments, (list, tuple)) else [], 127, "", result["launch_error"])
    if result["timed_out"]:
        return subprocess.CompletedProcess(list(arguments), 124, "", "timed out after %d seconds" % timeout)
    return subprocess.CompletedProcess(list(arguments), result["exit_code"], result["stdout"].decode("utf-8", "replace"),
                                       result["stderr"].decode("utf-8", "replace"))


def safe_environment(base: Optional[Mapping[str, str]] = None,
                     identity: Optional[Tuple[str, str]] = None) -> Dict[str, str]:
    """The base environment without inherited Git state, plus the fixed safe settings.

    `identity` is an optional (name, email) used for commits in disposable fixtures.
    """
    source = os.environ if base is None else base
    env = {name: value for name, value in source.items()
           if not name.startswith("GIT_") and name not in STRIPPED_VARIABLES}
    env.update(FIXED_VARIABLES)
    env["GIT_CONFIG_COUNT"] = str(len(SAFE_CONFIG))
    for index, (key, value) in enumerate(SAFE_CONFIG):
        env["GIT_CONFIG_KEY_%d" % index] = key
        env["GIT_CONFIG_VALUE_%d" % index] = value
    if identity is not None:
        name, email = identity
        env.update(GIT_AUTHOR_NAME=name, GIT_COMMITTER_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
    return env




def environment_problems(env: Mapping[str, str]) -> list:
    """Why the Git-related part of an environment is not the safe one; empty when it is.

    Only GIT_* variables and the stripped variables are judged. Other variables that can change what a
    shell or Git does (HOME, PATH, XDG_CONFIG_HOME, ZDOTDIR, ...) are inherited and not checked here.
    """
    problems = []
    expected = safe_environment({}, None)
    for name, value in env.items():
        if name.startswith("GIT_") and name not in expected and name not in (
                "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            problems.append("unexpected Git variable: %s" % name)
        elif name in expected and value != expected[name]:
            problems.append("changed safe setting: %s" % name)
        elif name in STRIPPED_VARIABLES and name != "PAGER":
            problems.append("inherited variable: %s" % name)
    problems.extend("missing safe setting: %s" % name for name in expected if name not in env)
    return problems
