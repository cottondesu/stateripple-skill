# stateripple-skill v0.1.0 — Round 4 Security Assurance Remediation 報告

対象: Codex Round 3 独立監査(TECHNICAL AUDIT FAIL、Blocking 0 / Major 2 / Minor 2)の指摘 FR-01〜FR-04。
作成者: 実装を行った AI エージェント(Claude Code)。本書は Human Review ではありません。
レビュー対象のダイジェスト(skill_revision / source_tree_sha256 / evidence_digest)は、本書自身がソースツリーに含まれるため本書には書かず、スレッドの報告に記載します。

## A. Executive Summary

| 指摘 | 重大度 | 判定 | 要点 |
|---|---|---|---|
| FR-01 Codex 本体がサンドボックス外 | MAJOR | VERIFIED FIXED | 両エージェントをプログラム全体として OS サンドボックス下で起動。Codex は内蔵サンドボックスを切り、外側のプロファイル1枚で本体・内蔵ツール・子孫プロセスを制限 |
| FR-02 probe が一般的な失敗を拒否成功と扱う | MAJOR | VERIFIED FIXED | 専用プログラムが実際のシステムコールを行い errno を報告。EPERM / EACCES のみを拒否と認め、終了コード・タイムアウト・stderr 文字列は証拠にしない |
| FR-03 Replay の Manifest 検証が Gate より弱い | MINOR | VERIFIED FIXED | Gate と Replay が同じ検証関数を共有 |
| FR-04 cleanup の例外が元の結果を置き換える | MINOR | VERIFIED FIXED | cleanup は例外を出さず、結果を記録に残す |

- 残存 Blocking 0 / Major 0 / Minor 2(K 節)。内部の独立レビューは 4 回実施し、最終状態は Blocking 0 / Major 0。
- 単体テスト 189 件すべて成功(基準 156 件から 33 件増)。ResourceWarning なし。
- 新しい Live 証拠を両エージェント × (L01〜L14 + 活性化 8 件) = 44 件で取得し、Replay 44 件はすべて PASS。
- Release Gate は NOT READY。理由は Human Review 未実施のみ(I 節)。**Release Readiness: NOT READY**。
- 作業中に2件の事故がありました(K 節 K-3、K-4)。いずれも検出後に除去し、実害は確認していませんが、隠さず記載します。

## B. Mandala Coverage Plan(Phase 1)

Mandala CLI v0.4.0 を読み取り専用で使用しました(`show --json`、`gaps --required --json`)。`init` / `add` / `mark` / `done` / `clean` は一度も実行していません。

- Goal: stateripple-skill v0.1.0 を、検証済みの CLI 契約・安全なルーティング・信頼できる結果解釈・再現可能な評価・文書化されたリリース準備とともに提供する(既存 Goal、変更なし)。
- 葉セル 20 個。Required Open は `release.audit` と `routing.activation` の 2 個。
- 今回の指摘が該当するセル: `routing.safety`(FR-01、FR-02)、`release.codex`(FR-01)、`verification.coverage`(FR-02、FR-04)、`verification.replay`(FR-03)。これらは既に `done` でしたが、`done` を根拠にせず実装とテストを見直しました。状態は変更していません。

## C. FR-01 Agent Sandbox Architecture

**構成。** `claude` と `codex` はどちらも `/usr/bin/sandbox-exec -f <ツリー>.write-guard.sb` を前置して起動します(`scripts/writeguard.py` の `agent_argv`)。プロファイルは「すべてのファイル書き込みを拒否し、列挙した場所だけ許可」です。

| | Claude Code | Codex |
|---|---|---|
| サンドボックスの内側 | `claude` 本体とその子孫すべて | `codex` 本体、内蔵のファイル編集ツール、その子孫すべて |
| 書き込み可能 | 評価ツリーの中、一部のデバイス、セッションディレクトリ 1 個(`~/.claude/projects/<ワークスペースパス由来の名前>`) | 評価ツリーの中、一部のデバイス |
| エージェント自身の状態 | 一時ファイルはツリー内 `host-state/` | `CODEX_HOME` をツリー内 `host-state/codex-home/` に向け、ログイン情報のコピーだけを置く |
| 内蔵サンドボックス | 対象外 | 無効化(macOS は異なるプロファイルの入れ子を拒否するため)。外側のプロファイルが唯一の制限 |

