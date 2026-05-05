# Remote MCP Server 連携ガイド

AI ペルソナシステムの主要機能（ペルソナ生成、議論シミュレーション、インサイト生成）を MCP ツールとして外部 AI エージェントから利用できるようにする Remote MCP Server オプションの設定ガイドです。

## 概要

AgentCore Gateway を介して MCP プロトコルのエンドポイントを公開し、Amazon Quick や他の AI エージェントから AI ペルソナシステムの機能をツールとして呼び出せるようにします。

### アーキテクチャ

```
外部 AI エージェント（Amazon Quick 等）
    │
    ▼
AgentCore Gateway（MCP エンドポイント + Cognito M2M 認証）
    │  OpenAPI Target（REST ↔ MCP 自動変換）
    ▼
既存 ECS Express Mode（FastAPI REST API）
    │
    ▼
既存 Manager / Service 層
```

- **AgentCore Gateway** が MCP プロトコルのエンドポイントを提供し、Cognito M2M（Client Credentials）認証を自動管理します
- **OpenAPI Target** により、REST API と MCP ツールの変換が自動で行われます
- 既存の ECS 上の FastAPI アプリケーションに MCP 用 REST エンドポイントを追加するだけで、Manager/Service 層はそのまま利用します

### 利用可能な MCP ツール

| ツール | エンドポイント | 処理方式 | 説明 |
|--------|---------------|---------|------|
| ペルソナ一覧取得 | `GET /api/personas` | 同期 | 保存済みペルソナの一覧を取得 |
| ペルソナ詳細取得 | `GET /api/personas/{id}` | 同期 | 指定ペルソナの詳細情報を取得 |
| ペルソナ生成 | `POST /api/mcp/personas/generate` | 非同期 | テキストデータから AI ペルソナを生成 |
| 議論実行 | `POST /api/mcp/discussions` | 非同期 | ペルソナ間の議論を実行 |
| 議論結果取得 | `GET /api/mcp/discussions/{id}` | 同期 | 議論結果（メッセージ・インサイト）を取得 |
| インサイト生成 | `POST /api/mcp/discussions/{id}/insights` | 同期 | 議論結果からインサイトを生成 |
| インタビュー実行 | `POST /api/mcp/interviews` | 同期 | ペルソナに質問して回答を取得 |
| ジョブステータス確認 | `GET /api/mcp/jobs/{job_id}` | 同期 | 非同期ジョブの進捗・結果を確認 |

ペルソナ生成と議論実行は処理に時間がかかるため、非同期ジョブとして実行されます。ジョブ投入後に返される `job_id` を使って `GET /api/mcp/jobs/{job_id}` でステータスと結果をポーリングしてください。

## 前提条件

- AI ペルソナシステムのメインスタック（`AIPersona-{env}`）がデプロイ済みであること
- OpenAPI spec ファイル（`openapi_mcp.json`）が生成済みであること

## セットアップ手順

### 1. OpenAPI spec の確認

MCP ツール用の OpenAPI spec（`openapi_mcp.json`）はリポジトリに含まれています。MCP エンドポイントを追加・変更した場合のみ再生成してください。

```bash
uv run python scripts/generate_mcp_openapi.py
```

### 2. パラメータの設定

#### deploy.sh を使う場合（推奨）

`--enable-mcp` オプションを付けてデプロイするだけで、パラメータ設定と MCP Gateway のデプロイが自動で行われます。

```bash
./deploy.sh --enable-mcp
```

再デプロイ時（コード更新のみ）:

```bash
./deploy.sh --skip-memory --skip-cognito --enable-mcp
```

#### CDK を直接使う場合

`cdk/parameters.ts` で `enableMcpGateway` を `true` に設定します。

```typescript
// 開発環境の例
export const devParameter: AppParameter = {
  // ... 既存設定 ...

  // MCP Gateway設定（AgentCore Gateway）
  enableMcpGateway: true,
};
```

### 3. CDK デプロイ

> `deploy.sh --enable-mcp` を使った場合はこのステップは不要です。

メインスタックが未デプロイまたは `exportName` の追加が必要な場合は、先にメインスタックをデプロイします。

```bash
cd cdk
npx cdk deploy AIPersona-dev
```

