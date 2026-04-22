# Design: D360 連携ペルソナ生成

## Architecture Overview

```
ペルソナ生成画面 (generation.html)
  │ data_type="dwh", analysis_angle="高単価リピーター層"
  ▼
persona.py (router)
  │ SSE ストリーミング
  ▼
persona_manager.py
  │ generate_personas_from_dwh()
  ├─ 1. AI で D360 への質問を生成
  ├─ 2. D360Service で分析結果を取得
  └─ 3. 分析結果をもとにペルソナ生成（既存 AI Service）
  │
  ▼
D360Service (新規)
  │ boto3 bedrock-agentcore invoke_agent_runtime
  ▼
AgentCore Runtime (D360 DWH Agent)
  │ Redshift SQL 実行
  ▼
分析結果テキスト
```

## Components

### 1. D360Service (`src/services/d360_service.py`) — 新規

D360 AgentCore Runtime への問い合わせを担当する薄いサービス層。

```python
class D360Service:
    def __init__(self, runtime_arn: str, region: str): ...
    def query(self, question: str) -> str: ...
```

- `query()`: 質問テキストを送り、SSE レスポンスから回答テキストを収集して返す
- セッション ID は毎回新規生成（独立した問い合わせ）
- `read_timeout=600` で SQL 実行の待ち時間に対応
- 同期メソッド（呼び出し元が ThreadPoolExecutor で実行）

### 2. PersonaManager 拡張 (`src/managers/persona_manager.py`)

既存クラスに D360 連携メソッドを追加。

```python
def generate_personas_from_dwh(
    self,
    analysis_angle: str,
    persona_count: int,
    custom_prompt: str | None = None,
) -> tuple[list[Persona], list[dict[str, str]]]:
```

内部フロー:
1. `ai_service` で `analysis_angle` から D360 への質問を生成
2. `d360_service.query()` で分析結果を取得
3. 分析結果テキストを入力として `ai_service` でペルソナ生成

### 3. Router 拡張 (`web/routers/persona.py`)

- `generate_persona` エンドポイントで `data_type="dwh"` を処理
- ファイルアップロードの代わりに `analysis_angle: str = Form("")` を受け取る
- 既存の SSE パターン（`executor.submit` → keepalive → result）を踏襲

### 4. Config 拡張 (`src/config.py`)

```python
# D360 連携設定
D360_RUNTIME_ARN: Optional[str] = None
D360_REGION: str = "ap-northeast-1"
ENABLE_D360_INTEGRATION: bool = False
```

環境変数: `D360_RUNTIME_ARN`, `D360_REGION`, `ENABLE_D360_INTEGRATION`

### 5. 設定画面 (`web/routers/settings.py`, テンプレート)

設定画面に「D360 連携」セクションを追加:
- Runtime ARN 入力
- リージョン入力
- 有効/無効トグル
- 接続テスト（簡単な質問を送って応答確認）

### 6. テンプレート (`web/templates/persona/generation.html`)

- data_type セレクタに「DWH（D360連携）」を追加
- `data_type="dwh"` 選択時: ファイルアップロードを非表示、「分析の切り口」テキストエリアを表示
- htmx で動的に切り替え

## Implementation Strategy

- Reusable: `PersonaManager`, `AIService`, `Config`, SSE パターン, テンプレート構造
- New: `D360Service`, `generate_personas_from_dwh`, D360 設定 UI, 質問自動生成プロンプト

## AI プロンプト設計

### D360 質問生成プロンプト

ユーザーの「分析の切り口」から D360 への質問を生成する。D360 は売上・注文・顧客・商品データを持つ DWH エージェントであることをコンテキストとして与える。

### ペルソナ生成プロンプト

既存の `generate_personas` のプロンプトを拡張し、D360 の分析結果（定量データ）を入力として受け取れるようにする。data_type として `"dwh"` を追加し、定量データからペルソナを構築する指示を含める。

---
**Created**: 2026-04-22
