# stateripple-skill

[日本語](README.ja.md)

An Agent Skill that uses [StateRipple](https://github.com/cottondesu/stateripple) v0.1.0 to simulate deterministic risk propagation over supplied graphs or analyze committed changes in supported Go repositories.

The skill chooses the CLI mode, checks prerequisites, runs the real StateRipple binary, and interprets JSON without overstating it. StateRipple remains the calculation and repository-analysis engine; this project does not reimplement propagation. Mandala is used only to track development coverage and is not a runtime dependency.

## Capabilities

- `simulate`: analyze a supplied schema-v1 snapshot without requiring Git, Go, or a repository.
- `analyze`: compare a base revision with `HEAD` in a clean, single-root Go module.
- Validate the process exit code together with JSON schema, status, and stop reason.
- Explain ordinal risk, incomplete results, tied causes, wave history, test-target projection, and input freshness safely.

The skill does not install StateRipple, alter input, calculate its own result, analyze uncommitted changes, run tests, or fall back from a failed `analyze` to `simulate`.

## Requirements

- StateRipple CLI exactly `v0.1.0`.
- An Agent Skills-compatible host such as Codex or Claude Code.
- For `analyze` only: Git 2.45.0 or later with `--no-lazy-fetch`, Go 1.23 or later, and a clean supported repository.

Install the StateRipple binary separately from this skill. See [Installation](docs/INSTALLATION.md) for personal and project layouts.

## Use

Explicit requests are the clearest activation signal:

```text
Use StateRipple to simulate risk propagation in ./snapshot.json.
Use StateRipple to analyze committed Go changes from HEAD~2 to HEAD.
```

The expected CLI shapes are:

```sh
stateripple simulate --input ./snapshot.json --format json
stateripple analyze --base HEAD~2 --format json
```

Risk is a propagated priority, not probability. Risk 0 is not proof of safety. `status: complete` means computation completed for the available snapshot, not that the snapshot contains every real dependency. StateRipple identifies `test_targets`; it does not execute tests.

## Development and verification

```sh
make build
make check
make test
make cli-compat
make eval-replay EVIDENCE=/path/to/live/run
make eval-coverage PROFILE=snapshot-simulation EVIDENCE=/path/to/live/run
make eval-release EVIDENCE="/path/to/codex/live /path/to/codex/replay /path/to/claude/live /path/to/claude/replay"
make release-check
```

Live agent evaluation is explicit and is not part of ordinary CI:

```sh
make eval-live AGENT=codex SUITE=release
make eval-live AGENT=claude SUITE=release
```

See [Evaluation](docs/EVALUATION.md), [CLI compatibility](docs/CLI_COMPATIBILITY.md), and [Safety](docs/SAFETY.md). The canonical skill source is `src/stateripple/`; `dist/stateripple/` is generated.

Release approval accepts an [independent AI release review](docs/AI_RELEASE_REVIEW.md) or an optional [Human release review](docs/HUMAN_REVIEW.md). AI approval alone can produce READY after every technical check passes. Grader PASS and Replay agent reviews alone cannot approve a release. If both records are supplied, both must be valid APPROVE; a failure never falls back to the other route. AI independence and evidence inspection depend on trusted execution logs within the [documented limits](docs/AI_RELEASE_REVIEW.md#trust-limits-and-evidence-reuse). READY does not publish anything.

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)" AI_REVIEW=release/ai-release-review.json
```

Historical audit reports retain their original Human-required policy and NOT READY results. They are not approval of a new source digest.

## Version scope

This release is pinned to StateRipple tag `v0.1.0`, commit `2334ba7573c1d0b4565189c93f780b35e08ea81d`, schema 1, and `risk-propagation-v1`. Later StateRipple behavior is not implicitly compatible.

## License

[MIT](LICENSE), copyright 2026 cottondesu.