次に MCP Gateway スタックをデプロイします。

```bash
npx cdk deploy AIPersonaMcp-dev
```

### 4. デプロイ出力の確認

デプロイ完了後、以下の出力を確認します。

| 出力キー | 説明 |
|---------|------|
| `GatewayId` | AgentCore Gateway の ID |
| `GatewayArn` | AgentCore Gateway の ARN |
| `TokenEndpointUrl` | Cognito トークンエンドポイント URL（M2M 認証用） |

Cognito User Pool の Client ID と Client Secret は AWS コンソールの Cognito 画面から確認してください。

## 認証と接続

### アクセストークンの取得

AgentCore Gateway はデフォルトで Cognito M2M（Client Credentials）認証を使用します。API を呼び出す前にアクセストークンを取得してください。

```bash
# トークン取得
TOKEN=$(curl -s -X POST "${TOKEN_ENDPOINT_URL}" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "scope=${OAUTH_SCOPES}" | jq -r '.access_token')
```

### MCP Inspector での動作確認

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) を使って Gateway の MCP エンドポイントに接続し、ツール一覧の取得やツール呼び出しをテストできます。

Gateway の MCP エンドポイント URL は以下の形式です:

```
https://{GatewayId}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp
```

`GatewayId` は CDK デプロイ時の出力から取得できます。

## Amazon Quick との連携

Amazon Quick の MCP 統合機能を使って、AgentCore Gateway の MCP エンドポイントを外部ツールとして登録できます。これにより、Amazon Quick のアシスタントが AI ペルソナシステムの機能をツールとして利用できるようになります。

### 前提条件

- Amazon Quick Enterprise サブスクリプションが有効であること
- AI ペルソナシステムの MCP Gateway（`AIPersonaMcp-{env}`）がデプロイ済みであること

### 接続情報の準備

MCP Gateway のデプロイ出力から以下の情報を控えておきます。

| 情報 | 取得元 |
|------|--------|
| MCP エンドポイント URL | `https://{GatewayId}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp` |
| Token URL | CDK 出力の `TokenEndpointUrl` |
| Client ID | AWS コンソール → Cognito → User Pool → App Client |
| Client Secret | AWS コンソール → Cognito → User Pool → App Client → Show client secret |

### 設定手順

1. **Amazon Quick コンソール**を開き、**Connectors** を選択
2. **Create for your team** タブを選択
3. **Model Context Protocol (MCP)** を選択
4. 統合の詳細を入力:
   - **Name**: `AI Persona System`（任意の名前）
   - **Description**: `AIペルソナシステム - ペルソナ生成・議論・インサイト生成`
   - **MCP server endpoint**: `https://{GatewayId}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp`
5. **Next** を選択
6. 認証方式で **Service authentication (Service-to-Service)** を選択
7. 認証情報を入力:
   - **Client ID**: Cognito App Client の Client ID
   - **Client Secret**: Cognito App Client の Client Secret
   - **Token URL**: CDK 出力の `TokenEndpointUrl`
8. **Create and continue** を選択
9. ツール一覧が自動検出されるので、利用するツールを確認して有効化
10. **Next** を選択し、必要に応じて他のユーザーと共有

### 利用例

設定完了後、Amazon Quick のチャットで以下のような指示が可能になります:

- 「保存されているペルソナの一覧を見せて」
- 「ペルソナ ID xxx と yyy で『新商品の価格設定』について議論して」
- 「このインタビューテキストからペルソナを3人生成して」
- 「議論 ID xxx のインサイトを生成して」

### 60 秒タイムアウトへの対応

Amazon Quick の MCP 統合には **60 秒のタイムアウト制限**があります。AI ペルソナシステムでは、処理時間が長いペルソナ生成と議論実行を非同期ジョブとして実装しているため、以下のフローで利用します:

1. ペルソナ生成 / 議論実行のツールを呼び出す → `job_id` が即座に返る（60 秒以内）
2. ジョブステータス確認ツールで `job_id` の進捗を確認 → `completed` になるまで繰り返す
3. 結果が `result` フィールドに含まれる

AI エージェントがこのポーリングパターンを自動的に実行するため、ユーザーは非同期処理を意識する必要はありません。

### 注意事項

