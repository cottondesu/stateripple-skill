#!/usr/bin/env python3
"""Attempt real file-system writes and report what the operating system answered.

The live runner starts this program twice before every case: once as it is, against a disposable
fixture, to show that the writes succeed when nothing restricts them, and once under the sandbox
the agent will run in, to show that the same kinds of write are refused there. Each operation is
a system call made here; the result is the outcome of that call and, when it failed, the errno
the operating system returned. Nothing is inferred from an exit status or from message text.

usage: write_probe.py NONCE SPEC_JSON
SPEC_JSON is a list of {"id": str, "kind": str, "path": str}, with "to": str as well for a rename. Kinds:
  create   open(path, O_WRONLY|O_CREAT|O_EXCL) and close
  mkdir    mkdir(path)
  append   open(path, O_WRONLY|O_APPEND) of an existing file and write one byte
  unlink   unlink(path) of an existing file
  rename   rename(path, to)
  child    start this program again for one "create" of path and report what the child saw
Prints one JSON object on standard output and exits 0 whenever every operation was attempted,
whatever their outcomes. Exit 3 means the request itself could not be read.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

if not __package__:
    from gitenv import safe_environment
else:
    from .gitenv import safe_environment

SCHEMA = 1
KINDS = ("create", "mkdir", "append", "unlink", "child", "rename")
CHILD_TIMEOUT_SECONDS = 20


def attempt(kind: str, path: str, to: str = "") -> dict:
    """One operation: {"outcome": "ok"} or {"outcome": "errno", "errno": N, "errno_name": NAME}."""
    try:
        if kind == "create":
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        elif kind == "mkdir":
            os.mkdir(path, 0o700)
        elif kind == "append":
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(descriptor, b"x")
            finally:
                os.close(descriptor)
        elif kind == "unlink":
            os.unlink(path)
        elif kind == "rename":
            os.rename(path, to)
        else:
            return {"outcome": "unsupported"}
    except OSError as error:
        return {"outcome": "errno", "errno": error.errno, "errno_name": errno.errorcode.get(error.errno, "UNKNOWN")}
    return {"outcome": "ok"}


def attempt_in_child(nonce: str, path: str) -> dict:
    """The same create made by a child process, which inherits whatever restricts this one."""
    spec = json.dumps([{"id": "child", "kind": "create", "path": path}])
    try:
        completed = subprocess.run([sys.executable, os.path.abspath(__file__), nonce, spec], env=safe_environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=CHILD_TIMEOUT_SECONDS)
        report = json.loads(completed.stdout.decode("utf-8"))
        result = report["results"][0]
        if completed.returncode != 0 or report.get("nonce") != nonce or result.get("id") != "child" \
                or result.get("outcome") not in ("ok", "errno"):
            return {"outcome": "child_failed"}
        return {key: result[key] for key in ("outcome", "errno", "errno_name") if key in result}
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired):
        return {"outcome": "child_failed"}


def main(argv) -> int:
    try:
        nonce, spec = argv[1], json.loads(argv[2])
        if not isinstance(nonce, str) or not isinstance(spec, list) or len(argv) != 3:
            raise ValueError("bad request")
        operations = [(item["id"], item["kind"], item["path"], item.get("to", "")) for item in spec]
        if not all(isinstance(value, str) for operation in operations for value in operation) \
                or not all(kind in KINDS and (bool(to) == (kind == "rename")) for _, kind, _, to in operations):
            raise ValueError("bad operation")
    except (IndexError, KeyError, TypeError, ValueError):
        return 3
    results = []
    for identifier, kind, path, to in operations:
        outcome = attempt_in_child(nonce, path) if kind == "child" else attempt(kind, path, to)
        results.append(dict(outcome, id=identifier, kind=kind))
    sys.stdout.write(json.dumps({"schema": SCHEMA, "nonce": nonce, "pid": os.getpid(), "results": results}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
