# 定石: rfm（購買の新しさ・頻度・金額で顧客を分ける）

## いつ使うか

- 1 人に複数行がある購買・利用の履歴（人の ID、日付、金額）があるとき。
- 指示が「優良顧客」「離反しそうな顧客」「よく買う人」のように、購買の行動で人を分けたいとき。

## 手順

1. `read_table("source_n")` で履歴の表を読む。
2. `rfm(df, id_column, date_column, amount_column, as_of=None)` で、人ごとの指標を作る。
   - `recency`: 最後の日付から `as_of`（省くと履歴の最後の日付）までの日数。
   - `frequency`: 行数。`monetary`: 金額の合計。
   - `r_score`・`f_score`・`m_score`: 1〜5 の点（新しく、よく、多く買う人ほど高い）。
3. 点の組み合わせで層を作る（例: R と F がどちらも 4 以上なら「優良」、R が 2 以下なら「離反の恐れ」）。
4. 1 行 1 人の表として `emit_result` を 1 回だけ呼ぶ。
   - 履歴の表は 1 人に複数行があるので、その表をそのまま母数（`source_n`）にはできない。人ごとの表（1 行 1 人の表）があれば `join_tables` で結合し、結合した表で割り当てる。
   - 履歴の表しかないときは、`rfm` の結果をそのまま渡し、母数を `people:source_n`、`id_column` を `data_reading` の `person_id` にする。人の ID が空の行は、どの人にも数えない（件数は記録に出る）。

## 履歴の表しかないときの emit_result の形

```python
scores = rfm(read_table("source_1"), "customer_id", "bought_on", "amount")
scores["segment"] = [
    "優良" if r >= 4 and f >= 4 else ("離反の恐れ" if r <= 2 else "その他")
    for r, f in zip(scores["r_score"], scores["f_score"])
]
emit_result(
    scores, "segment", "people:source_1", id_column="customer_id",
    metrics=["recency", "frequency", "monetary"],
)
```

## 人ごとの表があるときの emit_result の形

```python
scores = rfm(read_table("source_2"), "customer_id", "bought_on", "amount")
df = join_tables("source_1", "customer_id", [{"source": "source_2", "aggregations": [{"column": "amount", "func": "sum"}]}])
df = df.merge(
    scores[["customer_id", "recency", "frequency", "monetary", "r_score", "f_score"]],
    on="customer_id",
    how="left",
)
df["segment"] = [
    "優良" if (r or 0) >= 4 and (f or 0) >= 4 else ("離反の恐れ" if (r or 0) <= 2 else "その他")
    for r, f in zip(df["r_score"], df["f_score"])
]
emit_result(
    df, "segment", "join:source_1+source_2", id_column="customer_id",
    metrics=["recency", "frequency", "monetary"],
)
```

- `metrics` に、人ごとに計算した数値の列（`recency`・`frequency`・`monetary` など）を渡すと、層ごとの平均が「AI の計算」の特徴として画面と人物像の材料に出る。購買の行動で分けたときは、必ず渡す。

## 使う関数

- `rfm`（テスト済み）、`join_tables`、`read_table`、`emit_result`。