**ランナー側の起動もすべて制限下。** バージョン取得、各ターン(再開ターン含む)、エージェント実行後の Git 読み取り(L14)はすべて同じ前置きで起動します。ランナーがサンドボックス外でエージェントプログラムを起動する箇所はありません(旧 `executable_and_version` は削除)。

**プロファイルとツリーの保護(内部レビュー 3 回目の Blocking への対応)。** 当初プロファイルはツリー内にあり、Claude 側ではツリーをセッションディレクトリへ移動してリンクに差し替えることで、次の起動を無制限にできました(レビュアーが再現)。対応:
- プロファイルをツリーの隣(`<ツリー>.write-guard.sb`)に置き、どのプロファイルからも書けない場所にした。
- ツリー自身のエントリへの書き込み(改名・削除・差し替え)を拒否する規則を追加。
- probe に `profile-append` / `profile-unlink` / `tree-move` を追加し、毎ケースで拒否を確認。
- ランナーはツリー作成時の識別子(デバイス番号と inode)を記録し、各ターン前と Git 実行前に一致を確認。不一致ならそのケースを中止。

**ランナー自身の動作。** ランナーはサンドボックス外で動くため、エージェントが用意しうる場所での操作をリンクに追従しない形に書き直しました: ツリー削除とセッションディレクトリ削除(親ディレクトリのディスクリプタ相対、`O_NOFOLLOW`、深い入れ子は持ち上げて処理)、ログインコピーの読み取り、2 ターン目用スナップショットの差し替え、ツリー内ファイルの一覧化(リンクは辿らず、読めないものは「変更」として扱う)。これは OS による強制ではなくコードの性質であり、テストで確認しています。

**保証範囲。** 保証するのは「エージェント本体とその子孫による、ツリー外(上記セッションディレクトリを除く)へのファイルシステム経由の直接書き込みを OS が拒否する」ことです。封じ込めではありません(K 節)。

## D. FR-02 Probe Verification

- `scripts/write_probe.py` が実際のシステムコール(作成、ディレクトリ作成、追記、削除、子プロセスからの作成、改名)を行い、操作ごとに成功か errno を JSON で報告します。
- ケースごとに 2 回実行します。(1) サンドボックスなしで使い捨てフィクスチャの対照部分へ書き込み、すべて成功すること。(2) エージェントと同じ前置き・同じプロファイルの下で、ツリー内 1 件は成功し、ツリー外 14 件(`/tmp` 側 5、一時ディレクトリ側 5、ツリー内リンク経由 1、プロファイル 2、ツリー移動 1)がすべて `EPERM` または `EACCES` で拒否されること。その後ランナー自身がフィクスチャ・ツリー・プロファイルが変わっていないことを確認します。
- 拒否と認めるのは「probe プログラムが終了コード 0 で、このケースの nonce を返し、該当操作について errno が EPERM / EACCES」の場合だけです。終了コード 42 などの非ゼロ終了、シグナル、タイムアウト、起動失敗、不正な出力、別 nonce、別の errno(`ENOENT`、`EINVAL` など)、`Operation not permitted` という文字列は拒否と認めず、ケースは BLOCKED になりエージェントは起動されません。
- 証拠(`observations.write_guard`、形式 2)はケース ID・エージェント・実行ごとの nonce(`run_id`)・プロファイルのダイジェスト・probe プログラムのダイジェスト・前置きに結び付けられ、Grader は記録された全ターンのコマンドラインがランナーの生成するものと一語ずつ一致することも確認します。ホストの絶対パスは記録しません。
- 今回の Live 44 件すべてで、制限下の 14 操作が `EPERM` で拒否されました。確認できた OS の拒否原因は `EPERM` のみで、`EACCES` は実機では観測していません(受理条件としては残しています)。
- **主張しないこと。** nonce とハッシュは偶発的な取り違えや古い証拠の流用を防ぐもので、記録を書き換えて封印し直す協調的な改ざんへの耐性は保証しません。ホームディレクトリは対照書き込みが実ホームへの書き込みになるため probe していません(プロファイルは「全拒否+列挙した例外」の単一規則です)。

## E. FR-03 Replay Manifest Validation

