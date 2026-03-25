"""マッピング機能検証用のサンプルCSV生成（カラム名が標準と異なる）"""

import csv
import random
import uuid

random.seed(123)
N = 500

GENDERS = ["男", "女"]
JOBS = [
    "営業職", "事務職", "ITエンジニア", "販売スタッフ", "管理職",
    "フリーランス", "パート", "学生", "主婦", "医療従事者", "教員",
]
AREAS = ["東京都", "大阪府", "愛知県", "福岡県", "北海道", "宮城県", "広島県", "神奈川県", "埼玉県", "千葉県"]
FAMILY = ["独身", "既婚子なし", "既婚子あり（1人）", "既婚子あり（2人以上）"]
INTERESTS = [
    "映画鑑賞", "読書", "旅行", "料理", "スポーツ", "ゲーム", "音楽",
    "カフェ巡り", "写真", "DIY", "ガーデニング", "アウトドア", "ショッピング",
]
CONCERNS = [
    "健康管理", "老後の資金", "子供の教育", "キャリアアップ", "住宅ローン",
    "ワークライフバランス", "人間関係", "スキルアップ", "転職", "副業",
]

rows = []
for _ in range(N):
    gender = random.choice(GENDERS)
    age = random.randint(20, 69)
    job = random.choice(JOBS)
    area = random.choice(AREAS)
    family = random.choice(FAMILY)
    hobby = "、".join(random.sample(INTERESTS, k=random.randint(2, 4)))
    concern = random.choice(CONCERNS)
    profile = f"{area}在住{age}歳{gender}性。{job}として働く。趣味は{hobby}。"

    rows.append({
        "顧客ID": str(uuid.uuid4())[:8],
        "性別コード": gender,
        "年齢層": age,
        "お仕事": job,
        "お住まい": area,
        "家族構成": family,
        "趣味・関心事": hobby,
        "最近の悩み": concern,
        "顧客プロフィール": profile,
        "会員ランク": random.choice(["ゴールド", "シルバー", "ブロンズ", "一般"]),
        "年間購入額": random.randint(10000, 500000),
    })

out = "test_data/sample_custom_personas_500.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"Generated {len(rows)} rows -> {out}")
