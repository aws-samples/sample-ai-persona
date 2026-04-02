---
inclusion: manual
---

# CDK design

## 1. アーキテクチャ概要

ECS Express Mode上でFastAPIアプリケーションを実行し、DynamoDB・S3・Bedrock・AgentCore Memoryと連携するサーバーレスアーキテクチャ。CloudFront + Lambda@Edge（cognito-at-edge）による認証付き。

デプロイ順序:
```
ECR → AgentCoreMemory → Cognito → AIPersona (メイン) → callbackUrl更新
```

## 2. Stack一覧
* Stack IDのSuffixに`Stack`を付けないでください。

| Stack ID | 説明 | 依存関係 |
|----------|------|----------|
| AIPersonaMemory-{env} | AgentCore Memory（長期記憶）。独立デプロイ後、IDをparameters.tsに設定 | なし |
| AIPersonaEcr-{env} | ECRリポジトリ（コンテナイメージ格納） | なし |
| AIPersonaCognito-{env} | Cognito User Pool（認証）。先行デプロイ後、IDをparameters.tsに設定 | なし |
| AIPersona-{env} | メインスタック（DynamoDB, S3, ECS Express, CloudFront, VPC Origin, Lambda@Edge認証, WAF） | AIPersonaEcr-{env} |

## 3. Construct設計
* Construct IDのSuffixに`Construct`を付けないでください。
* ConstructのリソースIDは`Default`と`Resource`を適切に活用しConstruct IDを短縮してください。

| Construct ID | 説明 |
|-------------|------|
| Database | DynamoDBテーブル9個（Personas, Discussions, UploadedFiles, Datasets, PersonaDatasetBindings, SurveyTemplates, Surveys, KnowledgeBases, PersonaKBBindings）。GSI付き |
| Vpc | VPC（Public Subnet x2 AZ（NAT Gateway用）、Private Subnet x2 AZ（ECS用）、NAT Gateway x1） |
| UploadBucket | S3バケット（ファイルアップロード、バッチ推論入出力） |
| BedrockBatchRole | Bedrock Batch Inference用IAMロール（S3読み書き + モデル呼び出し） |
| ExpressService | ECS Express Mode Service（Private Subnet配置、Internal ALB自動管理） |
| CloudFrontDistribution | CloudFront + VPC Origin + Lambda@Edge認証 + WAF（prod） |
| AgentCoreMemory | AgentCore Memory（Summary + Semantic戦略）。CfnMemoryリソース |

### パラメータ設計
* パラメータは`cdk/parameters.ts`で管理:

| パラメータ名 | 説明 |
|-------------|------|
| env | AWSアカウント・リージョン |
| envName | 環境名（dev / prod） |
| dynamoDbTablePrefix | DynamoDBテーブル名プレフィックス |
| cognitoDomainPrefix | Cognito User Poolドメインプレフィックス（一意にする必要あり） |
| cognitoUserPoolId | Cognito User Pool ID（CognitoStackデプロイ後に設定） |
| cognitoUserPoolAppId | Cognito Client ID（CognitoStackデプロイ後に設定） |
| cognitoUserPoolDomain | Cognito Domain（CognitoStackデプロイ後に設定） |
| containerCpu | ECSコンテナCPU（dev: 1024, prod: 2048） |
| containerMemory | ECSコンテナメモリ（dev: 4096, prod: 8192） |
| enableWaf | CloudFront WAF有効化（dev: false, prod: true） |
| agentCoreMemoryId | AgentCore Memory ID（デプロイ後に設定） |
| summaryMemoryStrategyId | Summary Strategy ID（デプロイ後にCLIで取得して設定） |
| semanticMemoryStrategyId | Semantic Strategy ID（デプロイ後にCLIで取得して設定） |
| agentCoreMemoryEventExpiryDays | メモリイベント有効期限（日数、dev: 30, prod: 90） |
| bedrockModelId | メインモデルID（Claude Sonnet 4.5） |
| agentModelId | エージェントモデルID（Claude Haiku 4.5） |
| batchInferenceModelId | バッチ推論モデルID（Claude Haiku 4.5） |
| surveyS3Prefix | アンケート結果S3プレフィックス |
| batchInferenceS3Prefix | バッチ推論入出力S3プレフィックス |