`manifest_problems` を `scripts/eval_common.py` に移し、Release Gate と Replay の両方が同じ関数で検証します。Replay は、種類が live でない Manifest、Manifest に載っていないファイル、ハッシュ不一致、封印の不一致、複数リビジョンの混在、単独の記録ファイルを拒否します。旧形式の Manifest(`selected_cases` なし)は引き続き検証できます。

## F. FR-04 Cleanup Review

`cleanup_case` は例外を出さず、起きたことを文字列のリストで返します。ケースが完了した場合は記録の `process.cleanup` に保存され、それ以外の経路では標準エラーに出すだけで、元の結果や例外を置き換えません。ログインコピーが改ざんされた、または比較できなかった場合は `host-state-intact` チェックでそのケースを FAIL にします。ランナーが完了できなかったケース(ツリー内のログが壊れている等)は BLOCKED として記録し、残りのケースは続行します。

## G. Test Results(順次実行)

| コマンド | 結果 |
|---|---|
| `make build` | PASS |
| `make check` | PASS(22 契約) |
| `make test` | 189 件 OK、ResourceWarning なし(`-W error::ResourceWarning` でも成功) |
| `STATERIPPLE=<実 CLI> make cli-compat` | PASS(stateripple v0.1.0) |
| `make release-check` | PASS(7 項目) |
| Live / Replay 再採点 | Replay 44 件 PASS |
| Manifest 全エントリ検証 | Gate 内で実施、問題なし |
| Release Gate | NOT READY(Human Review 未実施のみ) |

追加した回帰テストの主なもの: 終了コード 42・一般エラー・拒否風の文字列・起動失敗・タイムアウトを拒否と認めない(test_02〜06)、ツリー外書き込みが成功したら不合格(test_08)、別ケース・別エージェント・別ポリシー・別 nonce の証拠を拒否(test_09〜11)、封印し直しても無効な証拠は無効(test_14)、実サンドボックス下でのプロセスツリー全体の拒否、ツリー移動とプロファイル書き換えの拒否、リンクを植えられたツリーの削除、ログインコピーのリンク非追従、実 Git を実プロファイル下で実行。Git 環境分離のテストは変更なく成功しています。

## H. Live / Replay / Manifest Evidence

採用した証拠(`docs/RELEASE_EVIDENCE.txt`):

| スイート | Live | Replay | 件数 |
|---|---|---|---|
| Claude release | `20261013T150739Z-claude-a78abe90` | `20261013T183915Z-edddb979` | 14 |
| Claude activation | `20261013T183633Z-claude-64a517fd` | `20261013T183915Z-1c786420` | 8 |
| Codex release | `20261013T151327Z-codex-6277ff73` | `20261013T183915Z-3f8adf4a` | 14 |
| Codex activation | `20261013T152453Z-codex-246d9705` | `20261013T183916Z-3e39ff11` | 8 |

- Live は 1 スイートずつ順次実行しました(並列なし)。全 44 件で自動判定 PASS、cleanup 記録は空、ホスト側の残留物(プロファイル、評価ツリー、セッションディレクトリ)は増えていません。
- **採用しなかった新規実行(記録は保存)。** Claude activation は 3 回実行しました。1 回目 `20261013T151137Z-claude-1ce7346c` は A-N03 / A-N04 がエージェント側の利用上限(session limit)で FAIL。2 回目 `20261013T183359Z-claude-16bd06ac` は A-N01 が FAIL(スキルを使わない依頼で、エージェントが全ファイルに `wc -c` を実行し、スキルファイルも対象に含まれたため、厳格な判定が「スキルファイルを読んだ可能性あり」とした)。判定基準は変更していません。3 回目を採用しました。A-N01 が 2 回の有効な試行のうち 1 回失敗した事実は残存リスクです(K-2)。
- 定性レビューは AI(実装エージェント)が全 44 件の回答とツールログを読んで作成しました。Human Review ではなく、Human Review の代わりにもなりません。
- **既存証拠の扱い。** Round 3 までの Live / Replay / Manifest / レビューは削除も変更もしていません(作業前に記録した 994 ファイルの SHA-256 がすべて一致)。Round 3 の証拠は、形式 2 の probe 証拠を持たず、Codex のコマンドラインが旧形式(本体がサンドボックス外)であるため、現行 Grader の `no-external-writes` を満たさず、採用対象から外しました。

