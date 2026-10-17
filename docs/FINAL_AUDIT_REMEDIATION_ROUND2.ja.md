# Final Audit Remediation Round 2 報告

作成日: 2026-10-13(UTC)。作成者: Claude Code(実装を担当した AI。人間ではありません)。

Release Readiness は **NOT READY** です。技術的な Gate の残りは Human Review だけで、これはメンテナ本人が行います。commit、push、tag、PR、Release は行っていません。

`review_target`(`skill_revision`、`source_tree_sha256`、`evidence_digest`)の最新値は、この文書自体が Source Digest の対象なのでここには書きません。次のコマンドの出力が正です。

```sh
make build
make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)"
```

## 1. R-001〜R-006 の対応状況

| ID | 重大度 | 状況 | 要点 |
|---|---|---|---|
| R-001 | MAJOR | 修正済み | Activation を「依頼の文面」ではなく「ホストが返した実行結果」で判定します。 |
| R-002 | MAJOR | 修正済み | 子プロセスの環境を `scripts/gitenv.py` の 1 か所で作り、全経路で使います。 |
| R-003 | MINOR | 修正済み | 全終了経路で、プロセスグループの終了、子の回収、パイプの close を上限付きで行います。 |
| R-004 | MINOR | 修正済み | `docs/HUMAN_REVIEW.md` から旧 L06 の記述を除き、`docs/RELEASE_EVIDENCE.txt` を唯一の参照先にしました。 |
| R-005 | MINOR | 修正済み | `.Rhistory` を `.gitignore` と release-check の hygiene に追加しました。ファイルは削除していません。 |
| R-006 | MINOR | 修正済み | Manifest の照合を Release Gate の必須条件にしました。 |

### R-001 Activation の誤検知

原因は、`cat .../SKILL.md` という文字列がコマンドに含まれるだけで「読んだ」と判定していたことです。

- Runner は、各ツール呼び出しとその結果を対にして記録します(`tool_calls`)。Claude は `tool_use` と `tool_result`、および Skill 呼び出し成功後にホストが渡す本文、Codex は `item.completed` の `aggregated_output`、`exit_code`、`status` です。
- 各結果を、ワークスペースに設置した Skill ファイルの本文と行単位で照合し、一致行数を記録します。出力そのものは保存しません(サイズとハッシュのみ)。
- 「ロードした」と認めるのは、呼び出しが完了し、エラーでなく、終了コードが 0 で、設置した `SKILL.md` の特徴行の 80% 以上が実際に返り、かつ依頼が設置したコピーを指していると読み切れる場合だけです。
- 依頼として認めるのは 3 種類です。`stateripple` への Skill 呼び出し、設置パスの Read、そして「名前を表示するだけのコマンド」と「ファイル全体を表示するコマンド(`cat`、`nl`、`less`、`more`、`bat`)」だけで構成され、読む対象がすべて設置した Skill ディレクトリ内にあるシェル依頼です。
- `false && cat`、`true || cat`、`echo`、`ls`、パスへの言及、出力を捨てた `cat`、`head` で切った出力、失敗したコマンド、結果のない呼び出しは、いずれも PASS になりません。
- 結果を記録していない旧形式の記録、型や件数が不正な記録は、Positive・Negative のどちらでも FAIL です。Gate はそれを未達行として扱います。
- 形の壊れた記録は、例外ではなく FAIL(`record-shape`)になります。

### R-002 Git 環境の隔離

原因は、release-check が起動する unit test やビルド、CLI 互換チェックの fixture 準備が、呼び出し元の環境をそのまま継承していたことです。

`scripts/gitenv.py` の `safe_environment()` が次を行います。

- 継承した `GIT_*` をすべて捨てます(`GIT_CONFIG_COUNT`、`GIT_CONFIG_KEY_*`、`GIT_CONFIG_VALUE_*`、`GIT_CONFIG_PARAMETERS`、`GIT_DIR`、`GIT_EXTERNAL_DIFF` などを含みます)。
- `SSH_AUTH_SOCK`、`SSH_ASKPASS`、`GH_TOKEN`、`GITHUB_TOKEN`、`PAGER`、`EDITOR`、`VISUAL`、`LESSOPEN`、`LESSCLOSE` も捨てます。
- global と system の Git 設定を無効にし、プロンプトと optional lock を止め、transport を `file` だけにします。
- 自前の `GIT_CONFIG_COUNT` で、fsmonitor、hooks、pager、credential helper、自動 maintenance、`ext` transport、署名を無効にします。孫プロセスの Git にも届きます。

