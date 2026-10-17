# Human release review

Human release review is an optional approval route. A project maintainer performs it in person after the code is frozen. An agent review, a subagent audit, or an automatic grader PASS is never a human review and must not be recorded as one. An [independent AI release review](AI_RELEASE_REVIEW.md) is a separate approval route that can authorize READY without this Human record.

Without either release approval, `make eval-release` reports `readiness: NOT READY` and exits 1. With only `AI_REVIEW` supplied, the Human status is `NOT_REQUIRED`. When both review paths are supplied, every record must validate and approve. A rejection, missing file, invalid format, or stale target in either path blocks READY.

## When to do it

1. Freeze the code. Any later change to `src/`, `scripts/`, `tests/`, `docs/`, `.github/`, `Makefile`, the READMEs, `CHANGELOG.md`, `LICENSE`, or `.gitignore` changes `source_tree_sha256` and invalidates the review.
2. Make sure the evidence listed in `docs/RELEASE_EVIDENCE.txt` is the evidence you intend to release on. That file is the only list to review from: earlier runs of the same cases are kept under `artifacts/evaluations/` as history, are not part of the release decision, and do not need to be read.
3. Run the gate without a review to get the values the review must be bound to:

   ```sh
   make build
   make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)"
   ```

   The JSON it prints contains `review_target` with `skill_revision`, `source_tree_sha256`, and `evidence_digest`. With no release approval it exits 1 and `blocking` should list only missing release approval; `evidence_manifests.status` should be `PASS`, with every manifest entry counted, adopted or not.

## Checklist

Answer every item yourself. The gate requires all ten to be `true`.

| ID | Statement |
|---|---|
| H01 | I am a human maintainer of this project; no AI system wrote or submitted this record for me. |
| H02 | The source tree I reviewed is the frozen release candidate and matches `source_tree_sha256` below. |
| H03 | I read `src/stateripple/SKILL.md` and the four reference files and accept them as the v0.1.0 skill text. |
| H04 | I ran, or watched, `make build`, `make check`, `make test`, `make cli-compat` and `make release-check` and they exited 0. |
| H05 | I read live answers myself for both agents, including L05, L06, L07, L11, L13 and L14, and agree with their verdicts. |
| H06 | I read positive and negative activation answers for both agents and agree with their verdicts. |
| H07 | I understand the agent reviews and audits were done by AI systems and I do not treat them as my review. |
| H08 | I read the known limitations in `CHANGELOG.md` and `docs/EVALUATION.md`, including L14's incomplete Git monitoring, and accept them for this release. |
| H09 | I checked that no secrets, personal paths or unrelated content are in the files to be published. |
| H10 | I decide the commit, tag, push and release myself; none was made by an AI system. |

Where to look:

- Live answers: each `L*.json` or `A-*.json` under the `artifacts/evaluations/live/...` paths in `docs/RELEASE_EVIDENCE.txt`, and only those. The file lists exactly one live record and one replay per case and agent. The answer is `observations.agent_events.final_response` (for L13, `observations.turns[*].final_response`); the commands are `observations.agent_events.tool_commands` and `observations.cli_invocations`; what each tool call returned is summarized in `observations.agent_events.tool_calls` (for activation, `exact` names the skill file a call's output was, and a Skill call's `host_confirmed` and `delivery` show what the host delivered); the restriction on writes the agent ran under is in `observations.write_guard`, with the errno the operating system returned for each probed write; for L14 also `observations.git_invocations` and `observations.git_state`.
- Agent review notes: `manual.notes` in the matching replay records. They are an AI system's opinion.
- Things an AI reviewer flagged that deserve your own judgment are written in `manual.notes` of the replay record for that case in `docs/RELEASE_EVIDENCE.txt`. Read them there, for the records listed there. Notes about superseded records do not apply to the current evidence.

## Record

Copy `docs/human-review.template.json` to `release/human-review.json` and fill it in by hand:

- `reviewer`: your name. `reviewer_kind` must be `human`.
- `reviewed_at`: UTC, `YYYY-MM-DDTHH:MM:SSZ`.
- `skill_revision`, `source_tree_sha256`, `evidence_digest`: copy from `review_target`.
- `git_commit`: the commit you are about to release, once it exists; otherwise `null`.
- `checklist`: each of H01–H10 set to `true` only if it is true.
- `notes`: what you looked at and anything you want on record.
- `decision`: `APPROVE` or `REJECT`.
- `attestation`: leave the sentence exactly as it is in the template if it is true.

Then run the gate with it:

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)" HUMAN_REVIEW=release/human-review.json
```

For the Human route, `readiness: READY` and exit 0 require all technical checks, zero live and activation gaps, and a record that names a human, approves, confirms every item, carries the attestation, and matches all three target values. A record for another revision or another evidence set fails. If an AI record is also supplied, it must independently validate and approve. READY does not commit, tag, push, or publish anything.

## Limits of this check

The gate cannot prove who typed the record. It rejects records that declare an agent reviewer, name a known AI system, omit the attestation, or do not match the frozen revision and evidence, but an honest process is what makes the review real. AI agents working on this repository must not create, fill in, or edit `release/human-review.json`.