## I. Release Gate & False Positive Tests

- Gate 結果: `readiness: NOT READY`。両エージェントの release / activation、Grader、Replay、Manifest、現行 skill revision の条件は満たし、残る理由は Human Review 記録が無いことだけです。AI は `release/human-review.json` を作成していません(`release/` ディレクトリは存在しません)。
- 誤って PASS にならないことのテスト: probe 証拠なし、旧形式、他ケースの証拠、保存済み `problems` あり、操作の欠落・重複、コマンドラインの 1 語違い、改ざんされたログインコピー、壊れた Manifest、Replay ディレクトリを Live として渡す、など。

## J. Review Target Digests

スレッドの報告に記載します(本書を含むソースツリーのダイジェストのため)。skill_revision は Round 3 から変わっていません(スキル本体は未変更)。

## K. Remaining Risks & Findings

**K-1(設計上の限界、文書化済み)。** サンドボックスが制限するのはファイル書き込みだけです。次は制限・観測していません: 読み取り全般(エージェントはユーザーのファイルを読めます)、ネットワーク(Codex のコマンドも外部通信可能)、既存サービスへのローカルソケット、他プロセスへのシグナル、システムサービスへの依頼(`open`、`osascript`、`launchctl`、キーチェーン、ペーストボード等)経由の動作、Claude のセッションディレクトリ内への書き込み。評価ツリーにはケースの間 Codex のログイン情報のコピーがあり、エージェントは読むことができ、ネットワークも開いています。プロセスグループを抜けたプロセスはターン後も(制限下のまま)生き残れます。拒否された書き込みの試行そのものは観測していません。macOS 以外では Live は BLOCKED です。

**K-2(MINOR、未解決)。** Claude の否定系活性化ケース A-N01 が、厳格な判定の下で 2 回中 1 回 FAIL しました。スキル内容がコンテキストに入った証拠はありませんが(出力はバイト数の合計のみ)、判定は保守的に FAIL とします。Codex 独立監査での再実行時にも再発しうる揺らぎです。

**K-3(事故、対処済み)。** 内部レビュー用のサブエージェント 1 体と、私自身の事前実験が、実ホームを差し替えないまま Codex のログインファイルを作業用スクラッチ領域にコピーしました。内容の表示・送信はしていません。スクラッチ全体を走査し、バイト一致するコピー 2 個を削除しました。以後のレビューでは偽のホームを必須にしました。念のため Codex へのログインし直し(トークン更新)を検討してください。

**K-4(事故、対処済み)。** 新しい `tree-move` probe のテストを偽ホームなしで実行したため、テスト用の一時ディレクトリ 1 個(中身は probe の空ファイルとリンクのみ)が実際の `~/.claude/projects/` に移動しました。直後に検出し、その 1 個だけを削除しました。既存の会話記録には触れていません。テストは偽ホームを使うよう修正し、probe 側も移動が成功した場合はツリーを元に戻すようにしました。

**K-5(MINOR、未解決)。** `STATERIPPLE_EVAL_RETURN_CODEX_LOGIN=1`(既定は無効)を設定した場合のみ、ツリー内で更新されたログインをユーザーのファイルへ書き戻します。形の検査では本物の更新と偽造を区別できません。既定では書き戻さないため、長時間の実行後に Codex への再ログインが必要になることがあります。今回の実行では無効のままで、cleanup 記録に更新の通知はありませんでした。

**K-6(情報)。** `~/.claude/projects/` に過去ラウンドの評価で作られたセッションディレクトリが約 150 個、一時ディレクトリに過去の評価ツリー 1 個(`stateripple-live-l06-…`)が残っています。過去の記録の削除は禁止事項のため触れていません。今回の実行では新たな残留はありません。

**K-7(情報)。** テストの一部はソースコード文字列の位置関係を確認する弱い形です(最終レビュー指摘 C)。挙動は別のテストで確認していますが、強化の余地があります。

## L. Independent Security Review

実装者とは別のサブエージェントによる内部レビューを 4 回行いました(Human Review でも Codex 独立監査でもありません)。

