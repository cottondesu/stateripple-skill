# stateripple-skill

[English](README.md)

[StateRipple](https://github.com/cottondesu/stateripple) v0.1.0を使用し、提供されたGraphの決定論的Risk伝播をsimulateする、または対応するGoリポジトリのコミット済み変更をanalyzeするAgent Skillです。

SkillはCLIモードを選択し、前提を検査し、実際のStateRippleバイナリを実行し、JSONを過剰解釈せずに報告します。計算とリポジトリ解析はStateRippleが担当し、このプロジェクトは伝播Engineを再実装しません。Mandalaは開発Coverageの追跡にのみ使用し、実行時依存関係には含めません。

## 機能

- `simulate`: Git、Go、リポジトリを必要とせず、提供されたschema v1 Snapshotを解析します。
- `analyze`: cleanな単一root Go moduleで、base revisionと`HEAD`を比較します。
- Processの終了コードと、JSON Schema、status、stop reasonを組み合わせて検証します。
- Risk、不完全結果、同点cause、wave履歴、test target投影、結果の鮮度を安全に解釈します。

SkillはStateRippleを自動インストールせず、入力を変更せず、独自の解析結果を計算せず、未コミット変更を解析せず、テストを実行せず、失敗した`analyze`を`simulate`へ切り替えません。

## 要件

- StateRipple CLIが厳密に`v0.1.0`であること。
- CodexまたはClaude Codeなど、Agent Skills対応host。
- `analyze`のみ: `--no-lazy-fetch`対応のGit 2.45.0以上、Go 1.23以上、cleanで対応可能なリポジトリ。

StateRipple CLIとSkillは別々に導入します。個人用・プロジェクト用の配置は[インストール手順](docs/INSTALLATION.ja.md)を参照してください。

## 使用例

明示的な依頼が最も確実な発火条件です。

```text
StateRippleで./snapshot.jsonのRisk伝播をsimulateして。
StateRippleでHEAD~2からHEADまでのコミット済みGo変更をanalyzeして。
```

実行するCLIの形は次のとおりです。

```sh
stateripple simulate --input ./snapshot.json --format json
stateripple analyze --base HEAD~2 --format json
```

Riskは伝播した優先度であり、確率ではありません。Risk 0は安全の証明ではありません。`status: complete`は利用可能なSnapshotに対する計算完了であり、現実の依存関係をすべて含む証明ではありません。StateRippleは`test_targets`を選択しますが、テストを実行しません。

## 開発と検証

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

Live Agent Evaluationは明示的に実行し、通常のCIには含めません。

```sh
make eval-live AGENT=codex SUITE=release
make eval-live AGENT=claude SUITE=release
```

[評価](docs/EVALUATION.md)、[CLI互換性](docs/CLI_COMPATIBILITY.md)、[安全契約](docs/SAFETY.md)も参照してください。Skillの正本は`src/stateripple/`で、`dist/stateripple/`は生成物です。

リリース承認には[独立AI Release Review](docs/AI_RELEASE_REVIEW.ja.md)または任意の[Human Review](docs/HUMAN_REVIEW.md)を使用します。Human Reviewは必須ではありません。すべての技術検証を通れば、独立AIのAPPROVEだけでREADYになります。自動GraderのPASSやReplayのAgent Reviewだけでは承認されません。両方を指定した場合は両方が有効なAPPROVEである必要があり、一方の失敗を他方で補完しません。独立性と証拠確認の保証範囲は[手順書](docs/AI_RELEASE_REVIEW.ja.md#保証範囲)を参照してください。READYはGitHubへの公開を実行しません。

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)" AI_REVIEW=release/ai-release-review.json
```

過去の監査報告のHuman必須やNOT READYは当時の対象についての記録として保持します。新しいSource Digestへの承認には流用しません。

## 対応範囲

本releaseはStateRipple tag `v0.1.0`、commit `2334ba7573c1d0b4565189c93f780b35e08ea81d`、schema 1、`risk-propagation-v1`に固定します。後続versionを暗黙に互換とは扱いません。

## License

[MIT](LICENSE)、copyright 2026 cottondesu。
