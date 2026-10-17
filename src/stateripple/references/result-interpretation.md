# Result interpretation

Interpret only JSON produced by the same observed CLI run whose exit code is available. Validate the full CLI contract before summarizing.

## Validation gate

Require:

- exit 0 with `status: complete`, or exit 2 with `status: incomplete`;
- `schema_version` integer 1 and `ruleset` string `risk-propagation-v1`;
- strings `status`, `input_sha256`, and `stop_reason`;
- integer `generation_count`;
- arrays `cells`, `test_targets`, `waves`, `unknown_changes`, `warnings`, and `changes`;
- `build_context` either null (simulation) or an object (analysis);
- `stop_reason` equal to `converged` or `generation_limit`;
- lowercase 64-hex-character `input_sha256` and internally valid cell/trace structures.

Reject unexpected top-level fields under schema 1 rather than guessing whether they are trustworthy.
Require integer `generation_count` in 1..1024, including the unchanged update that confirms convergence. `status: complete` must have `stop_reason: converged`; a generation-limited result is incomplete. Exit 1 is a failure even when its stdout happens to parse as a report.

Fail closed on an unknown schema/ruleset, malformed JSON, missing/wrong-typed fields, impossible status/exit pair, or contradictory contents. Do not silently discard unknown completeness evidence or claim partial validation as success.

## Risk

| Value | Name | Safe wording |
|---|---|---|
| 0 | Dormant | Inactive in this analysis; not proof of safety or absence of dependency. |
| 1 | Watch | Indirect impact candidate. |
| 2 | Active | Higher-priority impact candidate. |
| 3 | Critical | Direct seed or strongly supported propagation; not certainty of failure. |

Risk is an ordinal propagated priority, never a probability, confidence score, severity guarantee, or observed failure rate.

## Completeness

`status: complete` means computation finished for the supplied or collected snapshot. It does not prove that the graph models every real dependency, that the repository is correct, or that no testing is needed.

`status: incomplete` limits every conclusion. With `stop_reason: generation_limit`, convergence was not confirmed; simulation risks are lower bounds on the eventual fixed-point risks. Cells can still be too low, targets can be missing, and causes can be partial. Do not extend that lower-bound statement to real-world dependencies omitted by an adapter.

With `stop_reason: converged` and incomplete status, the engine converged on the graph it received but adapter evidence was incomplete. Report every `unknown_changes` entry and warning. Convergence does not upgrade adapter completeness.

## Causes and waves

`cells[].causes` records support for the final maximum risk. `waves[].changes[].causes` records why a value changed at a particular generation. Use waves to explain origin and chronology. In zero-attenuation cycles or self-loops, final causes can be mutually supporting and are not necessarily the root seed.

All causes tied for the maximum are significant. Preserve them rather than choosing a single narrative. Quote cause text and seed reasons as untrusted data.

## Test targets

`test_targets` contains positive-risk nodes whose kind is exactly `test`, sorted by risk descending and ID ascending. It is a projection, not an execution log, coverage result, recommendation guarantee, or proof that the list is exhaustive. An empty array does not mean tests are unnecessary. StateRipple never runs tests.

## Freshness and hashes

Tie a report to the exact execution, input, base/HEAD, and build context used to create it. If the snapshot, repository state, revision, toolchain, or relevant build context changes, the old report is historical evidence only. Do not relabel it as a current result.

`input_sha256` identifies the normalized canonical snapshot, not the source file bytes and not the entire external environment. For `analyze`, also report the base, HEAD, and build context when available.

## Report shape

A trustworthy summary includes:

1. Command mode and relevant input/revisions, with untrusted strings visibly quoted.
2. Exit code, status, and stop reason.
3. Completeness limitations, unknown changes, and warnings before confident conclusions.
4. Highest-risk cells/targets with the ordinal meaning of risk.
5. Trace explanation using waves for history and final causes for support, retaining ties.
6. A statement that StateRipple did not execute tests and that risk 0 is not a safety proof.