## 4. 使用するライブラリ
- `aws-cdk-lib/aws-dynamodb` - DynamoDBテーブル
- `aws-cdk-lib/aws-s3` - S3バケット
- `aws-cdk-lib/aws-ecs` - ECS Express Mode（CfnExpressGatewayService）
- `aws-cdk-lib/aws-ec2` - VPC
- `aws-cdk-lib/aws-ecr` - ECRリポジトリ
- `aws-cdk-lib/aws-iam` - IAMロール
- `aws-cdk-lib/aws-cognito` - Cognito User Pool
- `aws-cdk-lib/aws-cloudfront` - CloudFront Distribution, VPC Origin, EdgeFunction
- `aws-cdk-lib/aws-cloudfront-origins` - VPC Origin Source
- `aws-cdk-lib/aws-lambda` - Lambda@Edge関数
- `aws-cdk-lib/aws-wafv2` - WAF WebACL
- `aws-cdk-lib/aws-bedrockagentcore` - AgentCore Memory（CfnMemory）

## 5. ディレクトリ構造

```
cdk/
├── bin/
│   └── app.ts              # エントリポイント（4スタック定義）
├── lib/
│   ├── ai-persona-stack.ts        # メインスタック
│   ├── ecr-stack.ts               # ECRスタック
│   ├── cognito-stack.ts           # Cognitoスタック
│   ├── agentcore-memory-stack.ts  # AgentCore Memoryスタック
│   └── constructs/
│       ├── database.ts            # DynamoDBテーブル群
│       ├── vpc.ts                 # VPC（Private + Public + NAT Gateway）
│       ├── upload-bucket.ts       # S3バケット
│       ├── bedrock-batch-role.ts  # Batch Inference IAMロール
│       ├── express-service.ts     # ECS Express Mode Service（Private Subnet）
│       ├── cloudfront.ts          # CloudFront + VPC Origin + Lambda@Edge + WAF
│       └── agentcore-memory.ts    # AgentCore Memory
├── lambda/
│   └── auth-at-edge/             # Lambda@Edge認証関数（cognito-at-edge）
│       ├── index.js              # ハンドラー（CDK synth時に動的生成）
│       ├── package.json
│       └── node_modules/         # cognito-at-edgeライブラリ
├── parameters.ts          # 環境別パラメータ（dev / prod）
├── cdk.json              # CDK設定
├── package.json
└── tsconfig.json
```

## 6. その他の注意事項
- CognitoStackは先行デプロイし、出力されたUser Pool ID/Client ID/Domainを`parameters.ts`に設定してからメインスタックをデプロイする
- メインスタックデプロイ後、CognitoのcallbackUrlをCloudFrontドメインに更新する必要がある（deploy.shでは自動実行）
- AgentCoreMemoryStackは独立して先にデプロイし、出力されたMemory IDとStrategy ID（AWS CLIで取得）を`parameters.ts`に手動設定する
- `cognitoDomainPrefix`はグローバルで一意にする必要があるため、末尾にアカウントIDやランダム文字列を付与することを推奨
- ECS Express ModeはPrivate Subnetに配置。Internal ALBが自動作成される
- CloudFront VPC OriginでInternal ALBに接続。Lambda@Edge（cognito-at-edge）で認証
- Lambda@Edgeは環境変数をサポートしないため、Cognito設定はCDK synth時にindex.jsに埋め込む
- Lambda@Edge削除時はレプリカの削除に数時間かかる場合がある
- 本番環境（prod）ではRemovalPolicy.RETAINを使用し、開発環境（dev）ではRemovalPolicy.DESTROYを使用
