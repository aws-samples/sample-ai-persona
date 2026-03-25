"""10,000件のサンプルペルソナCSVデータを生成するスクリプト"""

import csv
import random
import uuid

random.seed(42)
N = 10000

SEXES = ["男性", "女性"]
OCCUPATIONS = [
    "会社員（営業）", "会社員（事務）", "会社員（技術）", "公務員", "自営業",
    "フリーランス", "パート・アルバイト", "学生", "主婦・主夫", "経営者",
    "医師", "看護師", "教師", "エンジニア", "デザイナー", "研究者", "農業従事者",
]
REGIONS = ["北海道", "東北", "関東", "中部", "近畿", "中国", "四国", "九州・沖縄"]
PREFECTURES = {
    "北海道": ["北海道"],
    "東北": ["青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県"],
    "関東": ["東京都", "神奈川県", "千葉県", "埼玉県", "茨城県", "栃木県", "群馬県"],
    "中部": ["愛知県", "静岡県", "新潟県", "長野県", "石川県", "富山県", "福井県", "山梨県", "岐阜県"],
    "近畿": ["大阪府", "京都府", "兵庫県", "奈良県", "滋賀県", "和歌山県", "三重県"],
    "中国": ["広島県", "岡山県", "山口県", "鳥取県", "島根県"],
    "四国": ["香川県", "愛媛県", "徳島県", "高知県"],
    "九州・沖縄": ["福岡県", "熊本県", "鹿児島県", "大分県", "宮崎県", "佐賀県", "長崎県", "沖縄県"],
}
MARITAL = ["未婚", "既婚・子供なし", "既婚・子供あり"]
EDUCATION = ["高校卒", "専門学校卒", "大学卒", "大学院卒"]
HOBBIES_POOL = [
    "読書", "映画鑑賞", "旅行", "料理", "ランニング", "ヨガ", "キャンプ",
    "ゲーム", "音楽鑑賞", "写真撮影", "ガーデニング", "DIY", "釣り",
    "サッカー", "テニス", "スノーボード", "カフェ巡り", "ワイン", "アニメ",
]
SKILLS_POOL = [
    "Excel/データ分析", "プレゼンテーション", "プログラミング", "マーケティング",
    "デザイン", "英語", "中国語", "会計・簿記", "プロジェクト管理", "接客",
    "ライティング", "動画編集", "SNS運用", "統計分析", "交渉力",
]
GOALS_POOL = [
    "管理職への昇進", "独立・起業", "専門スキルの深化", "ワークライフバランスの改善",
    "年収アップ", "海外勤務", "転職", "副業の確立", "資格取得", "定年後の準備",
]

rows = []
for _ in range(N):
    sex = random.choice(SEXES)
    age = random.randint(18, 75)
    occupation = random.choice(OCCUPATIONS)
    region = random.choice(REGIONS)
    prefecture = random.choice(PREFECTURES[region])
    marital = random.choice(MARITAL)
    education = random.choice(EDUCATION)
    hobbies = "、".join(random.sample(HOBBIES_POOL, k=random.randint(2, 4)))
    skills = "、".join(random.sample(SKILLS_POOL, k=random.randint(1, 3)))
    goal = random.choice(GOALS_POOL)

    persona_text = (
        f"{prefecture}在住の{age}歳{sex}。職業は{occupation}。"
        f"{education}。{marital}。趣味は{hobbies}。"
    )

    rows.append({
        "uuid": str(uuid.uuid4()),
        "sex": sex,
        "age": age,
        "occupation": occupation,
        "country": "日本",
        "region": region,
        "prefecture": prefecture,
        "marital_status": marital,
        "education_level": education,
        "persona": persona_text,
        "cultural_background": f"{prefecture}で生まれ育ち、地域の文化に親しんでいる。",
        "skills_and_expertise": skills,
        "hobbies_and_interests": hobbies,
        "career_goals_and_ambitions": goal,
    })

out = "test_data/sample_personas_10000.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"Generated {len(rows)} rows -> {out}")
