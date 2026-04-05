# Design: データ＋プロンプトによるペルソナ作成の柔軟性強化

## API Design

### エンドポイント変更
| Method | Path | Description | 変更 |
|--------|------|-------------|------|
| POST | `/persona/upload` | 複数ファイルアップロード（htmx） | 既存を拡張 |
| POST | `/persona/generate` | 統一ペルソナ生成（htmx） | 既存を置き換え |

### 廃止エンドポイント
- `POST /persona/generate-multiple` → `POST /persona/generate` に統合

## Components

### 変更対象

#### Presentation Layer
- `web/templates/persona/generation.html` — 統一UIに全面書き換え
- `web/templates/persona/partials/upload_success.html` — 複数ファイル対応に変更
- `web/templates/persona/partials/persona_candidates.html` — 再利用（変更なし）
- `web/routers/persona.py` — 新規エンドポイント追加、旧エンドポイント廃止

#### Application Layer
- `src/managers/persona_manager.py` — `generate_personas()` メソッド追加（統一生成）

#### Service Layer
- `src/services/agent_service.py` — `create_persona_generation_agent()` メソッド追加（汎用ペルソナ生成エージェント）
  - データ種別が「購買データ」「レビューデータ」等のCSV系の場合、MotherDuck MCPツールを付与してデータ分析可能にする
  - `MCPServerManager` 経由でMotherDuck MCPクライアントを取得し、エージェントのadditional_toolsに追加

### 新規コンポーネント
- `web/templates/persona/partials/uploaded_files.html` — アップロード済みファイル一覧パーシャル

## Implementation Strategy

### データ種別とプロンプト構築

データ種別ごとにベースプロンプトを定義し、カスタムプロンプトと結合してエージェントに渡す:

```python
DATA_TYPE_PROMPTS = {
    "interview": "以下はN1インタビュー・顧客ヒアリングのデータです。発言内容から読み取れる価値観、課題、行動パターンを分析してペルソナを生成してください。",
    "market_report": "以下は市場調査・分析レポートです。市場セグメント、顧客行動パターン、デモグラフィック情報を分析してペルソナを生成してください。",
    "review": "以下は商品レビュー・口コミデータです。ユーザーの満足点、不満点、利用シーン、期待を分析してペルソナを生成してください。",
    "purchase": "以下は購買データ・トランザクションデータです。購買パターン、嗜好、ライフスタイルを分析してペルソナを生成してください。",
    "other": None  # ユーザー入力のデータ説明を使用
}
```

### エージェントのプロンプト構築フロー

```
データ種別プロンプト（or ユーザー入力のデータ説明）
  + カスタムプロンプト（任意）
  + アップロードデータ内容
  + 生成数指定
  → ペルソナ作成エージェント
  → agent.structured_output(PersonaList) でStructured Output取得
  → パースエラーなしでペルソナリスト取得
```

### Structured Output

Strands SDK の `agent.structured_output(output_model)` を使用:

```python
from pydantic import BaseModel, Field

class PersonaOutput(BaseModel):
    name: str = Field(description="日本人の名前（姓 名）")
    age: int = Field(description="年齢")
    occupation: str = Field(description="職業")
    background: str = Field(description="背景・経歴")
    values: list[str] = Field(description="価値観（3-5個）")
    pain_points: list[str] = Field(description="課題・悩み（3-5個）")
    goals: list[str] = Field(description="目標・願望（3-5個）")

class PersonaListOutput(BaseModel):
    personas: list[PersonaOutput] = Field(description="生成されたペルソナのリスト")

# 使用方法
result = agent(prompt)  # まず通常実行
persona_list = agent.structured_output(PersonaListOutput)  # 構造化出力取得
```

これにより、JSONパースエラーを完全に防止し、型安全にペルソナを取得できる。

### ファイル処理

- 複数ファイルを受け取り、各ファイルからテキストを抽出
- 既存の `FileManager.extract_text_from_file()` を再利用
- 全ファイルのテキストを結合してエージェントに渡す
- 対応形式: .txt, .md, .pdf, .docx, .doc, .csv

### UIフロー（htmx）

```
[ファイルアップロード] → hx-post="/persona/upload"
  → #uploaded-files にファイル一覧表示

[生成ボタン] → hx-post="/persona/generate"
  → フォームデータ: data_type, data_description, custom_prompt, persona_count, file_ids
  → #persona-result にペルソナ候補表示
  → データ種別がCSV系の場合、エージェントがMotherDuck MCPでデータ分析→ペルソナ生成

[保存] → 既存の保存フローを再利用
```

### Reusable
- `FileManager.extract_text_from_file()` — テキスト抽出
- `PersonaManager._validate_generated_persona()` — ペルソナ検証
- `MCPServerManager` — MotherDuck MCPクライアント管理
- `persona/partials/persona_candidates.html` — 候補表示UI
- `persona/partials/generated_persona.html` — 単一ペルソナ表示UI

### New
- `PersonaManager.generate_personas()` — 統一ペルソナ生成メソッド
- `AgentService.create_persona_generation_agent()` — 汎用ペルソナ生成エージェント（MotherDuck MCP連携対応）
- `web/templates/persona/generation.html` — 統一UI（全面書き換え）
- `web/templates/persona/partials/uploaded_files.html` — ファイル一覧パーシャル

---
**Created**: 2026-04-05
