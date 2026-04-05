# Design: データ＋プロンプトによるペルソナ作成の柔軟性強化

## API Design

### 新規エンドポイント
| Method | Path | Description |
|--------|------|-------------|
| POST | `/persona/upload-files` | 複数ファイルアップロード（htmx） |
| POST | `/persona/generate-flexible` | 統一ペルソナ生成（htmx） |

### 廃止エンドポイント
- `POST /persona/upload` → `POST /persona/upload-files` に統合
- `POST /persona/generate` → `POST /persona/generate-flexible` に統合
- `POST /persona/generate-multiple` → `POST /persona/generate-flexible` に統合

## Components

### 変更対象

#### Presentation Layer
- `web/templates/persona/generation.html` — 統一UIに全面書き換え
- `web/templates/persona/partials/upload_success.html` — 複数ファイル対応に変更
- `web/templates/persona/partials/persona_candidates.html` — 再利用（変更なし）
- `web/routers/persona.py` — 新規エンドポイント追加、旧エンドポイント廃止

#### Application Layer
- `src/managers/persona_manager.py` — `generate_personas_flexible()` メソッド追加

#### Service Layer
- `src/services/agent_service.py` — `generate_personas_flexible()` メソッド追加（汎用ペルソナ生成エージェント）

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
  + 出力フォーマット指示
  → ペルソナ作成エージェント
  → JSON配列でペルソナ出力
```

### ファイル処理

- 複数ファイルを受け取り、各ファイルからテキストを抽出
- 既存の `FileManager.extract_text_from_file()` を再利用
- 全ファイルのテキストを結合してエージェントに渡す
- 対応形式: .txt, .md, .pdf, .docx, .doc, .csv

### UIフロー（htmx）

```
[ファイルアップロード] → hx-post="/persona/upload-files"
  → #uploaded-files にファイル一覧表示

[生成ボタン] → hx-post="/persona/generate-flexible"
  → フォームデータ: data_type, data_description, custom_prompt, persona_count, file_ids
  → #persona-result にペルソナ候補表示

[保存] → 既存の保存フローを再利用
```

### Reusable
- `FileManager.extract_text_from_file()` — テキスト抽出
- `AgentService._parse_personas_from_response()` — レスポンスパース
- `PersonaManager._validate_generated_persona()` — ペルソナ検証
- `persona/partials/persona_candidates.html` — 候補表示UI
- `persona/partials/generated_persona.html` — 単一ペルソナ表示UI

### New
- `PersonaManager.generate_personas_flexible()` — 統一ペルソナ生成メソッド
- `AgentService.generate_personas_flexible()` — 汎用ペルソナ生成エージェント
- `web/templates/persona/generation.html` — 統一UI（全面書き換え）
- `web/templates/persona/partials/uploaded_files.html` — ファイル一覧パーシャル

---
**Created**: 2026-04-05
