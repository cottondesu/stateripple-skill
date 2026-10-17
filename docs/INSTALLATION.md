# Installation

Install the StateRipple CLI and the skill separately. The skill contains instructions and references only; it does not bundle, download, build, or update the CLI.

## 1. Install StateRipple v0.1.0

Obtain StateRipple from its trusted upstream release or build the fixed tag yourself according to the [StateRipple repository](https://github.com/cottondesu/stateripple). Verify the executable visible to the agent:

```sh
command -v stateripple
stateripple version
```

The second command must print exactly:

```text
stateripple v0.1.0
```

Do not continue with a different version. `simulate` needs no Git or Go at runtime. `analyze` additionally needs Git 2.45.0+ with `--no-lazy-fetch` and Go 1.23+.

## 2. Build the skill package

From this repository:

```sh
make build
make check
```

Install the generated `dist/stateripple/` directory, not individual files. It contains `SKILL.md` and all four references.

The installed skill needs no Python process, background service, or Mandala CLI. Python and Mandala are development-time tools in this repository only.

## Codex

For a project-shared skill, copy the generated directory to:

```text
<repository>/.agents/skills/stateripple/
```

For a personal skill available across local projects, copy it to:

```text
~/.agents/skills/stateripple/
```

Start a new Codex session so skill discovery sees the installation. OpenAI's current examples use `.agents/skills/` for repository-local skills. Older `.codex/skills` layouts are not asserted as supported by this release; consult the documentation for the exact Codex version in use before choosing a legacy path.

## Claude Code

For a project skill, copy the generated directory to:

```text
<repository>/.claude/skills/stateripple/
```

For a personal skill on the local machine, copy it to:

```text
~/.claude/skills/stateripple/
```

A project skill that is neither committed nor ignored is an untracked file, and `stateripple analyze` rejects a worktree with untracked files. Commit the skill directory or add it to `.gitignore` before analyzing that repository.

Start a new Claude Code session. Invoke the skill explicitly with `/stateripple` or use a prompt matching its description. Project skills can be committed for the team; personal skills are machine-local and are not automatically available in cloud sessions.

## Verify discovery safely

Use an explicit, non-mutating request such as:

```text
Use the stateripple skill and tell me which StateRipple version is required. Do not run an analysis.
```

Then test a real simulation with a known snapshot in a disposable directory. The agent must check `stateripple version`, use `--format json`, and report the actual exit code. For `analyze`, use a disposable clean Go repository; do not use a worktree with valuable uncommitted changes.

Current product documentation changes independently of this package. Check the official [Codex skills guidance](https://developers.openai.com/blog/skills-agents-sdk) and [Claude Code skills documentation](https://code.claude.com/docs/en/skills) when installing into a newer host.
