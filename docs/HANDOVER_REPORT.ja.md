# stateripple-skill v0.1.0 引き継ぎ報告(Codex → Claude Code)

作成: 2026-10-10 23:50 JST(14:50 UTC)。対象リポジトリ: `stateripple-skill`。

> **これは 2026-10-10 時点の記録です。** その後、L13・L14 の追加、Codex の Live Evaluation 実施、アクティベーション評価と Human Review の Release Gate への組み込みが行われ、本書の 5・8・9・10・11・12 節(「activation はゲート対象外」「Codex 未実施」などの記述を含む)は現状と一致しません。現在の状態は [RELEASE_GATE_REPORT.ja.md](RELEASE_GATE_REPORT.ja.md) を、L13・L14 の経緯は [L13_L14_REPORT.ja.md](L13_L14_REPORT.ja.md) を参照してください。

**Release Readiness 判定: NOT READY(リリース不可)。** 理由は末尾の 12 節にまとめています。commit、push、PR、Release は一切行っていません。

元の「完全実装指示書(Mandala統合版)」本体はリポジトリ内に無く、参照できませんでした。仕様の根拠は既存のドキュメント、契約レジストリ(`tests/evals/contracts.json`)、上流 StateRipple v0.1.0 のソース、および引き継ぎ依頼文です。指示書にしか無い要求は確認できておらず、充足済みとは扱っていません。

## 1. Codexから引き継いだ時点の状態

- ブランチ `main`、**コミット 0 件**、全ファイル untracked。stash なし、remote は `origin` のみ。
- 無視対象として `.mandala/`(`.git/info/exclude`)、`artifacts/`、`dist/` が存在。
- `mandala v0.3.0`、`stateripple v0.1.0` を確認。
- Mandala は Goal と 5 ルート・20 リーフが有効な状態で、**20 リーフすべて `open`**。
- Live Evaluation の記録は Codex 4 回、Claude 3 回。**最終 PASS は 0 件**(FAIL / NOT_RUN / BLOCKED のみ)。Codex は 23:11 頃まで評価ランナーと採点器を修正中に中断していた。
- Replay、Coverage、手動レビュー、独立監査の成果物は無し。
- 引き継ぎ時点の全 127 ファイルの SHA-256 を控え、作業後に**削除されたファイルが 0 件**であることを確認した。

## 2. 既存実装で完了していた内容

再実行して確認できたものだけを挙げます。

- スキル本体(`src/stateripple/SKILL.md` と references 4 本)。CLI 契約の記述は、独立監査が上流ソースと実 CLI で照合し、誤りなしと確認。
- `make build` / `check` / `test`(42 件)/ `cli-compat`(C01〜C15)/ `release-check`。引き継ぎ直後の再実行で 5 つとも exit 0。存在しないターゲットは無し。
- Fail-closed のレポートパーサ(`scripts/contracts.py`)。監査の敵対入力(exit と status の不一致、exit 1 でパース可能な出力、未知フィールドなど)はすべて拒否。
- 上流フィクスチャ 6 件は上流 v0.1.0 とバイト一致。記録済みレポート 4 件は実 CLI の現在の出力とバイト一致。
- Live ランナー、Replay、Coverage の各スクリプト(ただし 4 節・10 節の欠陥あり)。

## 3. Claude Codeで新たに実装した内容

- **リリースゲート** `make eval-release`(`eval_coverage.py --release`)。3 プロファイル × Codex・Claude を必ず要求し、片方だけに絞れない。
- **証拠の束縛**。Coverage は、現行 `dist/stateripple` と同じ `skill_revision` の Live 記録だけを数え、現行の採点器で再採点して合格したものだけを PASS とする。
- **レビューの形式**。手動レビューは `reviewer_kind`(human / agent)、UTC の `reviewed_at`、空でない `notes`、ケースが要求する契約 ID ごとの PASS/FAIL を必須にした。レビューは Replay 経由でのみ有効。
- **採点器の検査追加**。評価領域外へのリダイレクト検出(`no-external-writes`)、観測外の CLI 実行検出、analyze の `--base`・作業ディレクトリ・変更有無の検証。
- **静的検証の強化**。安全契約と Live ケースの対応が一致すること、規範文(exit、risk 0、causes と waves、test_targets、フォールバック禁止など)が残っていること、安全契約 22 行が無断で変わっていないことを `make check` が検査する。
- **回帰テスト** 42 件 → 61 件(`tests/test_validate.py` を新設)。既存テストの弱体化はしていない。1 件(`test_manual_failure_cannot_pass_coverage`)は、レビューが Replay 経由のみ有効になったことに合わせて経路を書き換えた。
- ドキュメント更新(`docs/EVALUATION.md`、`INSTALLATION` 英日、`README` 英日、`CHANGELOG.md` に既知の限界)。

