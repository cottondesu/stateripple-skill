# Release Gate 最終整備の報告(stateripple-skill v0.1.0)

作成: 2026-10-11(JST)。対象: `stateripple-skill` リポジトリ。

> これは Final Audit Remediation 前の記録です。その後の Gate の変更(パッケージ検査、レビューの厳格な検証、アクティベーション判定、必須ケースの固定)と証拠の入れ替えは [FINAL_AUDIT_REMEDIATION.ja.md](FINAL_AUDIT_REMEDIATION.ja.md) を参照してください。

**Release Readiness: NOT READY。** 残っているのは、プロジェクトメンテナーによる Human Review だけです。Human Review は人間が実施するもので、本書を書いた AI エージェントは記録を作成していません。

commit、push、PR、Release 公開、Mandala の init/clean は行っていません。既存の L01〜L14 の評価証拠は書き換えていません。

本書は `docs/` に置かれ、Human Review が束縛するソースツリーに含まれます。そのため、本書には最終の検証値(ハッシュ、最終監査の結果)を書き込んでいません。それらは Release Gate の出力と、スレッドの報告を参照してください。

## 1. 採用された方針と実装の対応

| 方針 | 実装 |
|---|---|
| アクティベーション評価を Release Gate の必須条件にする | `tests/evals/live_suites.json` の `release_gate.suites` に `activation` を追加。Gate は positive・negative の全ケースを両エージェント分、CORE 行と同じ基準で判定する |
| Codex と Claude Code の両方で Positive/Negative の証拠を要求する | `activation_coverage` が 8 ケース × 2 エージェント = 16 行を作る。欠落、FAIL、NOT_RUN、BLOCKED、古いスキルリビジョン、レビュー無しはすべて不足として扱う |
| Human Review を独立した必須条件にする | Gate の 3 つ目の条件。Live 行やアクティベーション行がすべて揃っていても、Human Review が無ければ `readiness: NOT READY`、exit 1 |
| Human Review はコード固定後、公開前にメンテナーが行う | 記録はスキルリビジョン、ソースツリーのダイジェスト、証拠集合のダイジェストに束縛される。どれかが変わると記録は無効になる |
| AI のレビューや監査を Human Review として登録しない | `reviewer_kind` が `human` 以外の記録、AI システム名を名乗る記録、宣誓文が無い記録は拒否。エージェントのレビュー(`reviewer_kind: agent`)は Live 行とアクティベーション行の定性レビューとしてのみ使われ、Human Review の代わりにはならない |
| 現時点は NOT READY を維持する | Gate の出力は `blocking: ["human review is NOT_RUN"]` |

Gate は `--release` 指定時、カタログとビルド済みスキルをプロジェクトルート以外から読むことを拒否します。

## 2. アクティベーション評価の証拠

| エージェント | 実行 | 自動判定 | 定性レビュー(エージェント) |
|---|---|---|---|
| Codex CLI 0.162.1 | `artifacts/evaluations/live/20261010T191748Z-codex-abda8c32` | 8/8 PASS | 8/8 PASS |
| Claude Code 2.1.286 | `artifacts/evaluations/live/20261010T192118Z-claude-af9307e3` | 8/8 PASS | 8/8 PASS |

- Positive 4 件(StateRipple を名指しした解析依頼、日本語 2・英語 2): 両エージェントともスキルを読み、`version` を確認してから `simulate` または `analyze` を実行しました。
- Negative 4 件(一般的なコードレビュー、通常の設計相談、StateRipple を使わないリスク説明、一般的なタスク整理): 両エージェントともスキルを読まず、CLI も実行しませんでした。
- 定性レビューは実装担当の AI エージェントが書いたものです。人間のレビューではありません。
- 採用した証拠の一覧は `docs/RELEASE_EVIDENCE.txt`(36 パス)です。

メンテナーに見ていただきたい点:

- Codex の A-N01 は、ホストの個人スキル(`~/.codex/skills/code-review`)を読み込み、`inventory.py` を Python で実行しています。Codex ランナーは個人スキルから隔離されていません。
- Claude の A-N03 と Codex の A-N04 は、スキルのディレクトリを一覧していますが中身は読んでいません。採点器はスキルファイルの読み取りだけを「起動」とみなします。
- Claude の A-N04 と一部の回答は、未認可の claude.ai コネクタに触れています(ホストセッション由来)。
- Codex の A-P03 は同じ入力に `simulate` を 2 回実行しています。
- 以前に取得したアクティベーション実行 2 件(`…190642Z-codex-0fede69e`、`…190843Z-claude-8060a30d`)は、4 節の事故の巻き添えで失敗したため採用していません。