- ツール一覧は初回登録時に固定されます。MCP エンドポイントの追加・変更後は、統合を削除して再作成してください
- Amazon Quick は VPC 内の MCP サーバーへの直接接続をサポートしていません。AgentCore Gateway のパブリックエンドポイントを経由して接続します
- ステップアップ認証（追加スコープの要求）はサポートされていません

## MCP ツールの使い方

### ペルソナ生成（非同期）

```bash
# 1. ジョブ投入
JOB=$(curl -s -X POST "${GATEWAY_URL}/api/mcp/personas/generate" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "data_type": "interview",
    "file_contents": ["インタビュー内容のテキスト..."],
    "count": 3
  }')

JOB_ID=$(echo $JOB | jq -r '.job_id')

# 2. ステータス確認（completed になるまでポーリング）
curl -s "${GATEWAY_URL}/api/mcp/jobs/${JOB_ID}" \
  -H "Authorization: Bearer ${TOKEN}"
```

`data_type` には以下を指定できます:
- `interview` — N1 インタビューテキスト
- `market_report` — 市場調査レポート
- `review` — レビューデータ
- `purchase` — 購買データ
- `other` — その他（`description` で説明を追加）

### 議論実行（非同期）

```bash
# 1. ジョブ投入
JOB=$(curl -s -X POST "${GATEWAY_URL}/api/mcp/discussions" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "persona_ids": ["persona-id-1", "persona-id-2"],
    "topic": "新商品のターゲット層について",
    "mode": "classic"
  }')

JOB_ID=$(echo $JOB | jq -r '.job_id')

# 2. ステータス確認
curl -s "${GATEWAY_URL}/api/mcp/jobs/${JOB_ID}" \
  -H "Authorization: Bearer ${TOKEN}"
```

`mode` は `classic`（高速、3-5分）または `agent`（深い議論、5-15分）を指定できます。

### インタビュー（同期）

```bash
curl -s -X POST "${GATEWAY_URL}/api/mcp/interviews" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "persona_ids": ["persona-id-1"],
    "question": "普段どのような基準で商品を選びますか？"
  }'
```

### インサイト生成（同期）

```bash
curl -s -X POST "${GATEWAY_URL}/api/mcp/discussions/${DISCUSSION_ID}/insights" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "categories": [
      {"name": "顧客ニーズ", "description": "潜在的・顕在的ニーズ"},
      {"name": "市場機会", "description": "新たな市場セグメントや成長領域"}
    ]
  }'
```

`categories` を省略するとデフォルトカテゴリ（顧客ニーズ、市場機会、商品開発、マーケティング、その他）が使用されます。

## MCP Gateway の削除

MCP Gateway が不要になった場合、メインスタックに影響なく削除できます。

```bash
cd cdk
npx cdk destroy AIPersonaMcp-dev
```

削除後、次回の `deploy.sh` 実行時に `--enable-mcp` を付けなければ再作成されません。CDK を直接使う場合は `parameters.ts` の `enableMcpGateway` を `false` に戻してください。

## トラブルシューティング

| 問題 | 対処 |
|------|------|
| CDK デプロイで `Export not found` エラー | メインスタック（`AIPersona-{env}`）を先にデプロイしてください |
| `openapi_mcp.json` が見つからない | `uv run python scripts/generate_mcp_openapi.py` を実行して再生成してください |
| 認証エラー（401） | Cognito の Client ID / Secret / Scope が正しいか確認してください |
| ジョブが `failed` になる | `error` フィールドのメッセージを確認。Bedrock のモデルアクセス権限やペルソナ ID の存在を確認してください |
| 非同期ジョブの結果が消える | ジョブはインメモリ管理のため、ECS タスクの再起動で失われます。完了した結果は DB に保存されるため、議論やペルソナは通常の API で取得できます |

## 参考リンク

- [AgentCore Gateway Core Concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-core-concepts.html)
- [Set up AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-building.html)
- [Call a tool in a AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-call.html)
- [Amazon Quick MCP Integration](https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html)
- [Integrate external tools with Amazon Quick using MCP](https://aws.amazon.com/blogs/machine-learning/connect-amazon-quick-suite-to-enterprise-apps-and-agents-with-mcp/)