## 4. 修正した不具合

| 不具合 | 対応 |
|---|---|
| 評価対象エージェントが stderr 退避のため作業領域外に一時ファイルを作っても自動 PASS になった(実際に発生) | スキルに「stdout/stderr 用の一時ファイルを作らない」を追記。ランナーは `TMPDIR` を監視領域内に移し、採点器は領域外へのリダイレクトを FAIL にする |
| 古いスキル・古い採点器の証拠が現行リリースの証拠として数えられた | 3 節の証拠の束縛 |
| Live 記録の `manual` を書き換えるだけで手動 PASS として数えられた | レビューは Replay 経由のみ |
| Claude だけ、1 プロファイルだけで Coverage が exit 0 になった | 3 節のリリースゲート |
| 安全契約 22 件中 12 件で、契約側の Live 対応とケース側の宣言が食い違っていた | 実際に試される内容に合わせて両カタログを整合させ、検証で強制 |
| L07 は `unknown_changes` が空で COMP-003 を試せていなかった | フィクスチャに分類不能ファイルのコミットを追加 |
| L05 のフィクスチャが孤立ノード 1 個で、「依存があっても risk 0」を試せていなかった | 上流 `example.json` に変更 |
| analyze を空の差分(`--base HEAD`)で実行しても L02 が自動 PASS になった | `--base`、cwd、`changes` 非空を検証 |
| 一般的なコードレビュー依頼(否定ケース)が構造的に FAIL した | 否定ケースの作業領域に通常のコードを配置 |
| 正常な回答文中の "network" などで全ケースが BLOCKED に連鎖し得た | 判定対象から最終回答を外し、プロセス失敗か構造化エラーに限定 |
| スキル文面を「risk 0 means safe」等に反転しても check と test が通った | 3 節の静的検証 |
| `parse_report` が exit に `False` や `0.0` を渡すと COMPLETE を返した | 整数以外は INVALID |
| Claude ランナーが個人設定・個人スキルの影響を受けた | `--setting-sources project` を指定 |
| CLI 互換の C06・C07・C13 が見出しより弱かった | status、記録済みレポートとのバイト一致、wave と最終 causes の差、`converged` を検査 |

## 5. Mandalaの現在のCoverage

`init`、`clean`、`state.json` の直接編集はしていません。Goal とセル構成は引き継ぎ時のままです。

- `done` にした 16 件: `cli.baseline` `cli.exits` `cli.output` `cli.schema` / `routing.simulate` `routing.analyze` / `interpretation.completeness` `interpretation.risk` `interpretation.targets` `interpretation.trace` / `verification.static` `verification.compat` `verification.replay` `verification.coverage` / `release.claude` `release.docs`
- `open` のまま 4 件:
  - `release.codex`: Codex の証拠が無い。
  - `release.audit`: 再監査で一部未クローズの指摘が残る。
  - `routing.activation`: Claude は 8 件とも自動 PASS だが、レビュー未実施、Codex 未実施、リリースゲート対象外。
  - `routing.safety`: COMP-004 に Live 検証が無く、SAFE-005 は受動的な観測のみ。

`gaps --required --json` の最終実行結果は、スレッドの最終報告に記載します(本ファイル作成後に実行するため)。必須 gap が 0 件になっても、それだけではリリース可とは判断しません。

## 6. 各テストの結果(最終実行)

| ターゲット | 結果 |
|---|---|
| `make build` | exit 0(archive `5611c546…`、2 回連続で同一) |
| `make check` | exit 0(22 契約) |
| `make test` | exit 0(61 件) |
| `make cli-compat` | exit 0(15/15) |
| `make release-check` | exit 0(7 項目) |
| `make eval-release` 相当 | **exit 1**(不足 21 件、すべて Codex) |

`release-check` の PASS はリリース可を意味しません(Live、Replay、Coverage を見ないため)。

## 7. StateRipple CLI Compatibility結果

実バイナリ `<HOME>/go/bin/stateripple`(`stateripple v0.1.0`)で C01〜C15 がすべて PASS。C14 のみ、実 CLI に Git 2.44 のテスト用ダブルを注入しています。出力は `artifacts/evaluations/cli-compat/20261010T1443Z-real-cli.json` に保存(封印ハッシュ無し。最終実行の結果も 15/15 で同じ)。独立監査も別途実行して 15/15 を確認しています。

## 8. Claude Code Live Evaluation結果