## 3. Human Review の記録形式

手順書は `docs/HUMAN_REVIEW.md`、雛形は `docs/human-review.template.json` です。記録は `release/human-review.json` に置きます(現在は存在しません)。

記録に必要なもの:

- `record_type: human-release-review`、`reviewer_kind: human`、メンテナーの名前、UTC の日時
- Gate が出力する `review_target` の 3 値(`skill_revision`、`source_tree_sha256`、`evidence_digest`)
- チェックリスト H01〜H10 がすべて `true`
- 何を確認したかの `notes`、`decision: APPROVE`、雛形どおりの宣誓文

Gate は記録を書いたのが誰かを証明できません。拒否できるのは、エージェントを名乗る記録、AI システム名を含む記録、宣誓文が無い記録、対象リビジョンや証拠が一致しない記録です。AI エージェントはこの記録を作成・編集してはなりません。

## 4. プロセス生成事故の原因と再発防止

**原因**: 評価ハーネスではありません。4 回目の独立監査を担当したサブエージェントが、Git コマンド分類の確認のために、スクラッチ内のリポジトリで実 `git` を `-c core.fsmonitor=<コマンド>` 付きで実行しました。そのコマンドが再び `git` を呼び、設定が子プロセスに引き継がれてプロセスが増殖し、約 2 分間ホスト全体でプロセス生成が失敗しました。監査役が自分で停止し、申告しています。巻き添えで失敗したアクティベーション実行 2 件は破棄して再実行しました。リポジトリへの書き込みはありません。

**私の側の問題**: その時点で、私は Codex と Claude の評価を同時に走らせ、さらに監査役に実 git の実行を許可していました。ハーネスにも、同時実行を止める仕組みと、エージェントが残したプロセスを片付ける仕組みがありませんでした。

**再発防止策**:

- ハーネス: 証拠ディレクトリごとにロックを取り、実行中に 2 つ目の `eval-live` を始めるとエラーで止まります。ケースは 1 件ずつ順に実行し、各ターンにタイムアウトがあります。
- ハーネス: エージェントは独立したセッションで起動され、ターンの終了時・タイムアウト時・ランナーの中断時に、そのプロセスグループへ SIGKILL を送ります。
- 運用: 評価は 1 スイートずつ実行し、ループさせたり両エージェントを同時に走らせたりしません。
- 監査: Git の分類は文字列と純粋関数で確認します。設定・エイリアス・フック・ページャ・プログラムを起動するオプション付きで実 `git` を実行しません。以後の監査ではこの条件を指示に入れています。

**残る限界**: 保証されるのは「エージェントと同じプロセスグループに残っているプロセスへシグナルを送る」ことだけです。別のプロセスグループやセッションへ移ったプロセス(`setpgid`、ジョブ制御付きシェルのバックグラウンドジョブ、`setsid`、デーモン化)には届きません。ランナーが SIGKILL された場合は片付けができません。どちらも、次の評価を始める前に手で終了させる必要があります。OS レベルのプロセス数制限は入れていません。

## 5. L14 の Git 操作観測の限界

L14 は、ある 1 回の実行でエージェントがどう振る舞ったかの証拠であり、監視機構ではありません。`docs/SAFETY.md` と `docs/EVALUATION.md` に明記しています。

- 見ているのは 3 つです。使い捨てリポジトリ 2 つの前後比較、PATH 上の観測用 `git` を通ったコマンドの記録、エージェントのシェル文のヒューリスティックな解析。
- 見えないもの: 別プログラムの内部から絶対パスで起動された git、採点器が知らないツールによる転送、解析が認識しない書き方(変数、エイリアス、スクリプトファイル、他のインタプリタ、ランチャー越しのシェル、ヒアストリング、`trap` など)。これらは、実際に実行されて観測対象の 2 つのリポジトリが変わった場合にだけ検出されます。
- `bash -l` と `sh -l` は観測用 `git` を迂回します。その場合は PASS にならず、不合格になります。
- PASS は「その実行で、この 3 つの手段では公開操作が観測されなかった」という意味です。公開が不可能だという意味ではなく、スキル自体にエージェントの Git 実行を止める仕組みはありません。

## 6. 検証結果