### R-003 Popen の資源リーク

`gitenv.terminate_process_group` が、プロセスグループへの SIGKILL、5 秒上限の `communicate`、パイプの close、5 秒上限の `wait` を行います。回収済みの子や close 済みのパイプでも例外を出さず、再実行しても無害です。`eval_common.run_process` と `gitenv.run_bounded`(release-check と CLI 互換チェックが使用)が、正常終了、Timeout、KeyboardInterrupt の 3 経路すべてでこれを呼びます。

### R-006 Evidence Manifest

設計を確認したところ、Manifest は Replay でしか照合されておらず、Gate は記録ファイルを直接読んでいました。記録単体の封印では「どの実行の成果物か」を保証できないため、照合を Gate の必須条件にしました。

Gate は、渡された各記録が、同じディレクトリの封印済み Manifest に、現在のハッシュと Case ID で 1 回だけ載っていることを確認します。別ディレクトリへコピーした記録、完了した実行に属さないファイル、種類や schema version や Agent が違う Manifest、symlink はブロックします。既存の保証は弱めていません。

## 2. 修正したファイル

| 種別 | ファイル |
|---|---|
| 新規 | `scripts/gitenv.py` |
| スクリプト | `scripts/eval_common.py`、`scripts/eval_live.py`、`scripts/eval_coverage.py`、`scripts/release_check.py`、`scripts/check_cli_compat.py` |
| テスト | `tests/test_evaluation.py`、`tests/test_cli_compat.py` |
| 文書 | `docs/EVALUATION.md`、`docs/HUMAN_REVIEW.md`、`docs/RELEASE_EVIDENCE.txt`、`CHANGELOG.md`、この文書 |
| 設定 | `.gitignore` |

Skill 本体(`src/stateripple/`)は変更していません。`skill_revision` は前回と同じ `647893531631f505fb649875830d75656686ac92aa8aace1dbb8e5d44b7556c2` です。

## 3. 追加・書き直した回帰テスト

| 対象 | テスト |
|---|---|
| R-001 | `test_skill_load_is_judged_from_what_the_tool_returned`、`test_claude_skill_and_read_calls_need_a_successful_result`、`test_unexecuted_or_unproven_loads_do_not_pass_positive_cases`、`test_only_the_installed_workspace_copy_counts_as_the_skill_body`、`test_negative_activation_fails_on_any_access_to_skill_files`、`test_negative_cases_fail_on_any_skill_call_or_delivered_skill_text`、`test_negative_cases_need_a_complete_tool_log`、`test_malformed_tool_records_fail_both_kinds_of_case`、`test_records_of_the_wrong_shape_are_failures_not_crashes`、`test_live_runner_keeps_the_tool_log_in_the_record` |
| R-002 | `test_safe_environment_carries_no_inherited_git_state`、`test_every_child_process_gets_the_safe_environment`、`test_no_script_or_test_starts_a_child_with_an_inherited_environment`、`test_release_check_commands_are_bounded_and_git_is_isolated` |
| R-003 | `test_every_exit_path_collects_the_child_and_closes_its_pipes`、`test_cleanup_is_bounded_and_can_be_repeated`、`test_release_check_collects_its_children_on_every_path`、`test_bounded_runner_reports_timeouts_and_launch_failures_as_results` |
| R-005 | `test_stray_local_files_are_ignored_and_never_published` |
| R-006 | `test_gate_requires_each_evidence_file_in_the_manifest_of_its_run` |

R-002 の検証は、辞書と差し替えたプロセス起動関数だけで行っています。危険な設定を実際の Git に渡すテストはありません。

## 4. 再実行した検証

すべて逐次で実行しました。

| 検証 | 結果 |
|---|---|
| `make build` | PASS |
| `make check` | PASS(契約 22 件) |
| `make test` | PASS(126 件) |
| `make cli-compat`(`STATERIPPLE` に絶対パスを指定) | PASS(15 件) |
| `make release-check` | PASS(7 項目) |
| `python3 -W error::ResourceWarning -m unittest ...` | PASS(126 件、警告なし) |
| `make eval-release` | exit 1、`NOT READY`、blocking は `human review is NOT_RUN` のみ |

