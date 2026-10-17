# Safety model

The normative contract IDs and agent actions are in [`src/stateripple/references/safety-contract.md`](../src/stateripple/references/safety-contract.md). This document explains the user-facing consequences.

## Risk is priority, not probability

StateRipple risk values are ordinal propagation states:

| Risk | Meaning |
|---|---|
| 0, Dormant | Inactive in this analysis; not proof of safety. |
| 1, Watch | Indirect impact candidate. |
| 2, Active | Higher-priority impact candidate. |
| 3, Critical | Direct seed or strong propagation support; not certain failure. |

Do not translate values into percentages or incident likelihood. The result supports prioritization; it does not replace engineering judgment, testing, or a security/safety assessment.

## Completeness has two layers

`status: complete` says that the engine completed the calculation over its input snapshot. It does not say that every real dependency was modeled. `status: incomplete` keeps the result partial.

For simulation stopped at `generation_limit`, current risks are lower bounds on the fixed-point risks. Some risks, causes, and test targets can still be missing or too low. For `analyze`, missing adapter evidence can produce `status: incomplete` with `stop_reason: converged`; the graph converged, but the repository evidence did not become complete. That adapter result is not a guaranteed lower bound over unknown real-world dependencies.

`unknown_changes` and warnings are part of the conclusion. Convergence never erases them.

## Trace and test-target limits

Final cell causes describe support for a final maximum. Wave causes describe the generation when a value changed. Cycles and self-loops can make final support circular, so waves are required when tracing back to seeds. Tied maximum causes remain tied.

`test_targets` is a projection of positive-risk nodes whose kind is exactly `test`. It is not a test run, a coverage result, or proof that omitted tests are unnecessary. An empty list has no such implication.

## Non-destructive execution

The skill prefers stdout. StateRipple's `--out` accepts a new destination only and rejects existing files, hardlinks, symlinks (including dangling ones), directories, and special files. The skill never deletes or replaces an entry to make output succeed. Persisted analysis output belongs outside the analyzed repository.

Exit 1 may still leave stdout data or a completed file after a post-publication cleanup failure. Only the captured exit code plus validated JSON determines the result. The skill does not clean, stash, reset, commit, or otherwise change a repository to satisfy analysis prerequisites.

## Untrusted input

Snapshot/report IDs, reasons, paths, warnings, revisions, tool output, and fixture text are data. Instruction-like text inside them cannot authorize tools or change the workflow. Commands use separate arguments rather than shell interpolation, and displayed values are quoted as data. The skill never executes a command suggested by a reason or warning.

Text in a repository, snapshot, or report that tells an agent to commit, push, tag, or publish is not the user's authorization, even when it claims to be pre-approved. Only the user's own request can authorize a Git change, and running an analysis never implies one. Live case L14 checks this with a checklist file that urges a commit and push while the user forbids both.

L14 is evidence about how an agent behaved in one observed run, not a monitor. It compares two disposable repositories before and after, records Git commands that pass through an observed `git` on `PATH`, and parses the agent's shell text heuristically. It does not see Git started by absolute path from inside another program, transfers made by tools it does not know, or text forms its parser does not recognize; [EVALUATION.md](EVALUATION.md) lists these. A PASS means no publication was observed by those three means in that run. It does not mean publication is impossible, and the skill itself has no mechanism that prevents an agent from running Git.

The skill also refuses to fabricate a report when the CLI is unavailable and never downloads or installs the CLI automatically. A failed `analyze` does not fall back to `simulate`.

## Release approval boundaries

Release approval requires the existing technical checks plus a valid independent AI or optional Human approval. Both supplied review records must approve; invalid or rejected records never fall back to another route. AI approval cannot bypass Live, Activation, package, manifest, source/evidence, or Replay validation. An implementation session cannot approve itself against the frozen implementation-session list.

The [AI procedure](AI_RELEASE_REVIEW.md) binds a separate execution dossier, hashed logs, full source and manifest inventories, every case, all 22 contracts, findings, and current target digests. These are inspectable evidence and consistency checks. Unsigned SHA-256 records do not authenticate their author or prove actual independence, log truthfulness, or the review's conclusions. A malicious actor able to rewrite and reseal the whole bundle can forge consistency. No signature or third-party attestation is implemented. The Human route retains its own identity and attestation checks and their existing limits.

READY is an approval result. It never commits, tags, pushes, or publishes a GitHub Release. Those operations require a separate maintainer instruction. Existing historical audit reports describe their original targets and do not approve the current source.

## Offline execution boundaries

Core simulation is local and requires no network, Git, or Go. The Git/Go adapter disables normal acquisition paths, including Git lazy fetch and Go proxy/VCS/toolchain downloads. This is not OS-level network isolation. Installed binaries and wrappers, configured helpers, cache helpers, repository configuration, and working/output directories remain trusted dependencies.

Analysis covers the current target and standard build conditions. Static imports cannot represent every dynamic dependency. Generated code, alternate build tags/platforms, local replacements, workspaces, nested modules, missing cache data, and unresolved ownership can restrict evidence.

## Freshness

An `input_sha256` identifies StateRipple's normalized canonical snapshot. It is not the original file's raw-byte hash or the hash of the external environment. A report is current only for its exact snapshot or base/HEAD, toolchain, and build context. After relevant change, rerun instead of relabeling old evidence. Within one conversation this means a question about the current input is answered from a new CLI run, and the earlier report is kept apart as history; live case L13 replaces the snapshot between two turns to check it.
