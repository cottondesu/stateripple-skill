# StateRipple CLI compatibility

## Supported contract

stateripple-skill v0.1.0 supports exactly:

- StateRipple tag `v0.1.0`
- Upstream commit `2334ba7573c1d0b4565189c93f780b35e08ea81d`
- CLI version output `stateripple v0.1.0`
- Snapshot/report `schema_version: 1`
- Ruleset `risk-propagation-v1`

Compatibility with another version is not assumed from semantic-version similarity. A missing, different, or unparseable CLI version blocks analysis.

## Verification interface

Run the real binary, never a mock replacement:

```sh
make cli-compat
```

The checker may accept an explicit executable path so a compatible binary outside `PATH` can be tested. It must first execute the candidate's `version` command and reject anything other than v0.1.0. It runs in temporary directories and creates disposable Git/Go repositories for `analyze`; it must not modify the developer's working repository.

The minimum case set is:

| ID | Contract |
|---|---|
| C01 | Exact CLI version |
| C02 | Successful simulation |
| C03 | Simulation without Git/Go/repository |
| C04 | Generation limit returns exit 2 and incomplete JSON |
| C05 | Invalid snapshot returns exit 1 |
| C06 | Risk 0 remains inactive, not a safety claim |
| C07 | Final causes and wave history are present and distinct |
| C08 | `test_targets` projection |
| C09 | Existing output is preserved |
| C10 | Fresh output is created |
| C11 | Successful committed-Go analysis |
| C12 | Dirty worktree is rejected |
| C13 | Adapter incompleteness returns exit 2 |
| C14 | Unsupported Git is rejected without fallback |
| C15 | Identical normalized input produces identical JSON bytes |

Each result must retain command arguments, exit code, parsed status/stop reason where applicable, and sanitized stdout/stderr evidence. A file's existence alone never passes a case.

## Development preflight

The fixed source tree was inspected locally with `git show v0.1.0:<path>` after resolving tag `v0.1.0` to the expected commit. A compatible binary was supplied at `<HOME>/go/bin/stateripple` and its version was verified as v0.1.0. The path is an environment fact, not a portable installation path and is not embedded in the skill package.

Case verdicts are produced by the compatibility runner and its evidence artifacts. This document does not upgrade an unexecuted case to PASS. Real-CLI checks, live-agent checks, offline replay, and static tests are separate evidence classes.

## Upstream execution evidence and platform limits

StateRipple v0.1.0's own validation reports Go 1.23.12 full-suite execution on macOS arm64 and Linux arm64 containers, plus GitHub Actions Linux amd64. Windows runtime was not verified. Cross-compilation proves compilation only, not runtime behavior. stateripple-skill records its own compatibility execution separately and does not inherit upstream PASS results.

`analyze` evaluates the current target and standard build conditions. Dynamic loading, generated code, alternate tags/platforms, workspaces, nested modules, local replacements, missing cache objects, and unknown ownership can limit completeness. Git/Go fetch suppression is not OS network isolation.

Known upstream v0.1.0 operational limits include tracked symlinks receiving conservative treatment, a possible `go list` wait on an ignored `*.go` FIFO, exit 1 after an output was already published if cleanup fails, temporary-name length limits, conservative nested `go.mod` handling, and per-file base-blob process cost. See the fixed StateRipple `README.md` and `docs/VALIDATION.md` for their exact descriptions.
