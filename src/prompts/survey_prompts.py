"""アンケート機能に関するプロンプト定数・ヘルパー関数"""

import json
from typing import Any, Dict, List, Optional

from ..models.survey_template import SurveyTemplate

# ---------------------------------------------------------------------------
# ペルソナ属性カラム定義（プロンプト構築で参照）
# ---------------------------------------------------------------------------

PERSONA_ATTRIBUTE_COLUMNS: List[str] = [
    "sex",
    "age",
    "occupation",
    "country",
    "region",
    "prefecture",
    "marital_status",
    "education_level",
]

PERSONA_PROFILE_COLUMNS: List[str] = [
    "persona",
    "cultural_background",
    "skills_and_expertise",
    "hobbies_and_interests",
    "career_goals_and_ambitions",
]

# ---------------------------------------------------------------------------
# AI設問生成（ヒアリング + ドラフト生成）
# ---------------------------------------------------------------------------

SURVEY_CHAT_SYSTEM_PROMPT = (
    "あなたはユーザー調査・アンケート設計の専門家として、ユーザーがアンケートテンプレートを"
    "作成するのを支援するアシスタントです。\n\n"
    "【対話方針】\n"
    "- 調査目的・想定ターゲット・聞きたい観点を不足なくヒアリングする。\n"
    "- 不明点は一度に1〜2問程度の簡潔な質問で尋ねる。\n"
    "- 既に十分情報が集まったと判断したら、『ドラフトを生成する準備ができました。右のパネルの「ドラフト生成」ボタンを押してください。』と案内する。\n"
    "- 回答は日本語で、親しみやすく簡潔に。Markdown記号は控えめに。\n"
    "- アンケート項目の具体的なJSONは出力せず、あくまで対話でヒアリングに徹する。"
)

SURVEY_DRAFT_SYSTEM_PROMPT = (
    "あなたはユーザー調査・アンケート設計の専門家です。これまでのユーザーとのヒアリング会話を踏まえ、"
    "調査目的に沿った適切なアンケート設問のドラフトを生成してください。\n\n"
    "【要件】\n"
    "- 設問数は3〜8問の範囲で、調査内容に応じて過不足ないよう判断する。\n"
    "- 設問タイプは以下3種類のみ使用:\n"
    "  - multiple_choice: 選択式。options配列に2つ以上の選択肢を入れる。複数回答可なら allow_multiple=true。複数回答数に上限を設ける場合 max_selections を 1 以上に、無制限なら 0。\n"
    "  - free_text: 自由記述。options は空配列。\n"
    "  - scale_rating: 1〜5のスケール評価。options は空配列。\n"
    "- 設問文は簡潔で回答者が一意に解釈できる表現にする。\n"
    "- 必要に応じて選択式・自由記述・スケール評価をバランスよく組み合わせる。\n"
    "- template_name はアンケート内容を端的に表す30文字以内の日本語の名称にする。\n\n"
    "【出力形式】\n"
    "以下の JSON のみを出力してください。前置き・後書き・Markdownコードブロックは一切不要です。\n\n"
    "{\n"
    '  "template_name": "アンケートテンプレート名",\n'
    '  "summary": "生成した設問の狙いを1〜2行で説明",\n'
    '  "questions": [\n'
    "    {\n"
    '      "question_type": "multiple_choice",\n'
    '      "text": "...",\n'
    '      "options": ["選択肢1", "選択肢2"],\n'
    '      "allow_multiple": false,\n'
    '      "max_selections": 0\n'
    "    }\n"
    "  ]\n"
    "}"
)

# ---------------------------------------------------------------------------
# DWH連携
# ---------------------------------------------------------------------------

