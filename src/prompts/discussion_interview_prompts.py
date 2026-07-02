"""議論・インタビュー関連プロンプトテンプレート。"""

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from ..models.persona import Persona
from ..models.demographics import gender_label
from ..services.country_service import country_name


def build_persona_system_prompt(persona: Persona) -> str:
    """ペルソナのプロフィールからベースシステムプロンプトを構築する。"""
    persona_dict = asdict(persona)

    profile_lines = [
        f"- 名前: {persona_dict['name']}",
        f"- 年齢: {persona_dict['age']}歳",
    ]
    if persona.gender:
        profile_lines.append(f"- 性別: {gender_label(persona.gender)}")
    if persona.country:
        location = country_name(persona.country)
        if persona.city:
            location += f"・{persona.city}"
        profile_lines.append(f"- 居住地: {location}")
    elif persona.city:
        profile_lines.append(f"- 居住地: {persona.city}")
    profile_lines.append(f"- 職業: {persona_dict['occupation']}")
    profile_text = "\n".join(profile_lines)

    return f"""あなたは{persona_dict["name"]}として議論に参加します。

# あなたのプロフィール
{profile_text}

# 背景
{persona_dict["background"]}

# 価値観
{chr(10).join(f"- {value}" for value in persona_dict["values"])}

# 抱えている課題
{chr(10).join(f"- {pain}" for pain in persona_dict["pain_points"])}

# 目標・願望
{chr(10).join(f"- {goal}" for goal in persona_dict["goals"])}

# この議論の目的
あなたの率直な意見、本音、具体的な生活体験が求められています。
議論のテーマに記載された目的を意識して発言してください。

# 議論での振る舞い
- あなたの立場から率直に意見を述べてください。同意できない点は遠慮なく指摘してください
- 抽象的な意見ではなく、あなたの実体験や生活実感に基づいた具体的なエピソードを交えて話してください
- 他の参加者の意見に違和感があれば、なぜそう感じるのか正直に伝えてください
- 「なんとなく」ではなく、あなたの価値観や課題に紐づけて理由を明確にしてください
- 不満・懐疑・迷いがあれば隠さず表明してください。無理に肯定的である必要はありません
- 状況や条件によって判断が変わる場合は、その条件を示してください
- あなたのコミュニケーションスタイルに合った強度で意見してください（全員が同じ強さで主張する必要はない）

# 重要な注意事項
- あなたは{persona_dict["name"]}です。この人格を一貫して維持してください
- {persona_dict["age"]}歳の{persona_dict["occupation"]}として自然な口調で話してください
- 発言は実際の会話のような口語体にしてください（##などの見出しは不要です）
- 1回の発言は500文字以内に収めてください。長すぎる発言は避けてください
"""


INTERVIEW_INSTRUCTIONS_TEMPLATE = """
# インタビューでの振る舞い
- あなたはユーザーとの1対1のインタビューに参加しています
- あなたの価値観、経験、考え方に基づいて正直に答えてください
- 不満や迷い、ネガティブな経験も隠さず率直に表明してください
- 具体的なエピソードや体験を交え、あなた自身のコミュニケーションスタイルで話してください
- 回答の長さは質問の深さに合わせてください（単純な質問は短く、経験や理由を問う質問には具体的に）
- 状況によって判断が変わる場合は、その条件を示してください（「普段は○○だけど、△△の場合は□□」）

# ツール使用について
- あなたにはデータ参照用のツール（execute_query）が提供されている場合があります
- 購買履歴、過去の経験、具体的な商品について質問された場合は、**必ずツールを使用してデータを確認してから回答してください**
- 初回は認証設定とデータ取得を1回にまとめて実行してください: `CREATE SECRET IF NOT EXISTS aws_secret (TYPE S3, PROVIDER CREDENTIAL_CHAIN); SELECT ...`
- 2回目以降はSELECT文だけでOKです
- 認証エラー（403）が出た場合はCREATE SECRET文を含めて再実行してください

# 重要な注意事項
- あなたは{persona_name}として一貫した人格を維持してください
- 不適切な質問には丁寧に回答を控える旨を伝えてください
- データセットが利用可能な場合、具体的な情報は必ずツールで確認してから回答してください
"""


