# 独立AIレビューでリリースを承認する

Human Reviewは任意です。独立AI Reviewだけでも、すべての技術検証と承認検証を通ればREADYになります。自動GraderのPASS、ReplayのAgent Review、過去の監査報告だけでは承認されません。

`AI_REVIEW`と`HUMAN_REVIEW`の両方を指定した場合は、両方が有効なAPPROVEである必要があります。どちらかがREJECT、不正、未存在、または古い対象ならNOT READYです。他方へのフォールバックはありません。

## 対象をFreezeする

1. 実装に参加したセッションと変更前の`baseline_source_tree_sha256`を`docs/release-implementation.json`へ記録します。既存の実装セッションを残します。このファイルもSource Digestの対象です。
2. `make build`、`make check`、`make test`、`make cli-compat`、`make release-check`を実行し、出力を保存します。互換CLIがPATH外なら`STATERIPPLE`を指定します。macOSの深いディレクトリのテストには400を超えるFDが必要です。必要なら`ulimit -n 4096`を設定し、OS既定の一時領域を`/tmp`とは別に保ちます。`sandbox-exec`を適用できる実行環境が必要です。
3. `docs/RELEASE_EVIDENCE.txt`の採用証拠を指定し、承認なしでGateを実行します。

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)"
```

技術検証がすべてPASSでも承認がないため終了コードは1です。出力の`review_target`にある`skill_revision`、`source_tree_sha256`、`evidence_digest`を保存します。レビュー開始前に、承認未提出以外の阻害要因を解消します。

ソースを変更したら承認は失効します。レビュー記録と実行証拠は、Source Digestに入らない`release/`へ保存します。除外は記録を無条件に信用するという意味ではありません。

## 別セッションでレビューする

実装担当とは別のAI実行を開始し、ソースを読み取り専用で渡します。凍結対象、変更前ソースまたは差分、採用証拠、テスト出力を渡します。モデルが異なることだけで独立性を証明したとは扱いません。

レビュアーは変更コードと差分、Skillと22安全契約、採用した全Live回答とReplayレビュー、PositiveとNegative Activation、検証出力、既知の限界を読みます。L13の鮮度、L14の無断Git操作、no-external-writes、Manifest全件検証、旧Human経路も確認します。実装担当の説明を実ファイルとログで検証し、レビュー前後のSource Digestを記録します。

実際の確認が終わってから[AIテンプレート](ai-release-review.template.json)に基づき`release/ai-release-review.json`を作成します。空のテンプレートは承認として無効です。AIがH01〜H10へ人間として回答したり、人間用宣誓を記録したりしてはいけません。

## 記録と実行証拠

厳密なフィールド定義は[英語の仕様](AI_RELEASE_REVIEW.md#record-format)と`ai-release-review.template.json`を参照してください。

- Schema 1、`record_type: ai-release-review`、`reviewer_kind: ai`を使用します。実際のレビュアーID、モデル、実行セッション、UTC日時、手法、判断理由が必須です。
- 対象の3ダイジェストはGateの現在値と一致させます。`independence`には凍結済み実装セッション一覧、独立性の根拠、検証限界を記録します。実装セッション自身の承認は拒否されます。
- `evidence_review`の8項目はLive、Replay、Positive Activation、Negative Activation、Package、Manifest、安全契約、既知の限界です。各項目にPASS、具体的な確認メモ、実行ログの行範囲を記録します。
- `safety_contracts`は既存22契約すべてにPASSを必要とします。ケース別には、現在の全ケースと両エージェント、現時点で44組のLiveとReplayのseal、確認メモ、ログ行範囲が必要です。
- FindingにはID、重大度、説明、証拠、状態、解決または受容理由が必要です。OPENは承認を阻害します。BLOCKERとMAJORは証拠付きでRESOLVEDである必要があります。MINORだけは理由付きのACCEPTEDを認めます。
- DecisionはAPPROVEまたはREJECTです。空の判断理由、不正形式、不正な時刻、将来時刻、確認漏れは拒否されます。
- 別の`ai-review-execution`記録に、同一のレビュー実行ID、対象、開始と終了時刻、読み取り専用宣言、レビュー前後のSource Digest、全ソースのパスとSHA-256、採用ManifestのパスとSHA-256、ケース別確認、5つの検証コマンドの終了コードと出力ログを保存します。
- 変更前のSource Digestも凍結済み情報と照合します。確認した差分を`diff`としてハッシュ付きで参照し、差分を再現できる変更前ソースも保全します。
- 実行ログとコマンド出力はパスとraw SHA-256で参照します。承認記録のディレクトリ内の通常ファイルに限定します。絶対パス、親への移動、配下のsymlink、空ファイル、16 MiB超のファイルは拒否します。
- 実行記録と承認記録は、既存の`eval_common.seal_record`方式でsealします。実行記録を先に保存し、そのraw SHA-256を承認へ束縛します。sealの作成自体はレビューではありません。

## 承認を検証する

```sh
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)" AI_REVIEW=release/ai-release-review.json
```

正常時はLiveとActivationのGapが0、PackageとManifestがPASS、AIのDecisionがAPPROVE、HumanがNOT_REQUIRED、Release ReadinessがREADYとなり終了コード0です。Humanも指定する場合は`HUMAN_REVIEW=release/human-review.json`を追加し、[Humanの手順](HUMAN_REVIEW.md)で本人が記録します。AI単独では未記入のHuman記録を指定しません。

READYは承認判定です。Git commit、tag、push、GitHub Releaseを実行しません。公開にはメンテナーからの別の指示が必要です。

## 保証範囲

GateはJSON構造、seal、参照ファイル、ケース網羅、凍結された実装セッションとの不一致、時刻の順序、現在のダイジェスト、実際の技術検証結果を検証します。`independent: true`だけでは通りません。

一方、未署名の実行ログが真実か、実際に別の実行者だったか、コマンドが本当に実行されたか、確認メモの解釈が正しいかを完全には証明しません。全記録を書き換えて再ハッシュできる主体の意図的な偽造は防げません。署名、第三者証明、本人認証は実装していません。利用可能なホスト側の実行ログと照合し、この限界を承認記録にも残します。

Skill本文、Recorderの意味、Grader契約、ケースの意味、Sandboxポリシー、証拠形式が変わらなければ既存LiveとReplayを優先して再利用します。その場合も現在のGraderとManifest検証を通します。採用証拠の改変や再sealでPASSを強制してはいけません。ソース変更後は新しい対象とレビューが必要です。

MandalaのGap 0は宣言済み項目の完了だけを表し、検証や独立監査の代替ではありません。過去の監査報告のHuman必須やNOT READYは当時の記録として保持します。
