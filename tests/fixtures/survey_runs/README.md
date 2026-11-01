# ResultWriter の出力の見本

`SurveyRunnerService.iter_results` のテストが使う見本。Step Functions の Distributed Map の
ResultWriter が書く形（manifest と、成功・失敗ごとの結果のファイル）を、AWS の文書
（Step Functions 開発者ガイド「ResultWriter (Map)」）の形で作った。dev の 1,000 件の試験の
出力そのものは手元にないため、ビルドとテストの段階で dev の実際の出力と比べる。

- `manifest.json`: `ResultFiles` の `SUCCEEDED`・`FAILED`・`PENDING` に結果のファイルのキー。
  キーの `<prefix>` は、テストが S3 に置くときに実際の場所へ置き換える。
- `SUCCEEDED_0.json`: 子の実行 1 件ごとに `Input`（JSONL の 1 行の JSON 文字列）と
  `Output`（`ResultSelector` で残した応答の `content` の JSON 文字列）。
  1 件目は画像なし（`body` を持つ）、2 件目は画像あり（`body_s3_uri` だけを持つ）、
  3 件目は応答がスキーマに合わない（読めない JSON）。
- `FAILED_0.json`: 再試行が尽きた 1 件。`Error` と `Cause` を持ち、`Output` を持たない。

ペルソナの属性と回答は架空の値。
