# Approve a release with an independent AI review

An independent AI release review can approve this project without a Human Review. A Human Review remains an optional route. Automatic graders, Replay agent reviews, and earlier audit reports do not authorize a release.

`make eval-release` requires all technical checks and at least one valid release approval. If both `AI_REVIEW` and `HUMAN_REVIEW` are supplied, both must be valid `APPROVE` records. A rejection, missing file, malformed record, or stale target in either path blocks the release. The Gate never falls back to the other path.

## Freeze the target

1. Record every implementation session and the pre-change `baseline_source_tree_sha256` in `docs/release-implementation.json` before freezing the source. Preserve previous implementation sessions. This file is part of `source_tree_sha256`.
2. Run `make build`, `make check`, `make test`, `make cli-compat`, and `make release-check`. Supply `STATERIPPLE` when the compatible binary is outside PATH. On macOS, the deep-directory test needs more than 400 file descriptors. Use `ulimit -n 4096` if the shell limit is too low. Keep the OS default temporary directory separate from `/tmp`. Nested `sandbox-exec` tests need a host that permits applying that profile.
3. Select the adopted bundles from `docs/RELEASE_EVIDENCE.txt`. Run the Gate without an approval to obtain the current target.

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)"
```

Without approval the command exits 1. Before review, all technical results must pass and the only blocker must be missing release approval. Retain the printed `review_target` containing `skill_revision`, `source_tree_sha256`, and `evidence_digest`.

Any subsequent change to the source inventory invalidates the review. `release/`, `artifacts/`, and Mandala work areas outside that inventory are excluded. Keep the review and its supporting bundle under `release/` to avoid a circular digest. Exclusion from the source digest does not exempt the review from verification.

## Run a separate review session

Start a separate AI execution with read-only access to the source. Give it the frozen target, pre-change source or a diff, adopted evidence paths, and verification logs. The implementation session must not write its own approval. A different model is useful but does not itself establish independence.

The reviewer must read the changed code and diff, the Skill and safety contracts, all adopted Live answers and Replay reviews, positive and negative Activation answers, verification results, and known limitations. It must inspect L13 freshness, L14 unauthorized Git behavior, no-external-writes, manifest validation, and the Human approval route. It must verify the implementer's claims against files and logs. Keep source hashes before and after the review.

Create `release/ai-release-review.json` from [the AI template](ai-release-review.template.json) only after that work. The template is deliberately invalid. Never copy H01-H10 or a human attestation into an AI record. An AI record passed to `HUMAN_REVIEW` fails.

## Record format

Schema 1 uses `record_type: ai-release-review` and `reviewer_kind: ai`. All top-level fields in the template are required. Unknown fields are rejected. `reviewer_id`, `model`, and `session_id` are 3 to 160 ASCII characters using letters, digits, `_`, `.`, `:`, `/`, or `-`. Record the actual model and actual review execution identity. Times use real UTC `YYYY-MM-DDTHH:MM:SSZ` values and cannot be in the future.

The three target fields must equal the Gate's current values. `method` describes how the review ran. `rationale` explains the decision from inspected evidence. `independence.implementation_sessions` and `independence.baseline_source_tree_sha256` must equal the frozen provenance. The review session cannot appear in the implementation list. `basis` identifies the separate execution and its provenance. `limitations` states what cannot be independently authenticated.

`evidence_review` has exactly eight checks, each with `status: PASS`, concrete `notes`, and `log_lines: [first, last]` pointing to nonempty lines of the bound transcript.

| Check | Required inspection |
| --- | --- |
| `live` | Every required L01-L14 case for Codex and Claude. |
| `replay` | The matching Replay records and their bound qualitative reviews. |
| `positive_activation` | A-P01 through A-P04 for both agents. |
| `negative_activation` | A-N01 through A-N04 for both agents. |
| `package` | Package sources, archive, manifest, and reproducibility. |
| `manifests` | Every entry in all adopted run manifests. |
| `safety_contracts` | Every contract in `scripts.validate.CONTRACT_IDS`, currently 22. |
| `known_limitations` | Current limitations in the Changelog, Evaluation, Safety, and this guide. |

`safety_contracts` maps exactly those contract IDs to `PASS`. A finding has exactly `id`, `severity`, `description`, `evidence`, `status`, and `resolution`. IDs are unique. Severities are `BLOCKER`, `MAJOR`, or `MINOR`. Statuses are `OPEN`, `RESOLVED`, or `ACCEPTED`. An `OPEN` finding blocks approval. BLOCKER and MAJOR findings must be `RESOLVED` with resolution evidence. Only MINOR findings may be `ACCEPTED`, with a nonempty acceptance rationale. An empty findings array is valid after actual inspection.

`decision` is `APPROVE` or `REJECT`. A valid `REJECT` always blocks. An APPROVE with unresolved serious findings, missing evidence, or incomplete checks also blocks.

## Bind the execution evidence

`execution` references a separate sealed JSON dossier using `{"path": "ai-review-evidence/execution.json", "sha256": "<raw-file SHA-256>"}`. Paths are relative to the approval record directory. Absolute paths, parent traversal, symlinks below that directory, empty files, and artifacts larger than 16 MiB are refused. Each artifact reference contains exactly `path` and `sha256`.

The execution dossier requires these fields.

- `schema_version: 1`, `record_type: ai-review-execution`, and the same `reviewer_id`, `model`, and `session_id` as the approval.
- `review_target` with all three current digests, `read_only: true`, and `source_tree_before` and `source_tree_after` equal to the frozen source digest.
- `baseline_source_tree_sha256` matching the frozen pre-change digest, and `diff`, a hashed artifact reference to the reviewed source diff. Preserve the baseline source or another trustworthy way to reproduce this diff.
- `started_at` and `finished_at` in UTC, ordered before or at `reviewed_at`.
- `source_files`, an exact map of every path from `eval_coverage.source_tree_files(project_root)` to its raw SHA-256. Source symlinks use the link text, matching the source digest convention.
- `manifest_files`, an exact map of adopted run manifest paths to their raw SHA-256. Paths are relative to the project root. This binds the manifests as well as the record-based `evidence_digest`.
- `transcript`, a hashed reference to the actual review execution log, including tool actions, inspected files, findings, and conclusions.
- `verification`, keyed by exactly `make build`, `make check`, `make test`, `make cli-compat`, and `make release-check`. Each value contains integer `exit_code: 0`, the frozen `source_tree_sha256`, and a `log` artifact reference to the command output. Retain invocation and environment details in these logs, including an explicit `STATERIPPLE` path when needed.
- `case_reviews`, exactly one entry for every current case and agent, currently 44 entries. Each entry has `case_id`, `agent`, `records: {"live": "<record seal>", "replay": "<record seal>"}`, `status: PASS`, inspection `notes`, and `log_lines`. Both record seals must match the adopted records. Added catalog cases must also be reviewed.
- `integrity`, a canonical SHA-256 seal produced with the existing `eval_common.seal_record` convention. The approval itself also needs this seal.

Seal the dossier first, compute its raw file hash, then seal the approval referencing it. Do not change either file afterward. An independent reviewer can save completed records with `eval_common.write_json_exclusive(path, eval_common.seal_record(record))`. This operation does not perform a review and must never generate an approval without one.

## Check the approval

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)" AI_REVIEW=release/ai-release-review.json
```

