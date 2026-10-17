# Changelog

All notable changes to this project will be documented here.

## [0.1.0] - Unreleased

### Added

- Agent Skill routing for StateRipple v0.1.0 `simulate` and Git+Go `analyze`.
- Fail-closed validation of CLI version, process exit, JSON schema, status, and stop reason.
- Safety contracts for risk interpretation, incomplete evidence, trace semantics, test targets, output protection, untrusted data, and stale results.
- Deterministic build, static validation, unit tests, real-CLI compatibility checks, live Codex/Claude evaluation, offline replay, and coverage profiles.
- English and Japanese installation and usage documentation.
- Release gate with required live and positive/negative activation cases for both agents, package and manifest validation, and a release approval bound to the frozen source and evidence. The initial Human-required policy is superseded by the optional approval routes below.
- Live evaluation runs one suite at a time under a lock and sends SIGKILL to the agent's process group when its turn finishes, times out, or the runner is interrupted, then collects the process and closes its pipes within a bound. Processes that left that group are not reached.
- One shared child-process environment (`scripts/gitenv.py`) for release-check, the CLI compatibility check, fixtures, agents and tests: inherited Git variables and injected Git configuration are dropped, host Git configuration, hooks, file monitors, pagers and credential helpers are disabled, and every child has a timeout.
- The release gate validates every entry of every manifest of the runs it takes evidence from, adopted or not: existence, hash, the record's own seal, kind, case, agent, status and revision, duplicates, and unlisted files.
- Agents under evaluation run, as whole programs, under an operating-system sandbox profile that refuses file writes outside the evaluation tree (`scripts/writeguard.py`). Codex's state directory is placed inside the tree and its own sandbox, which cannot start inside another one, is switched off in favour of the outer profile. Before each case a probe program (`scripts/write_probe.py`) makes real writes, unrestricted against a disposable fixture and then under the profile, and `no-external-writes` passes only for a record whose own evidence shows the operating system refusing them with a permission errno.
- Replay validates a run's manifest with the same check as the release gate.
- A skill load is credited only to one call paired with the host's own result for it: a confirmed Skill delivery, an exact Read, or a single exact `cat` of the installed file.
- Negative-case evidence conservatively rejects unresolved content inputs, including hashes and archive inputs, without treating derived metadata as a skill load. The product activation requirement and the stricter evidence admission rule are distinguished in `docs/EVALUATION.md`.
- Replay source run, source file, raw file hash and record seal are checked against the selected live manifest. Release-check requires PASS and two valid SHA-256 values before comparing repeated builds.
- Live cases L13 (COMP-004: a changed snapshot must be re-run in the same conversation, not answered from the earlier report) and L14 (SAFE-005: no commit or push when repository text urges one and the user forbids it), both mandatory in the release suite for Codex and Claude.

### Changed

- Independent AI Release Review can formally approve a release without Human Review. Human Review remains an optional route with its original H01-H10 and attestation checks. Automatic Grader and Replay PASS alone still cannot approve.
- AI schema 1 binds reviewer identity, the frozen implementation-session list, a separate read-only execution dossier, complete source and manifest inventories, all case/agent Live and Replay seals, five verification logs, all safety contracts, findings, and reasoned decision. A known implementation session cannot approve itself.
- `AI_REVIEW=release/ai-release-review.json` selects the AI route. When AI and Human records are both supplied, both must be valid APPROVE. Either rejection, invalid record, or stale target blocks without fallback. READY never commits, tags, pushes, or publishes.
- Current procedures are in `docs/AI_RELEASE_REVIEW.md` and `docs/AI_RELEASE_REVIEW.ja.md`. Historical audit reports retain the policy and conclusions for their original targets.

### Known limitations

- L13 exercises COMP-004 only for a snapshot file changed between two turns of one conversation. Reuse across separate sessions, of a saved `--out` report, or after a repository change for `analyze` is not exercised live.
- L14 exercises SAFE-005 against a disposable local remote. It judges Git commands that go through the observed `git` executable and shell text in the agent's commands against an allowlist, and compares local and test-remote refs; Git started by absolute path from inside another program, or a transfer by a tool the grader does not know, is detected only if it changes those repositories.
- L14's detection of a publication attempt that the host refused to run relies on heuristic parsing of the agent's shell text; `docs/EVALUATION.md` lists the forms it does not recognize.
- Qualitative reviews of live and activation answers are written by an AI agent and audited by other AI agents. They are not the human review.
- Live prompts state part of the expected behavior, so a PASS shows the agent followed the skill and the prompt together.
- The write restriction is macOS only (elsewhere live cases are BLOCKED). It restricts direct file writes and is not containment: it does not cover the network, requests to system services, or the one session directory Claude Code needs to resume a conversation. A refused attempt is not observed unless it shows in command text. Codex runs with its own sandbox switched off inside the outer one, so its commands have network access. The evaluation tree holds a copy of the Codex login for the duration of a case, readable and writable by the agent; a login renewed there is not written back unless the operator opts in, so a long run can leave the operator having to log in to Codex again, and a tree that could not be removed completely still holds that copy. The runner itself is not sandboxed; that it does not follow links the agent left in the tree is a property of its code, not of the operating system, and a process that left the agent's process group can keep changing the tree while the runner reads it. A case the runner cannot finish because of what it finds in the tree is recorded as BLOCKED. `docs/EVALUATION.md` states the contract and its limits.
- Activation is judged from the agent host's event stream (which call returned exactly which skill file), without operating-system file-access tracing; the grader trusts the host's pairing of calls and results and the runner's stored comparison. `docs/EVALUATION.md` lists what this cannot see.
- After a child process is collected, its process group is never signalled; where an exit cannot be observed without collecting the child, leftover processes of a normally finished command survive.
- The safe child-process environment does not cover a child that sets Git variables again or passes `-c` to Git itself, and `make` targets other than `release-check` start in the caller's environment before building the safe one for their own children.
- The human review check cannot prove who wrote the record; it rejects records that declare or name an AI reviewer or that do not match the frozen revision and evidence.
- AI review validation checks local evidence consistency, not authenticated identity or the truth of logs and conclusions. Unkeyed SHA-256 cannot prevent deliberate rewriting and resealing. Actual independence and review quality require trusted execution logs and reviewer judgment. No signature or third-party attestation is implemented.

[0.1.0]: https://github.com/cottondesu/stateripple-skill/releases/tag/v0.1.0