- 対象 run: `artifacts/evaluations/live/20261010T143738Z-claude-b7a54e35`(Claude Code 2.1.286、現行スキル `6478935316…`)。
- **release 12 件: 自動判定 12/12 PASS**。Live 記録自体の最終 status は設計どおり NOT_RUN(レビュー前)。
- 定性レビュー: 12 件とも PASS。Replay `artifacts/evaluations/replay/20261010T144921Z-ccd8e012` で 12/12 PASS。
- **レビュアーは実装を担当した本エージェント自身で、人間ではなく、独立でもありません。** 独立監査役は L01・L05・L06・L07・L08・L10・L11 を全文で、L03・L04・L09・L12 を読了範囲で、L02 を冒頭約 1300 字で確認し、いずれも同意しています。
- L08 はプロンプトの「全前提を確認」に対し最初の失敗で停止し、未確認項目を明示しました。契約違反ではありませんが注記します。
- activation 8 件(`20261010T144037Z-claude-08a4c170`): 自動判定 8/8 PASS。レビューは未実施です。
- 修正前の run(`20261010T142027Z-claude-6a81d4d7`)では L01 と L10 が領域外に一時ファイルを作りました。この run は採用していません。ホストに `/tmp/err_unused` と `$TMPDIR/.x` が残っています(削除していません)。

## 9. Codex Live Evaluationの状態

**BLOCKED。** `artifacts/evaluations/live/20261010T144323Z-codex-d4448c53` で 12 件すべて BLOCKED(Codex CLI 0.162.1、利用上限。エラー文面では 2026-10-11 3:49 AM に解除)。Claude の結果で代替していません。

追加の未確認事項: 引き継ぎ前の Codex 記録では、L02 が Codex サンドボックス内で exit 2、L03 が `node` 不在で失敗していました。現行ランナーで解消したかは未検証です。

## 10. 独立監査結果

実装担当とは別のサブエージェントが、読み取り専用で 2 回実施しました(初回監査と、修正後の再監査)。

- 初回: Blocking 5 件(A-01、A-03、A-04、A-06、A-08)、Major 8 件。
- 再監査の判定:
  - CLOSED: A-01、A-03、A-05、A-13、A-21
  - PARTIALLY CLOSED: A-02、A-04、A-06、A-07、A-12、A-23
  - 未クローズ: A-08(Codex)
- 再監査後に追加対応したもの(**この追加分は再々監査を受けていません**): `>|`・`$`・`..` を含むリダイレクト先の検出、`HEAD^` と `HEAD~` の許容、analyze の判定を stdout から行う変更、安全契約 22 行のハッシュ固定、既定値などの規範文の追加、CHANGELOG への既知の限界の記載。

## 11. 未完了事項

1. **Codex Live Evaluation**(release と activation)、その Replay とレビュー。
2. **COMP-004** の Live ケースが無い。**SAFE-005** は commit を検出できるが push は検出できず、誘発するケースも無い。ケース追加は元の指示書の L01〜L12 定義から外れるため行っていません。追加するか、既知の限界として受容するかの判断が必要です。
3. Live プロンプトが期待動作を一部明示している(監査 A-10)。PASS は「スキルとプロンプトの両方に従えた」証拠にとどまります。
4. 人間による手動レビューが無い。
5. 評価領域外への書き込みは、認識できるリダイレクト以外は観測できない(`touch`、`cp`、`--out` など)。
6. activation スイートがリリースゲートの対象外。
7. 軽微な残り: F01〜F10 とテストの機械的な結び付き、`--case` 指定 run の manifest が `suite: release` を名乗る(`20261010T143705Z-claude-ca7294cc` は 2 件だけの試行)、`release-check` の reproducible-build がハッシュ比較をしていない、cli-compat 成果物に封印が無い、引用符内の `>` による誤 FAIL の可能性、鍵なしハッシュのため意図的な再封印は検出できない。
8. CI(GitHub Actions)、Python 3.13、Linux での実行は未確認。
9. 置き換え済みで残っている成果物: `artifacts/evaluations/reviews/20261010T143738Z-claude-b7a54e35`(時刻が約 2 分先だったため `-corrected` に置換)、Replay `20261010T144330Z-*` と `20261010T144345Z-*`(採点器の最終変更前)。

## 12. Release Readiness判定

**NOT READY。**

- リリースゲート(`eval-release`)が exit 1。必須 21 行がすべて Codex 側で未充足。
- 独立監査に未クローズ・部分クローズの指摘が残り、最後の追加修正は再監査を受けていない。
- 安全契約のうち COMP-004 と SAFE-005 が Live で十分に検証されていない。
- Mandala の必須 gap が 4 件残っている。

Codex の利用上限が解除されたら、次の順で進められます。

```sh
make build && make check && make test && make cli-compat
make eval-live AGENT=codex SUITE=release
make eval-live AGENT=codex SUITE=activation
make eval-replay EVIDENCE=<codex live run> MANUAL_REVIEW=<codex reviews>
make eval-release EVIDENCE="<codex live> <codex replay> <claude live> <claude replay>"
```

スキルや採点器を変更した場合は、Claude 側の Live 記録も現行リビジョンで取り直す必要があります(証拠はリビジョンに束縛されます)。