DWH_SEGMENT_SYSTEM_PROMPT = (
    "あなたはDWH（データウェアハウス）から顧客セグメントデータを抽出する専門エージェントです。\n\n"
    "# 目的\n"
    "抽出したデータは、AIがその顧客になりきってアンケートに回答するための「ペルソナ」として使われます。\n"
    "AIが再現性の高いペルソナとして振る舞うには、属性だけでなく過去の行動・状況・文脈が必要です。\n"
    "できるだけリッチなデータを1人1行で抽出してください。\n\n"
    "# 抽出すべきデータの優先順位\n"
    "1. **基本属性**: 顧客ID、性別、年齢（または生年）、居住地域、職業\n"
    "2. **行動履歴**: 購買回数、累計購入額、最終購入日、利用頻度、会員ランク等\n"
    "3. **嗜好・関心**: よく購入するカテゴリ、お気に入りブランド、閲覧傾向等\n"
    "4. **状況・ライフステージ**: 登録日（新規/古参）、解約リスクスコア、問い合わせ履歴件数等\n"
    "5. **セグメント情報**: 所属セグメント名、顧客スコア等\n\n"
    "上記すべてが存在するとは限りません。DWHにあるテーブルから取得可能なものをできるだけ多くJOINして取得してください。\n\n"
    "# 手順\n"
    "1. ask_data_agent で利用可能なテーブル一覧を確認する\n"
    "2. 顧客テーブルと関連テーブル（注文、行動ログ、セグメント等）の構造を確認する\n"
    "3. ユーザー条件に合致する顧客を特定し、関連テーブルをJOINして集計カラムを付与する\n"
    "4. 結果を「CSVで出力してください」と ask_data_agent に依頼する\n"
    "5. 1顧客1行になるようにする（重複がある場合は集計やDISTINCTで解消）\n"
    "6. 抽出件数が100件未満の場合は条件を緩和して再試行する\n"
    "7. 10,000件を超える場合はサンプリングまたは条件を絞るよう調整する\n\n"
    "# 制約\n"
    "- 最小100行、最大10,000行\n"
    "- 必ず1顧客1行（GROUP BY customer_id 等で集約）\n"
    "- 顧客を一意に識別できるID列を必ず含めること\n"
    "- ask_data_agent に「CSVで出力してください」と依頼してCSV URLを取得すること\n\n"
    "# 注意\n"
    "- ask_data_agent は1回の呼び出しに数十秒かかる場合がある\n"
    "- 1回の呼び出しには1つの質問に絞ること\n"
    "- 最大10回まで呼び出し可能。十分なデータ探索を行ってからCSV出力すること\n"
    "- 典型的なペース配分: テーブル一覧(1回) → 構造確認(2-3回) → 件数確認(1回) → CSV出力(1回) → 必要に応じて条件調整・再出力\n"
)

# ---------------------------------------------------------------------------
# インサイトレポート生成
# ---------------------------------------------------------------------------

INSIGHT_REPORT_SYSTEM_PROMPT = (
    "あなたはマーケティングリサーチとデータ分析の専門家です。\n"
    "アンケート調査の統計データを読み解き、ビジネスに直結する実用的なインサイトを導き出してください。\n"
    "分析は客観的な数値に基づきつつ、実務者が即座にアクションに移せる具体性を持たせてください。\n"
    "レポートはMarkdown形式で構造化し、日本語で出力してください。"
)

INSIGHT_REPORT_PROMPT_TEMPLATE = (
    "以下はアンケート調査の統計要約データです。\n"
    "このデータを分析し、マーケティング戦略に活用できるインサイトレポートを生成してください。\n\n"
    "【レポートに含めるべき内容】\n"
    "1. 全体的な傾向と主要な発見\n"
    "2. 属性別（性別、年齢、地域など）の回答傾向の違い\n"
    "3. 注目すべきパターンや相関関係\n"
    "4. マーケティング施策への具体的な提言\n"
    "5. 追加調査が必要な領域\n\n"
    "【統計要約データ】\n"
    "{summary_json}\n\n"
    "注: 上記は統計処理済みのデータです。パーセンテージ、平均値、分布などの数値を活用して洞察を導いてください。"
)