Gate の内訳は、Live gaps 0、Activation gaps 0、package PASS、evidence_manifests PASS(88 件を照合)、human_review NOT_RUN です。

## 5. ResourceWarning

解消しました。原因は、割り込み経路でプロセスグループを kill したあと、子を回収せずパイプも閉じなかったことです。テストスイート全体を `ResourceWarning` をエラー扱いにして実行し、警告が出ないことを確認しました。正常終了、Timeout、KeyboardInterrupt の各経路で、子が回収されパイプが閉じていることも個別にテストしています。

## 6. Git 環境隔離の有効範囲

有効な範囲:

- release-check の全子プロセス(2 回のビルド、静的検証、unit test、自身の Git 呼び出し)
- CLI 互換チェックの StateRipple CLI 実行と fixture 用 Git
- Live Runner の fixture 準備、Git 状態確認、Agent 本体
- unit test が直接起動するプロセス

有効でない範囲:

- `make build`、`make check`、`make test`、`make cli-compat` 自体は呼び出し元の環境で始まります。それらが起動する子は安全な環境になります。
- 子が自分で Git 変数を設定し直した場合と、Git のコマンドラインに `-c` を渡した場合は、その指定が優先されます。
- リポジトリ内の設定のうち、固定していない項目(filter、textconv、alias、include など)はそのまま効きます。
- 評価対象の Agent は安全な環境を受け取りますが、自分が起動するコマンドでは変更できます。
- `PATH`、`HOME` などはそのまま継承します。
- 外部 diff プログラムは環境の設定では固定していません(空文字が全コマンドで安全な「無効」とは限らないため)。継承した `GIT_EXTERNAL_DIFF` は捨て、global 設定は無効です。

## 7. Activation 検知の有効範囲

OS レベルのファイルアクセス監視はありません。観測しているのは「ホストが Agent に返した出力」です。そのため次の限界があります。

- 出力がツール結果に現れない読み取りは、Positive ではロードと認めません。Negative では、依頼が Skill を名指ししない限り検出できません。
- ツール呼び出しを経ずにホストが本文を注入した場合は、user ロールのテキストブロック以外では検出できません。
- Skill 呼び出しと Read は、依頼が指すファイルをホストが返したものとして信頼します。
- 出力の照合は行の完全一致です。大文字化、接頭辞の付加、折り返し、エンコード、1 行への連結をされると認識できません。
- 80% のしきい値は、数行欠けた読み取りを許容します。
- Grader は Runner が記録した一致行数を信頼します。出力はハッシュでしか残らないため、再計算はできません。
- 封印は鍵なしのダイジェストです。偶発的な変更は検出できますが、意図的な偽造は防げません。
- 判定は fail-closed なので、正当な実行が FAIL になる形があります(`sed -n 1,200p SKILL.md`、Skill ディレクトリへ `cd` してからの相対パスなど)。その場合は再実行が必要です。

詳細は `docs/EVALUATION.md` に記載しています。

## 8. Live 証拠

### 既存の 44 件

新しい Grader で再判定した結果、44 件すべてが `skill-activation` で FAIL でした。旧形式の記録はツールの実行結果を持たず、ロードの有無を判定できないためです。判定は緩めていません。既存の記録は 1 件も変更・削除していません(作業前に控えた 566 ファイルのハッシュと一致)。現在は履歴として残っています。

### 新規に取得した証拠

結果を記録する Runner で、44 件すべてを取り直しました。実行は 1 本ずつ、Claude、Codex の順です。

| 実行 | 件数 | 結果 |
|---|---|---|
| Claude release `20261013T002207Z-claude-aee50bf9` | 14 | 全件 PASS |
| Claude activation `20261013T002602Z-claude-2d15980a` | 8 | 7 件 PASS、A-P01 は FAIL |
| Claude A-P01 再実行 `20261013T004151Z-claude-855440bd` | 1 | PASS |
| Codex release `20261013T002811Z-codex-adc73b9e` | 14 | 全件 PASS |
| Codex activation `20261013T003639Z-codex-d6072f39` | 8 | 全件 PASS |

Claude A-P01 の FAIL は Agent 側の実際の失敗です。stderr の捨て先を `/dev/null` ではなく `/tmp/null_err_unused` と書き、ワークスペース外に空ファイルを作りました。Grader はこれを `no-external-writes` で正しく FAIL にしました。この記録は失敗として残し、1 件だけ再実行して PASS を得ています。ホストの `/tmp/null_err_unused`(0 バイト)は削除していません。

