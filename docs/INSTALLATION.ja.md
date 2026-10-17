# インストール

StateRipple CLIとSkillは別々に導入します。Skillは指示と参照文書だけを含み、CLIを同梱、download、build、updateしません。

## 1. StateRipple v0.1.0

信頼できるupstream releaseからStateRippleを取得するか、[StateRipple repository](https://github.com/cottondesu/stateripple)の固定tagを手順どおりbuildします。Agentから見える実行ファイルを確認します。

```sh
command -v stateripple
stateripple version
```

2つ目のcommandは厳密に次を出力する必要があります。

```text
stateripple v0.1.0
```

異なるversionでは解析を続行しません。`simulate`の実行時にGitやGoは不要です。`analyze`にはさらに`--no-lazy-fetch`対応のGit 2.45.0以上とGo 1.23以上が必要です。

## 2. Skill packageのbuild

本repositoryのrootで実行します。

```sh
make build
make check
```

個別ファイルではなく、生成された`dist/stateripple/`directory全体を導入します。`SKILL.md`と4つのreferenceが含まれます。

導入後のSkillはPython process、常駐service、Mandala CLIを必要としません。PythonとMandalaは本repositoryの開発時だけに使用します。

## Codex

Projectで共有する場合は次へcopyします。

```text
<repository>/.agents/skills/stateripple/
```

Localの全projectで使う個人用Skillは次へcopyします。

```text
~/.agents/skills/stateripple/
```

導入後に新しいCodex sessionを開始します。OpenAIの現行例はrepository-local Skillに`.agents/skills/`を使用します。本releaseは古い`.codex/skills`配置の対応を断定しません。legacy pathを使う前に、利用するCodex versionの文書を確認してください。

## Claude Code

Project Skillは次へcopyします。

```text
<repository>/.claude/skills/stateripple/
```

Local machineの個人用Skillは次へcopyします。

```text
~/.claude/skills/stateripple/
```

commitもignoreもしていないProject Skillは未追跡fileになり、`stateripple analyze`は未追跡fileがあるworktreeを拒否します。そのrepositoryを解析する前に、Skill directoryをcommitするか`.gitignore`へ追加してください。

新しいClaude Code sessionを開始します。`/stateripple`で明示的に呼び出すか、descriptionに合うpromptを使用します。Project Skillはteam用にcommitできます。個人用Skillはmachine-localであり、cloud sessionに自動で導入されません。

## 安全な確認

まず解析を実行しない明示的な依頼で発見を確認します。

```text
stateripple skillを使い、必要なStateRipple versionだけを教えてください。解析は実行しないでください。
```

次に使い捨てdirectoryの既知Snapshotで実CLIのsimulationを試します。Agentが`stateripple version`を確認し、`--format json`を使用し、実際の終了コードを扱うことを確認します。`analyze`は使い捨てのcleanなGo repositoryを使用し、未コミット作業があるworktreeで試さないでください。

Host製品の仕様は本packageと独立して変化します。新しいhostへ導入する際は、公式の[Codex Skills guidance](https://developers.openai.com/blog/skills-agents-sdk)と[Claude Code Skills documentation](https://code.claude.com/docs/en/skills)を確認してください。