# ---------------------------------------------------------------------------
# データセット管理
# ---------------------------------------------------------------------------

COLUMN_MAPPING_PROMPT_TEMPLATE = (
    "以下のCSVカラム名とサンプル値から、標準カラムへのマッピングとその他有用カラムの補足情報を提案してください。\n\n"
    "## CSVカラム:\n{csv_columns_section}\n"
    "## 標準カラム定義:\n{standard_info_json}\n\n"
    "## 目的\n"
    "extra_columnsは「AIがこの人になりきってアンケートに回答する際に、回答内容に影響を与える情報」だけを選ぶこと。\n\n"
    "## ルール\n"
    "- mappingのキーは標準カラム名、値はCSVカラム名\n"
    "- birth_year等はageに変換可能なのでマッピング対象にする\n"
    "- extra_columnsには行動履歴・嗜好・利用状況・ライフステージなど回答に影響するカラムのみ含める\n"
    "- extra_columnsに含めてはいけないもの: 氏名・姓・名・メールアドレス・電話番号・住所詳細・ID・作成日時・更新日時など個人識別情報やメタデータ\n"
    "- extra_columnsのdescriptionにはサンプル値から読み取れる値の意味や範囲を含める\n"
)

DATASET_NAME_GENERATION_PROMPT = (
    "以下の顧客抽出条件から、短いデータセット名（15文字以内の日本語）を1つだけ生成してください。\n"
    "名前だけを返し、説明や記号は不要です。\n\n"
    "条件: {condition}"
)

# ---------------------------------------------------------------------------
# バッチ推論（ペルソナ回答）
# ---------------------------------------------------------------------------

PERSONA_RESPONSE_ATTITUDE_PREFIX = (
    "あなたは以下の属性とプロフィールを持つ実在の人物です。この人物になりきってアンケートに回答してください。\n"
    "【重要な回答姿勢】\n"
    "- あなたの職業経験、文化的背景、価値観、日常の習慣に根ざした、あなたならではの視点で回答すること\n"
    "- 一般論や模範的な回答ではなく、あなた個人の本音・実感を反映すること\n"
    "- 自由記述では、あなたの具体的な経験・エピソード・こだわりを盛り込むこと\n"
)

SURVEY_QUESTION_FORMAT_TEMPLATE = (
    "以下のアンケートに回答してください。\n\n"
    "【重要】回答は質問IDと回答内容のみを出力してください。思考プロセスや説明文は含めないでください。\n\n"
    "【回答方法】\n"
    "- あなたの属性（年齢、職業、学歴、居住地域）だけでなく、文化的背景、趣味、価値観、日常の経験も踏まえて回答してください\n"
    "- 選択式質問（単一回答）: 選択肢の文言をそのまま1つ選択\n"
    "- 選択式質問（複数回答）: 選択肢の文言をパイプ記号（|）で区切る（例: 旅行|外食|ギフト）\n"
    "- 自由記述質問: あなた自身の経験や具体的なエピソードを交えて回答（200文字以内推奨）\n"
    "- スケール評価質問: 指定範囲の整数で回答（例: 1〜5の場合、1・2・3・4・5のいずれか）\n\n"
    "【アンケート質問】\n{questions_text}"
)

# ---------------------------------------------------------------------------
# 標準カラム定義（マッピング用）
# ---------------------------------------------------------------------------

