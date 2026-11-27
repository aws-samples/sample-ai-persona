# 定石: join（共通の ID 列で表を結合する）

## いつ使うか

- 計画の入力に JoinPlan（軸の表、付ける表、キーの列、一致の割合）があるとき。
- 1 行 1 人の表（軸）に、1 人に複数行がありうる表（購買履歴など）の情報を足して、人ごとの層を作りたいとき。
- JoinPlan がない表は結合しない。表ごとに `analyze_dataset` と `cluster_segments` の `source` で別々に分析する。

## 手順

1. `join_tables(axis, key, others)` で軸の表に左結合する。
   - `axis`: 軸の表の名前（例: `"source_1"`）。
   - `key`: キーの列（JoinPlan の `key_column`）。
   - `others`: 付ける表ごとに `{"source": "source_2", "aggregations": [{"column": "amount", "func": "sum"}, {"column": "bought_on", "func": "last"}]}`。
   - 使える集計: `count`、`count_distinct`、`sum`、`mean`（`avg`）、`min`、`max`、`first`（最初の値・日付）、`last`（最後の値・日付）。
2. 結果は 1 行 1 人の pandas DataFrame。付けた列の名前は `<表>_<列>_<集計>`（例: `source_2_amount_sum`、`source_2_購入金額_sum`）。
   - 日本語の列名はそのまま残る。名前に使えない文字（空白や記号）だけが `_` になる。
   - 名前がぶつかったら、後の列に `_2`・`_3` が付く。正確な名前は `df.attrs["joined_columns"]` で確かめる。
   - 付けた表ごとに `<表>_rows`（その人の行数。一致しない人は 0）と `<表>_matched`（一致したか）が足される。
   - 一致しなかった人も残る。付けた列は空になる。消さずに「一致なし」の層として扱う。
3. 結合した表の上で、層の割り当ての列を作る（しきい値・分位・クラスタリングなど）。
4. `emit_result` を 1 回だけ呼ぶ。

## emit_result の形

```python
df = join_tables("source_1", "customer_id", [
    {"source": "source_2", "aggregations": [{"column": "amount", "func": "sum"}]},
])
df["segment"] = ["高額" if v and v >= 10000 else ("一致なし" if not m else "通常")
                 for v, m in zip(df["source_2_amount_sum"], df["source_2_matched"])]
emit_result(df, "segment", "join:source_1+source_2", id_column="customer_id")
```

- `population_source` は `join:<軸>+<付けた表>`（付けた順。2 つ付けたら `join:source_1+source_2+source_3`）。
- `id_column` は軸の表の人の ID の列。母数は軸の表の人数で、ライブラリが数え直す。
- 結合した列は `df.attrs["joined_columns"]` から自動で記録される。

## 使う関数

- `join_tables`（テスト済み）、`read_table`、`emit_result`。