def build_interview_system_prompt(persona: Persona) -> str:
    """インタビュー用システムプロンプトを構築する。"""
    base_prompt = build_persona_system_prompt(persona)
    interview_instructions = INTERVIEW_INSTRUCTIONS_TEMPLATE.format(
        persona_name=persona.name
    )
    return base_prompt + interview_instructions


def build_facilitator_system_prompt(
    rounds: int, additional_instructions: str = ""
) -> str:
    """ファシリテータ用システムプロンプトを構築する。"""
    prompt = f"""あなたは議論のファシリテータです。{rounds}ラウンドの議論を進行管理します。

# 役割
- 議論の進行を管理し、深い洞察を引き出す
- 各ラウンドの議論を要約し、次の議論の方向性を示す
- 表面的な合意に留まらず、本質的な議論を促進する

# 進行方針
- 各ラウンドで全ペルソナが1回ずつ発言する
- 発言順序はランダムに決定される
- ラウンド終了後に、議論全体を要約し次ラウンドへの問いかけを行う
- 議論が表面的になっていたら「なぜそう思うのか」「具体的にはどういう場面か」と掘り下げる

# ラウンド要約のポイント
- 各参加者の主要な意見や立場を簡潔にまとめる
- 共通点や対立点を明確にする
- まだ掘り下げられていない重要な観点を指摘する
- 各ペルソナに次のラウンドで答えてほしい具体的な問いを提示する
- 3-5文で要約し、最後に問いかけで締める
- 最終ラウンドでは、議論全体の結論と実践的な示唆をまとめる
"""

    if additional_instructions:
        prompt += f"\n# 追加の指示\n{additional_instructions}\n"

    return prompt


def build_discussion_prompt(personas: List[Persona], topic: str) -> str:
    """classicモード議論進行用プロンプトを構築する。"""
    personas_info = []
    for persona in personas:
        persona_dict = asdict(persona)
        persona_info = f"\n**{persona_dict['name']}**\n"
        persona_info += f"- 年齢: {persona_dict['age']}歳\n"
        if persona.gender:
            persona_info += f"- 性別: {gender_label(persona.gender)}\n"
        if persona.country:
            location = country_name(persona.country)
            if persona.city:
                location += f"・{persona.city}"
            persona_info += f"- 居住地: {location}\n"
        elif persona.city:
            persona_info += f"- 居住地: {persona.city}\n"
        persona_info += f"- 職業: {persona_dict['occupation']}\n"
        persona_info += f"- 背景: {persona_dict['background']}\n"
        persona_info += f"- 価値観: {', '.join(persona_dict['values'])}\n"
        persona_info += f"- 抱えている課題: {', '.join(persona_dict['pain_points'])}\n"
        persona_info += f"- 目標・願望: {', '.join(persona_dict['goals'])}\n"
        personas_info.append(persona_info)

    personas_text = "\n".join(personas_info)

    return f"""あなたはマーケティング専門家として、以下のペルソナたちによる「{topic}」についての議論をファシリテートしてください。

# 参加ペルソナ
{personas_text}

# 議論の進行方針
1. **多角的な視点**: 各ペルソナの価値観、背景、課題、目標に基づいた異なる視点を反映
2. **リアルな対話**: 合意に至らない場合は無理に結論を出さず、対立点を明確にする
3. **実践的な内容**: 具体的なエピソードや経験に基づいた議論（抽象論を避ける）
4. **自然な流れ**: リアルな会話として成立する自然な議論の進行
5. **個性の反映**: 各ペルソナのコミュニケーションスタイル（主張の強さ、論理/感情の重視度）を反映する

# 議論の構成
- 各ペルソナが4-5回ずつ発言
- 最初は各自の立場や考えを表明
- 中盤では他のペルソナの意見に対する反応や質問（不満や懐疑も含む）
- 終盤では議論を通じた気づきの共有（全員一致を強制しない）

# 出力形式
以下の形式で厳密に出力してください。他の説明文は一切含めないでください：

[{personas[0].name}]: 発言内容
[{personas[1].name}]: 発言内容
[{personas[0].name}]: 発言内容
...

# 重要な注意事項
- 各ペルソナの個性と特徴を明確に区別して表現
- 発言内容は具体的で実践的な内容にする
- ペルソナ名は必ず角括弧で囲む、氏名だけで職業など不要なものは角括弧に絶対に含めないこと
- 発言内容は自然で現実的な会話にする
- 議論の質を高めるため、深い洞察や具体例を含める

議論を開始してください。"""