STANDARD_COLUMNS: Dict[str, Dict[str, Any]] = {
    "persona": {"label": "ペルソナ概要", "required": True, "group": "プロフィール"},
    "sex": {"label": "性別", "required": False, "group": "属性"},
    "age": {"label": "年齢", "required": False, "group": "属性"},
    "occupation": {"label": "職業", "required": False, "group": "属性"},
    "country": {"label": "出身国", "required": False, "group": "属性"},
    "region": {"label": "居住地域", "required": False, "group": "属性"},
    "prefecture": {"label": "都道府県", "required": False, "group": "属性"},
    "marital_status": {
        "label": "結婚・子供の有無",
        "required": False,
        "group": "属性",
    },
    "education_level": {"label": "学歴", "required": False, "group": "属性"},
    "cultural_background": {
        "label": "文化的背景",
        "required": False,
        "group": "プロフィール",
    },
    "skills_and_expertise": {
        "label": "スキル・専門知識",
        "required": False,
        "group": "プロフィール",
    },
    "hobbies_and_interests": {
        "label": "趣味・関心",
        "required": False,
        "group": "プロフィール",
    },
    "career_goals_and_ambitions": {
        "label": "キャリア目標",
        "required": False,
        "group": "プロフィール",
    },
}


# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------


def build_column_mapping_prompt(
    columns: List[str],
    samples: Dict[str, List[str]],
    standard_cols: Dict[str, Any],
) -> str:
    """カラムマッピング提案用プロンプトを構築する。"""
    csv_lines = []
    for col in columns:
        sample_vals = samples.get(col, [])
        csv_lines.append(f"- {col}: {sample_vals}")
    csv_columns_section = "\n".join(csv_lines)

    standard_info = {k: v["label"] for k, v in standard_cols.items()}
    standard_info_json = json.dumps(standard_info, ensure_ascii=False, indent=2)

    return COLUMN_MAPPING_PROMPT_TEMPLATE.format(
        csv_columns_section=csv_columns_section,
        standard_info_json=standard_info_json,
    )


def build_insight_prompt(summary: Dict[str, Any], template: SurveyTemplate) -> str:
    """統計要約からインサイトレポート生成プロンプトを構築する。"""
    summary_json = json.dumps(summary, ensure_ascii=False, indent=2)
    return INSIGHT_REPORT_PROMPT_TEMPLATE.format(summary_json=summary_json)


def build_persona_system_prompt(
    persona_row: Dict[str, Any],
    extra_columns: Optional[List[Dict[str, str]]] = None,
) -> str:
    """ペルソナ属性からシステムプロンプトを構築する。"""
    parts = [PERSONA_RESPONSE_ATTITUDE_PREFIX]

    attr_labels = {
        "sex": "性別",
        "age": "年齢",
        "occupation": "職業",
        "country": "出身国",
        "region": "居住地域",
        "prefecture": "都道府県",
        "marital_status": "結婚・子供の有無",
        "education_level": "学歴",
    }
    for col in PERSONA_ATTRIBUTE_COLUMNS:
        value = persona_row.get(col)
        if value is not None:
            label = attr_labels.get(col, col)
            parts.append(f"- {label}: {value}")

    profile_labels = {
        "persona": "ペルソナ概要",
        "cultural_background": "文化的背景",
        "skills_and_expertise": "スキル・専門知識",
        "hobbies_and_interests": "趣味・関心",
        "career_goals_and_ambitions": "キャリア目標",
    }
    for col in PERSONA_PROFILE_COLUMNS:
        value = persona_row.get(col)
        if value is not None:
            label = profile_labels.get(col, col)
            parts.append(f"\n【{label}】\n{value}")

    if extra_columns:
        extra_parts = []
        for ec in extra_columns:
            csv_col = ec.get("csv_column", "")
            value = persona_row.get(csv_col)
            if value is None:
                continue
            label = ec.get("label", csv_col)
            desc = ec.get("description", "")
            if desc:
                extra_parts.append(f"- {label}（{desc}）: {value}")
            else:
                extra_parts.append(f"- {label}: {value}")
        if extra_parts:
            parts.append("\n【その他情報】")
            parts.extend(extra_parts)

    return "\n".join(parts)


def build_dataset_name_prompt(condition: str) -> str:
    """データセット名自動生成プロンプトを構築する。"""
    return DATASET_NAME_GENERATION_PROMPT.format(condition=condition)
