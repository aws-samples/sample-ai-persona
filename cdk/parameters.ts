import { Environment } from 'aws-cdk-lib';

export interface AppParameter {
  env?: Environment;
  envName: string;
  
  // DynamoDB設定
  dynamoDbTablePrefix: string;
  
  // Cognito設定
  cognitoDomainPrefix: string;
  // Cognito認証設定（CognitoStackデプロイ後に設定）
  cognitoUserPoolId: string;
  cognitoUserPoolAppId: string;
  cognitoUserPoolDomain: string;
  
  // ECS Express Mode設定
  containerCpu: string;
  containerMemory: string;
  
  // CloudFront + WAF設定
  enableWaf?: boolean;
  allowedIpAddresses?: string[]; // CIDR形式 例: ['203.0.113.0/24', '198.51.100.1/32']
  
  // AgentCore Memory設定
  // AgentCoreMemoryStackをデプロイ後、出力されたIDをここに設定してください
  // Memory IDの取得: CloudFormation OutputまたはAWS CLIで確認
  // Strategy IDの取得: 
  //   Summary: aws bedrock-agentcore-control get-memory --memory-id <MEMORY_ID> --query 'memory.strategies[?type==`SUMMARIZATION`].strategyId' --output text
  //   Semantic: aws bedrock-agentcore-control get-memory --memory-id <MEMORY_ID> --query 'memory.strategies[?type==`SEMANTIC`].strategyId' --output text
  agentCoreMemoryId: string;
  summaryMemoryStrategyId: string;
  semanticMemoryStrategyId: string;
  
  // AgentCore Memory作成設定（AgentCoreMemoryStackで使用）
  agentCoreMemoryEventExpiryDays?: number;
  
  // Bedrock設定
  bedrockModelId: string;
  agentModelId: string;
  
  // マスアンケート機能設定
  batchInferenceModelId: string;
  surveyS3Prefix?: string;
  batchInferenceS3Prefix?: string;
}

// 開発環境
export const devParameter: AppParameter = {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: 'us-east-1',
  },
  envName: 'dev',
  
  dynamoDbTablePrefix: 'AIPersonaDev',
  cognitoDomainPrefix: 'ai-persona-dev', // 一意のPrefixにする必要があるため末尾にランダムな文字列かアカウントIDを付与することを推奨：例: 'ai-persona-dev-ABC1234xyz-12345678910'
  // Cognito認証設定（CognitoStackデプロイ後に設定）
  // 1. cdk deploy AIPersonaCognito-dev を実行
  // 2. 出力されたUserPoolId, UserPoolClientId, CognitoDomainUrlを設定
  // 3. メインスタックを再デプロイ: cdk deploy AIPersona-dev
  cognitoUserPoolId: '', // 例: 'us-east-1_XXXXXXXXX'
  cognitoUserPoolAppId: '', // 例: 'xxxxxxxxxxxxxxxxxxxxxxxxxx'
  cognitoUserPoolDomain: '', // 例: 'ai-persona-dev-xxx.auth.us-east-1.amazoncognito.com'
  
  containerCpu: '1024',
  containerMemory: '4096',
  
  // CloudFront + WAF設定
  enableWaf: false,
  
  // AgentCore Memory設定
  // TODO: AgentCoreMemoryStackをデプロイ後、以下のIDを設定してください
  // 1. cdk deploy AIPersonaMemory-dev を実行
  // 2. 出力されたMemoryIdをagentCoreMemoryIdに設定
  // 3. AWS CLIコマンド（出力されたGetStrategyIdCommand）を実行してSummary Strategy IDを取得
  // 4. AWS CLIコマンド（出力されたGetSemanticStrategyIdCommand）を実行してSemantic Strategy IDを取得
  // 5. 取得したStrategy IDをそれぞれ設定
  // 6. メインスタックをデプロイ: cdk deploy AIPersona-dev
  agentCoreMemoryId: '', // 例: 'memory_ai_persona-FWI4503z4n'
  summaryMemoryStrategyId: '', // 例: 'summary-cGiRRh8umv'
  semanticMemoryStrategyId: '', // 例: 'semantic-XYZ1234abc' - TODO: デプロイ後に設定
  agentCoreMemoryEventExpiryDays: 30,
  
  bedrockModelId: 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  agentModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  
  // マスアンケート機能設定
  batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  surveyS3Prefix: 'survey-results/',
  batchInferenceS3Prefix: 'batch-inference/',
};

// 本番環境
export const prodParameter: AppParameter = {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: 'us-east-1',
  },
  envName: 'prod',
  
  dynamoDbTablePrefix: 'AIPersona',
  cognitoDomainPrefix: 'ai-persona-prod', // 一意のPrefixにする必要があるため末尾にランダムな文字列かアカウントIDを付与することを推奨：例: 'ai-persona-dev-ABC1234xyz-12345678910'
  cognitoUserPoolId: '',
  cognitoUserPoolAppId: '',
  cognitoUserPoolDomain: '',
  
  containerCpu: '2048',
  containerMemory: '8192',
  
  // CloudFront + WAF設定
  enableWaf: true,
  
  // AgentCore Memory設定
  // TODO: AgentCoreMemoryStackをデプロイ後、以下のIDを設定してください
  agentCoreMemoryId: '', // 例: 'memory_ai_persona-ABC1234xyz'
  summaryMemoryStrategyId: '', // 例: 'summary-XYZ9876abc'
  semanticMemoryStrategyId: '', // 例: 'semantic-DEF5678xyz'
  agentCoreMemoryEventExpiryDays: 90,
  
  bedrockModelId: 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  agentModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  
  // マスアンケート機能設定
  batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  surveyS3Prefix: 'survey-results/',
  batchInferenceS3Prefix: 'batch-inference/',
};
