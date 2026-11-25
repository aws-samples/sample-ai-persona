# 定石: compare_groups（群どうしの差を検定する）

## いつ使うか

- 指示が「A と B の違い」「〜の層で何が違うか」のように、群どうしの比較を求めるとき。
- `analyze_dataset` の集計だけでは、差が偶然かどうかを言えないとき。

## 手順

1. `read_table("source_n")`（結合するなら `join_tables`）で表を読む。
2. 群の列を作る（例: 年代、プラン、購入の有無）。
3. `compare_groups(df, group_column, columns)` で検定する。
   - 数値の列: Welch の t 検定と効果量（Cohen の d）。群が 3 つ以上なら、群ごとに「その群とそれ以外」を比べる。
   - カテゴリの列: カイ二乗検定と効果量（Cramér の V）。
   - 返り値は `[{name, column, value, p_value, effect_size, ...}]`。
4. 差の大きい列（p 値が小さく、効果量が大きい列）を、層の特徴として使う。
5. 群を層として `emit_result` を 1 回だけ呼ぶ。検定の結果は `statistics` に渡す。

## emit_result の形

```python
df = read_table("source_1")
df["group"] = ["30代以下" if a < 40 else "40代以上" for a in df["age"]]
stats = compare_groups(df, "group", ["monthly_spend", "plan"])
emit_result(df, "group", "source_1", id_column="customer_id", statistics=stats)
```

- 根拠（Evidence）の `statistic` に書く数字は、返った結果の人数（size）・母数（population）と、そこから出した割合だけにする。
- `statistics`（検定の値・効果量）と `features` はコードが渡した値で、ライブラリは数え直さない。これらの数字を根拠の `statistic` に書くと、その根拠は除かれる。

## 使う関数

- `compare_groups`（テスト済み）、`read_table`、`emit_result`。
