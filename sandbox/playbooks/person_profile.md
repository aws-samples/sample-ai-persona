# 定石: person_profile（履歴を人ごとの 1 行にまとめて、人を分ける）

## いつ使うか

- 1 人に複数行がある履歴の表（購買・利用ログ・勤怠・問い合わせ・アンケートの複数回答など）があるとき。
- 計画に `person_profile` の手順があるとき。履歴の中身（何を、いつ、どれだけ）で人を分けたい。
- 列の名前や業種は問わない。型と値の種類の数で、列を自動で見分ける。

## 手順

1. `read_table("source_n")` で履歴の表を読む。人の列は `data_reading` の `person_id`、特徴にしない列は `skip` を使う。
2. `person_profile(df, id_column, skip=[...])` で、人ごとの 1 行の表を作る。
   - `回数（行）`、日付の列があれば `最後からの日数`・`期間（日）`・`活動日数`・`土日の割合`、時刻があれば時間帯の割合（`0〜5時の割合` など）。
   - 数値の列は `<列>の合計`・`<列>の平均`、値が 2〜20 種類の列は `<列>=<値>の割合`（上位 5 値）。
   - ID・コード・値がすべて違う文字の列・`skip` の列は入らない。
3. 1 人 1 行の表（`data_reading` の kind が people の表）に、人の列で `merge` する（`how="left"`）。
   - 1 人 1 行の表がない（履歴の表しかない）ときは、`person_profile` の結果をそのまま使い、`emit_result` の母数を `people:source_n`（`id_column` は `data_reading` の `person_id`）にする。人の ID が空の行は、どの人にも数えない（件数は記録に出る）。
4. 作った特徴の列で層に分ける。数が多いときは、層ごとの違いが大きい列を選ぶ。
   - 例: 数値の列を標準化して `KMeans` で分ける。層の数はペルソナの数と同じにする。
   - 列の値が欠けた人（履歴がない人）の欠損を、平均などで埋めて層の名前や特徴にしない。
5. `emit_result` を 1 回だけ呼ぶ。`metrics` に、人ごとの特徴の列を渡す。

## emit_result の形

```python
history = read_table("source_2")
profile = person_profile(history, "customer_id", skip=["customer_id", "store_id"])
people = read_table("source_1").merge(profile, on="customer_id", how="left")

from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

features = [c for c in profile.columns if c != "customer_id"]
matrix = StandardScaler().fit_transform(people[features].fillna(0))
people["segment"] = [f"層{n + 1}" for n in KMeans(n_clusters=5, n_init=10, random_state=42).fit_predict(matrix)]
emit_result(
    people, "segment", "source_1", id_column="customer_id", metrics=features,
)
```

- 履歴の表しかないときは、`people = profile` として割り当て、`emit_result(people, "segment", "people:source_2", id_column="customer_id", metrics=features)` とする。
- 履歴がない人は、特徴が欠けたままになる。クラスタリングの計算のためだけに 0 で埋めてよいが、`metrics` に渡す列は欠けたままにする。
- 層の名前は、`emit_result` のあと、特徴の違い（結果の特徴の表）を見て付ける。

## 使う関数

- `person_profile`（テスト済み）、`read_table`、`emit_result`。`join_tables` は、履歴の表を軸にした結合のときだけ。