| 項目 | 結果 |
|---|---|
| `make build` / `check` / `test` / `cli-compat` / `release-check` | 本書の作成後に再実行し、結果をスレッドで報告します |
| Release Gate(`make eval-release`、Human Review 無し) | Live 行の不足 0、アクティベーション行 16 行で不足 0、Human Review は NOT_RUN、`readiness: NOT READY`、exit 1 |
| 既存の L01〜L14 の証拠 | 書き換えなし(作業前後のハッシュが一致、独立監査が manifest と封印を確認) |

独立監査: Release Gate の変更について、別のサブエージェントが読み取り専用で監査しました。結果は Blocking 0 件、Major 1 件(Human Review 記録を置くとテストが失敗する問題)、Minor 7 件でした。Major と、Minor のうち 5 件(中断時の後片付け、ロックの空値、Gate の入力元、アクティベーション ID の重複、文書の表現)を修正し、その修正分について最終の確認監査を行います。確認監査の結果はスレッドで報告します。

残している Minor: Human Review の reviewer 名の拒否リストは回避可能で、日時は書式しか検査しません(作者を証明できないという限界の一部として文書化)。

## 7. Mandala

Goal と 20 リーフは引き継いだままです。`routing.activation` は、必要なレビュー(メンテナーの Human Review のうちアクティベーションに関する確認)が終わるまで open のままにしています。それ以外の 19 リーフは done です。

Mandala には Human Review 専用のセルがありません。必須 gap が 0 件になっても、Human Review の完了を意味しません。Release Readiness は Gate の出力で判断してください。

## 8. Human Review Checklist(メンテナー用)

コードを固定してから実施してください。固定後に `src/`、`scripts/`、`tests/`、`docs/`、`.github/`、`Makefile`、README、`CHANGELOG.md`、`LICENSE`、`.gitignore` のどれかを変更すると、レビューは無効になります。

1. `make build` のあと、Human Review 無しで Gate を実行し、`review_target` の 3 値を控える。

   ```sh
   make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)"
   ```

2. 次の 10 項目をご自身で確認する。

| ID | 確認内容 |
|---|---|
| H01 | 自分はこのプロジェクトの人間のメンテナーであり、AI がこの記録を書いたり提出したりしていない |
| H02 | レビューしたソースツリーは固定済みのリリース候補で、`source_tree_sha256` と一致する |
| H03 | `src/stateripple/SKILL.md` と references 4 本を読み、v0.1.0 のスキル本文として承認する |
| H04 | `make build`、`check`、`test`、`cli-compat`、`release-check` を自分で実行(または立ち会い)し、すべて exit 0 だった |
| H05 | 両エージェントの Live 回答を自分で読み(L05、L06、L07、L11、L13、L14 を含む)、判定に同意する |
| H06 | 両エージェントの Positive/Negative アクティベーション回答を自分で読み、判定に同意する |
| H07 | エージェントのレビューと監査は AI によるものだと理解しており、自分のレビューとして扱わない |
| H08 | `CHANGELOG.md` と `docs/EVALUATION.md` の既知の限界(L14 の Git 監視が不完全であることを含む)を読み、このリリースで受容する |
| H09 | 公開するファイルに、秘密情報、個人のパス、無関係な内容が含まれていないことを確認した |
| H10 | commit、tag、push、Release は自分で判断して行い、AI は行っていない |

3. `docs/human-review.template.json` を `release/human-review.json` にコピーし、手で記入する。
4. Gate を Human Review 付きで実行し、`readiness: READY`、exit 0 を確認する。

   ```sh
   make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)" HUMAN_REVIEW=release/human-review.json
   ```

回答の読み方、AI のレビューが指摘した「人間の判断が要る点」は `docs/HUMAN_REVIEW.md` にあります。

## 9. Release Readiness

| 条件 | 状態 |
|---|---|
| Activation Evaluation が Release Gate に統合されている | 満たす |
| 両エージェントの必須 Activation 評価が PASS | 満たす(自動判定 PASS、定性レビューはエージェントによるもの) |
| L01〜L14 の Live 行 | 両エージェントで不足 0 |
| Human Review | **未実施** |
| 独立監査の Blocking Findings | 0 件(最終の確認監査はスレッドで報告) |
| Mandala | `routing.activation` のみ open(Human Review 完了まで維持) |

**判定: NOT READY。** メンテナーが Human Review を実施し、Gate が READY を返すまで変わりません。