採用した 44 件はすべて、回答とツール結果を私が読んで Agent レビューを書き、最終版の Grader で Replay を作りました。採用パスは `docs/RELEASE_EVIDENCE.txt` にあります(Live 44 件、Replay 44 件)。採用しなかった実行(動作確認の 2 件、途中版の Replay)も削除せず残しています。

これらのレビューは AI によるものです。Human Review ではありません。

## 9. 独立内部監査

実装者とは別のレビュアー(サブエージェント)が、読み取り専用で 2 回監査しました。実 Git への危険な設定の投入、Live 評価の実行、リポジトリへの書き込みは禁止し、どちらも守られています。

| 回 | 結果 | 対応 |
|---|---|---|
| 1 回目(Round 2 の実装全体) | Blocking 0、Major 0、Minor 8、Note 9 | Minor はすべて修正しました。 |
| 2 回目(1 回目の修正差分) | Blocking 0、Major 0、Minor 4、Note 7 | Minor はすべて修正し、Note は修正または文書化しました。 |

両回とも、元の R-001(`false && cat` と `true || cat`)と R-002(注入した Git 設定の継承)が再現しないことを確認しています。2 回目は採用した Live 44 件を独立に再判定し、全件 PASS、Manifest 88 件 PASS を確認しました。

2 回目の指摘への修正(依頼の読み切り条件の厳格化など)のあとは、内部監査を行っていません。回帰テストと 44 件の再判定は通っていますが、ここは Codex の再監査で見ていただきたい点です。

## 10. Mandala

`mandala gaps --required --json` の結果は次のとおりです(v0.3.0)。

```json
{"schema_version":1,"gaps":[{"id":"release.audit","required":true},{"id":"routing.activation","required":true}]}
```

- `release.audit` は open のままです。Codex による独立再監査が PASS するまで完了にしません。
- `routing.activation` は open のままです。Human Review の Activation 確認が終わるまで完了にしません。

再初期化、`clean`、`state.json` の直接編集は行っていません。

## 11. 未解決の問題と既知の限界

1. Human Review が未実施です。`release/human-review.json` は作成していません。
2. Activation は OS レベルの監視ではなく、出力の観測に基づきます(7 章)。
3. Git 環境の隔離には 6 章の範囲外があります。
4. 記録と Manifest の封印は鍵なしです。実行ディレクトリ全体の偽造は検出できません。
5. Codex の `tool_uses` と `tool_commands` は、開始と完了の両方のイベントで記録されるため 2 重になります。採用済み証拠の形式を揃えるため直していません。呼び出し回数と使用回数の一致は検査していません。
6. プロセス起動の AST ガードは綴りの検査です。別の書き方での起動がないことの証明ではありません。
7. プロセスグループを離れた孫プロセスは kill できません。その場合の待ち時間の上限は「timeout + 5 秒」です。
8. `.mandala/` は `.gitignore` に入っていません。追跡すると release-check の hygiene が FAIL になります。扱いはメンテナの判断です。
9. リポジトリ直下の `.DS_Store`、`src/.DS_Store`、`.Rhistory` は ignore 済みで、削除していません。

## 12. Codex に再監査してほしい項目

1. `eval_common._request_names_installed_body`: 設置コピーを指す依頼の判定に、まだ抜け道がないか。内部監査後に厳格化した部分です。
2. `eval_common.extract_agent_events`: Claude と Codex の実ストリームに対して、呼び出しと結果の対応付けが正しいか。特に Skill 本文の帰属。
3. `eval_common.tool_log_problems` と `_call_malformed`: 不正な記録が fail-open する経路が残っていないか。
4. `gitenv.safe_environment`: 取りこぼしている変数や、Git の正常動作を壊す設定がないか。
5. `gitenv.run_bounded` と `terminate_process_group`: リーク、上限を超える待ち、pid 再利用の競合。
6. `eval_coverage.evidence_manifest_status`: 回避方法がないか。
7. 採用した Live 44 件と Replay 44 件の独立した再判定、および AI レビューの記述と記録内容の整合。
8. `docs/EVALUATION.md` の記述が、コードの実際の動作を超えていないか。
