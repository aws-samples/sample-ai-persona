# Design: D360 連携ペルソナ生成

## Architecture Overview

```
ペルソナ生成画面 (generation.html)
  │ data_type="dwh", analysis_angle="高単価リピーター層", persona_count=3
  ▼
persona.py (router) — SSE ストリーミング、ThreadPoolExecutor
  ▼
persona_manager.py — generate_personas() 既存フロー
  ▼
agent_service.py — generate_personas_with_agent()
  │
  ├─ create_persona_generation_agent(data_type="dwh")
  │    ├─ system_prompt: DWH データ分析用
  │    └─ tool: ask_data_agent ← 新規追加
  │
  └─ agent(prompt)  ← analysis_angle を含むプロンプト
       │
       │ Agent が自律的に D360 に問い合わせ
       ▼
     ask_data_agent(question)  ← Strands @tool
       │
       ▼
     D360Service.query(question)  ← boto3 AgentCore Runtime invoke
       │
       ▼
     AgentCore Runtime (D360 DWH Agent) → Redshift SQL → 分析結果テキスト
```

既存の `generate_personas_with_agent` フローにそのまま乗る。
MCP ツールの代わりに `ask_data_agent` ツールを Agent に渡す形。

## Components

### 1. D360Service (`src/services/d360_service.py`) — 新規

AgentCore Runtime への問い合わせを担当する薄いラッパー。

```python
class D360Service:
    def __init__(self, runtime_arn: str, region: str): ...
    def query(self, question: str) -> str: ...
```

- `query()`: 質問テキストを送り、SSE レスポンスから回答テキストを収集して返す
- セッション ID は毎回新規生成（独立した問い合わせ）
- `read_timeout=600`, `connect_timeout=120`
- 同期メソッド（Agent 内のツール呼び出しはスレッドプール上で実行される）

### 2. ask_data_agent ツール (`src/services/d360_service.py` 内)

D360Service を Strands `@tool` でラップしたファクトリ関数。

```python
def create_d360_tool(runtime_arn: str, region: str):
    service = D360Service(runtime_arn, region)

    @tool
    def ask_data_agent(question: str) -> str:
        """DWH に自然言語で問い合わせる。売上・注文・顧客・商品データを分析できる。
        Args:
            question: データに関する質問
        """
        return service.query(question)

    return ask_data_agent
```

### 3. AgentService 拡張 (`src/services/agent_service.py`)

`create_persona_generation_agent` に `data_type="dwh"` 分岐を追加。

変更点:
- `DATA_TYPE_PROMPTS` に `"dwh"` を追加
- `data_type == "dwh"` の場合、`create_d360_tool()` で生成したツールを `tools` に追加
- system_prompt に DWH 分析の指示を追加（Agent が自律的に何を聞くか判断する）

### 4. PersonaManager 拡張 (`src/managers/persona_manager.py`)

`generate_personas` 内の `data_type="dwh"` ハンドリング。

変更点:
- ファイルアップロードなしでも `"dwh"` なら処理を続行
- `analysis_angle` を `data_text` として渡す（Agent へのプロンプトに含まれる）

### 5. Router 拡張 (`web/routers/persona.py`)

- `data_type="dwh"` 時はファイル必須チェックをスキップ
- `analysis_angle: str = Form("")` を受け取り、`data_text` として渡す

### 6. Config 拡張 (`src/config.py`)

```python
D360_RUNTIME_ARN: Optional[str] = None
D360_REGION: str = "ap-northeast-1"
ENABLE_D360_INTEGRATION: bool = False
```

### 7. 設定画面・テンプレート

- `settings.py`: D360 接続設定セクション追加
- `generation.html`: data_type に「DWH（D360連携）」追加、切り口入力 UI

## DWH 用 system_prompt

```
あなたはデータからリアルで具体的なペルソナを生成する専門家です。

# 役割
DWH（データウェアハウス）に蓄積された実際の売上・注文・顧客・商品データを
分析してペルソナを生成します。ask_data_agent ツールを使って DWH に問い合わせ、
定量データに基づいたペルソナを作成してください。

# 分析手順
1. まず ask_data_agent で全体像を把握する質問をする
   （例: 顧客セグメントの分布、売上上位カテゴリなど）
2. ユーザーの分析の切り口に沿って深掘りの質問をする
3. 得られた定量データをもとにペルソナを生成する
4. 各ペルソナにデータ根拠を明示する

# 注意
- ask_data_agent は 1 回の呼び出しに数十秒かかる場合がある
- 必要最小限の回数（2-3回程度）で効率的に情報を集める
- 200件を超えるデータは取得できないため、集計クエリを依頼する
```

---
**Created**: 2026-04-22
**Updated**: 2026-04-22 — boto3 直接方式から Strands Agent ツール方式に変更