A successful report has zero Live and Activation gaps, Package and Evidence Manifests PASS, `ai_review.status: PASS`, `ai_review.decision: APPROVE`, `human_review.status: NOT_REQUIRED`, and `readiness: READY`. Exit 0 means release approval passed. It does not commit, tag, push, or publish a GitHub Release. Publication requires a separate maintainer instruction.

To supply both routes, add `HUMAN_REVIEW=release/human-review.json`. Follow [the Human procedure](HUMAN_REVIEW.md) for that record. Do not supply the unfilled Human template when selecting AI alone.

## Trust limits and evidence reuse

The Gate checks structure, seals, references, coverage, identities declared in frozen provenance, time ordering, current digests, and actual technical evaluation results. It requires review artifacts, not merely `independent: true`. It does not prove that a transcript is truthful, that notes correctly interpret the source, that a command really ran, or that the declared session belongs to a separate actor. A person or AI able to rewrite all unsigned records can also recompute their hashes. No signature, third-party attestation, identity authentication, or complete independence proof is implemented. Inspect the available host execution logs before relying on an approval.

Existing Live and Replay evidence can be reused when Skill text, recorder meaning, grading contracts, case meaning, sandbox policy, and evidence format are unchanged. The Gate still regrades the records and validates all manifests against the current Skill. Approval metadata changes alone do not require a fresh Live run. Any source change requires a new approval target and a new review. Never edit or reseal adopted evidence to force reuse.

Mandala tracks declared coverage and gaps. Its zero-gap result is separate from technical checks and independent review. Historical audit reports retain their original Human-required conclusions and are not approval records for a new source digest.