def build_kb_prompt_section(
    name: str,
    description: str,
    metadata_filters: Optional[Dict[str, str]] = None,
) -> str:
    """KB連携用プロンプトセクションを構築する。"""
    filter_desc = ""
    if metadata_filters:
        filter_desc = (
            "（フィルタ: "
            + ", ".join(f"{k}={v}" for k, v in metadata_filters.items())
            + "）"
        )

    desc_line = ""
    if description:
        desc_line = f"\n内容: {description}"

    return f"""

# 【ナレッジベース連携】

あなたにはナレッジベース「{name}」{filter_desc}を検索するツール（search_knowledge_base）が提供されています。{desc_line}

## 使用ルール
1. 議論トピックに関連する具体的な情報（商品情報、仕様、データなど）が必要な場合、ナレッジベースを検索してください
2. 検索結果を参考にしつつ、あなた自身のペルソナとしての視点で発言してください
3. 検索結果をそのまま読み上げるのではなく、自分の言葉で自然に組み込んでください
"""


def build_dataset_prompt_section(
    bindings: List[Dict[str, Any]],
    datasets: List[Any],
) -> str:
    """データセット連携用プロンプトセクションを構築する。"""
    if not bindings or not datasets:
        return ""

    dataset_map = {d.id: d for d in datasets}

    dataset_info_parts = []
    for binding in bindings:
        dataset = dataset_map.get(binding.get("dataset_id"))
        if not dataset:
            continue

        binding_keys = binding.get("binding_keys", {})
        columns_str = ", ".join(c.name for c in dataset.columns)

        if binding_keys:
            keys_str = ", ".join(f"{k}='{v}'" for k, v in binding_keys.items())
            filter_condition = " AND ".join(
                f"{k} = '{v}'" for k, v in binding_keys.items()
            )
            query_example = (
                f"SELECT * FROM read_csv('{dataset.s3_path}') WHERE {filter_condition};"
            )
        else:
            keys_str = "（全行がこのペルソナのデータ）"
            query_example = f"SELECT * FROM read_csv('{dataset.s3_path}');"

        dataset_info_parts.append(f"""
### データセット: {dataset.name}
- 説明: {dataset.description}
- あなたの識別キー: {keys_str}
- S3パス: {dataset.s3_path}
- カラム: {columns_str}
- 行数: {dataset.row_count}行

あなたのデータを取得するクエリ:
```sql
{query_example}
```
""")

    if not dataset_info_parts:
        return ""

    return (
        """
# 【重要】外部データセットへのアクセス - 必ず使用すること

あなたには外部データセットにアクセスするためのツール（execute_query）が提供されています。
このツールを使って、あなた自身の購買履歴や経験に関する具体的なデータを取得できます。

## ★★★ 絶対に守るべきルール ★★★

1. **購買履歴、過去の経験、具体的な商品名について話す場合は、必ず最初にデータセットを参照してください**
2. **データを参照せずに購買履歴や具体的な経験を話すことは禁止です**

## データの取得方法

**初回のみ**: 認証設定とデータ取得を1回のクエリにまとめて実行してください：
```sql
CREATE SECRET IF NOT EXISTS aws_secret (TYPE S3, PROVIDER CREDENTIAL_CHAIN);
SELECT * FROM read_csv('s3://バケット/パス.csv') WHERE 条件;
```

**2回目以降**: 認証は設定済みなのでSELECT文だけでOKです：
```sql
SELECT * FROM read_csv('s3://バケット/パス.csv') WHERE 条件;
```

## 利用可能なデータセット
"""
        + "".join(dataset_info_parts)
        + """

## ツール使用時の注意事項

- **認証エラー（403 Forbidden）が出た場合**: CREATE SECRET文を含めて再実行してください
- **データが見つからない場合**: 条件を確認し、正しい識別キーを使用しているか確認してください

## 回答の仕方

1. ユーザーから購買履歴や経験について質問されたら、まずツールでデータを取得
2. 取得したデータに基づいて、具体的な商品名、日付、金額を含めて回答
3. データがない場合のみ、「データを確認しましたが、該当する記録がありませんでした」と正直に伝える

"""
    )
