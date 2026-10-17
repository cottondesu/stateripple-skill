# StateRipple v0.1.0 CLI contract

This reference is fixed to StateRipple tag `v0.1.0`, commit `2334ba7573c1d0b4565189c93f780b35e08ea81d`. Do not import behavior from later branches or releases.

## Commands

```text
stateripple version
stateripple simulate --input <snapshot.json> --format json [--out <fresh-path>]
stateripple analyze [--base <revision>] --format json [--out <fresh-path>]
```

`simulate --input` is required. `analyze --base` defaults to `HEAD~1`. The output format defaults to text, so this skill always supplies `--format json`. Snapshot input via stdin and flags not listed above are unsupported.

`simulate`, `help`, and `version` require neither Git nor Go. `analyze` requires a clean, single-root-module Git repository; Git 2.45.0 or later with global `--no-lazy-fetch`; Go 1.23 or later; resolvable `HEAD` and base; and supported current build conditions. It compares base with `HEAD`, never uncommitted changes.

## Exit contract

| Exit | Meaning | Required handling |
|---|---|---|
| 0 | Complete result | Require valid JSON with `status: complete`; otherwise fail closed. |
| 1 | Input, usage, requirement, execution, or output failure | Do not interpret as an analysis result, even if stdout or an `--out` file contains data. Report stderr safely. |
| 2 | Incomplete result | Require valid JSON with `status: incomplete`; report partial findings and their limit. |

Any other exit, malformed JSON, exit/status mismatch, unknown schema, or missing required field is an invalid run. Do not repair it or infer what the CLI meant.

## Snapshot v1

Required top-level fields are `schema_version`, `ruleset`, `nodes`, `edges`, and `seeds`; `max_generations` is optional and normalizes to 16. Require `schema_version: 1` and `ruleset: risk-propagation-v1`.

- Nodes are `{id, kind}`. Kinds are nonempty free-form labels. Only the exact, case-sensitive node kind `test` has a report convention.
- Edges are `{from, to, kind, attenuation}`. Direction is `from` to `to`; attenuation is an integer from 0 through 3. Kind does not imply attenuation.
- Seeds are `{id, risk, reason}`. Risk is an integer from 0 through 3.
- `max_generations`, when explicit, is an integer from 1 through 1024.
- Cycles and self-loops are valid.

StateRipple rejects unknown fields, duplicate JSON keys, nulls, wrong types, invalid UTF-8, trailing JSON values, duplicate node IDs, invalid references, and conflicting edges. It normalizes order and exact duplicate edge/seed records; the skill does not preemptively repair or reinterpret input.

RiskPropagationV1 synchronously computes:

```text
R_next(v) = max(R(v), Seed(v), max over u->v of max(0, R(u) - attenuation(u,v)))
```

This formula documents the CLI contract; it is not permission to calculate a replacement result in the skill.

## Report v1

Required top-level fields, in the CLI's canonical order, are:

```text
schema_version, ruleset, status, input_sha256, generation_count,
cells, test_targets, waves, unknown_changes, warnings, changes,
build_context, stop_reason
```

For simulation, `changes` is empty and `build_context` is null. For analysis, `changes` records Git classifications and `build_context` records the current Go environment. The valid engine stop reasons are `converged` and `generation_limit`; adapter incompleteness can produce `status: incomplete` with `stop_reason: converged`.

`input_sha256` hashes the normalized snapshot's canonical compact JSON, including its final LF. It is not the hash of the original file bytes. Validate known hashes only against official v0.1.0 golden fixtures.

## Output safety

Stdout is preferred. With `--out`, StateRipple only creates a new file and rejects every existing destination entry, including regular files, hardlinks, symlinks, dangling symlinks, directories, and special files. Never delete or replace an entry to make a retry succeed. Use a new destination if another run is needed.

Exit 1 may follow output publication or leave partial stdout. A present file therefore does not establish success. Preserve the exit code, stderr, and output together. Store output outside an analyzed repository so the report does not dirty the next analysis.

StateRipple disables Git and Go acquisition paths but does not provide OS network isolation. Installed binaries, wrappers, repository helpers, credential helpers, external cache helpers, and the working/output directories remain trust boundaries.