| 回 | 判定 | 主な指摘 | 対応 |
|---|---|---|---|
| 1 | 実行不可 | ランナーがエージェント由来のバイト列をユーザーのログインへ書く、スナップショット差し替えがリンクを辿る、実行後 Git と版数取得がサンドボックス外、など 13 件 | すべて修正 |
| 2 | 実行不可 | Blocking: ツリー削除時にログインコピーをパスで消すため、植えられたリンク経由でユーザーの実ログインを消せる。他 Minor 6 件 | すべて修正 |
| 3 | 実行不可 | Blocking: Claude 側でツリーを移動しプロファイルを書き換えられる(再現あり)。Major 2 件、Minor 4 件 | すべて修正(C 節) |
| 4(最終状態) | **実行可**、Blocking 0 / Major 0 | Minor 1(弱いテスト)、Note 3 | 追加のコード変更なし |

4 回目は、両エージェントの実プロファイル下でのプロファイル改変・ツリー差し替え・ツリー外書き込み(ハードリンク、大文字小文字違い、`/tmp` 別名、`renamex_np` の交換など)がすべて `EPERM` になること、生き残ったプロセスと競合させてもランナーがツリー外を読まず書かず消さないこと、FR-02 の受理条件を実験で確認しました。4 回目の後にスクリプトとテストは変更していません(変更したのは証拠一覧 `docs/RELEASE_EVIDENCE.txt` と本書のみ)。Live 44 件はこの監査済みの状態で実行しました。

## M. Mandala Coverage Result(Phase 9)

最終確認も読み取り専用です。`gaps --required --json` は終了コード 1 で、Required Gap は `release.audit` と `routing.activation` の 2 個のまま。状態は一切変更していません。

| 確認点 | 結果 |
|---|---|
| FR-01 の修正範囲(両エージェント、本体と子孫) | 実装・テスト・Live 証拠あり |
| FR-02 終了コード 42 等を拒否と認めない | テストと最終レビューで確認 |
| probe とケースの結び付け | nonce・ケース・エージェント・ポリシーで確認 |
| Manifest の整合(Gate と Replay) | 共有関数で確認 |
| cleanup の例外処理 | テストで確認 |
| Live 証拠の鮮度(現行 revision、新形式) | 44 件採用 |
| Git 環境分離の非退行 | 既存テスト成功 |
| プロセス安全性(並列なし、無制限生成なし) | 順次実行、ロックあり |
| Gate の異常系 | テスト成功 |

新たに見えたギャップ: (1) ランナー自身は OS に制限されておらず、リンク非追従はコードの性質であること。(2) 否定系活性化ケースの揺らぎ(K-2)。(3) 読み取りとネットワークが開いたままのログインコピー(K-1)。いずれも Mandala のセルには追加していません(状態変更の依頼がないため)。`release.audit` は Codex の独立監査と Human Review が済むまで、`routing.activation` は活性化の確認が独立に済むまで open のままです。

## N. Codex Independent Audit Handover

確認していただきたい点:
1. `scripts/writeguard.py` の `sandbox_profile` / `agent_argv` / `guard_prefix` と、Codex が `--dangerously-bypass-approvals-and-sandbox` で起動されることの妥当性(外側プロファイルが唯一の制限)。
2. `probe_problems` / `guard_problems` が EPERM / EACCES 以外を受理しないこと(終了コード 42 の再現を含む)。
3. Claude 側のセッションディレクトリという例外と、ツリー移動の拒否。
4. ランナーのリンク非追従処理(`scripts/gitenv.py` の `remove_tree` / `read_regular_file`、`eval_live.py` の `replace_fixture` / `require_same_tree`)。
5. `docs/EVALUATION.md` の「Writes outside the evaluation tree」節が実装より強い主張をしていないこと。
6. A-N01 の揺らぎ(K-2)と、採用しなかった 2 回の実行の扱い。
7. K-1 の限界を踏まえ、`no-external-writes` という契約名と保証範囲が妥当か。

再現手順: `make build`、`make check`、`make test`、`STATERIPPLE=<実 CLI のパス> make cli-compat`、`make release-check`、`make eval-release EVIDENCE="$(tr '\n' ' ' < docs/RELEASE_EVIDENCE.txt)"`。Live の再取得は 1 スイートずつ順に `python3 scripts/eval_live.py --agent <claude|codex> --suite <release|activation>`。

Git の commit / push / tag / PR / リリース公開、Human Review 記録の作成は行っていません。
