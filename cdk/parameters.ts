import { Environment } from 'aws-cdk-lib';

/** 公開構成（public）か閉域構成（closed）か。closed は Stage AIPersonaDevClosed で作る。 */
export type DeploymentMode = 'public' | 'closed';

/**
 * 閉域の Stage を作るための入力（ClosedStageParameters、U1 閉域）。
 *
 * closedDevParameter の値に、コンテキスト（-c closedCertificateArn=... など）が重なる。
 * 証明書の ARN と FQDN の両方がそろったときだけ閉域の Stage を作る（片方だけはエラー）。
 * 実際の値はリポジトリに書かず、deploy.sh --closed の引数で渡す。
 */
export interface ClosedStageParameters {
  /** 閉域の ALB の証明書（ACM。Stage と同じリージョン）。コンテキスト closedCertificateArn。 */
  certificateArn: string;
  /** 利用者が開く FQDN（Private Hosted Zone の名前）。コンテキスト closedDomainName。 */
  domainName: string;
  /** ALB に届いてよい送り元の CIDR。コンテキスト closedAllowedCidrs（カンマ区切り）。 */
  allowedCidrs: string[];
  /** 検証用の Windows EC2 を作るか。コンテキスト closedCreateTestEnvironment。 */
  createTestEnvironment: boolean;
  /** 新規 VPC の CIDR。コンテキスト closedVpcCidr。 */
  vpcCidr: string;
}

export interface AppParameter {
  /** Data・App スタックの置き場所（Edge は常に us-east-1）。 */
  env?: Environment;
  envName: string;

  // デプロイの形（コンテキスト -c deploymentMode=... / -c appDesiredCount=... で上書きできる）
  deploymentMode?: DeploymentMode;
  /** アプリのタスクの台数（1〜10、既定 1）。自動の増減はしない。 */
  appDesiredCount?: number;

  // ECS Fargate のタスクの大きさ（CPU ユニット、メモリ MiB）
  containerCpu: string;
  containerMemory: string;

  // CloudFront + WAF設定
  enableWaf?: boolean;
  allowedIpAddresses?: string[]; // CIDR形式 例: ['203.0.113.0/24', '198.51.100.1/32']

  // AgentCore Memory（Data スタックが作る。Memory ID・Strategy ID はアプリが実行時に引く）
  agentCoreMemoryEventExpiryDays?: number;

  // Bedrock設定
  bedrockModelId: string;
  agentModelId: string;
  // 追加ペルソナベースモデル（GPT-5.6 Terra/Luna、Gemma 4 31B。いずれもBedrock Mantle
  // 経由で呼び出す非Anthropicモデル）を選択可能にするか（既定false）。
  // 有効時はtaskRoleにbedrock-mantle権限を付与する
  enableAdditionalPersonaModels?: boolean;

  // マスアンケート機能設定
  batchInferenceModelId: string;
  surveyS3Prefix?: string;
  batchInferenceS3Prefix?: string;

  // データセット構造化分析ツール(analyze_dataset)のkill switch（既定: 有効）。
  // 無効化するとペルソナに analyze_dataset ツールが登録されずプロンプトも付与されない。
  enableDatasetAnalysis?: boolean;

  // データ分析エージェント連携設定
  dataAgentRuntimeArn?: string;
  dataAgentRegion?: string;

  // MCP Gateway設定（AgentCore Gateway）。v1.0.0 では true にできない（U8 以降）
  enableMcpGateway?: boolean;

  // 閉域構成の入力（deploymentMode が closed の Stage だけが読む）
  closed?: ClosedStageParameters;
}

// 開発環境（Stage AIPersonaDev）
export const devParameter: AppParameter = {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: 'ap-northeast-1',
  },
  envName: 'dev',

  deploymentMode: 'public',
  appDesiredCount: 1,

  containerCpu: '1024',
  containerMemory: '4096',

  // CloudFront + WAF設定
  enableWaf: false,

  agentCoreMemoryEventExpiryDays: 30,

  bedrockModelId: 'global.anthropic.claude-sonnet-5',
  agentModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  enableAdditionalPersonaModels: false,

  // マスアンケート機能設定
  batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  surveyS3Prefix: 'survey-results/',
  batchInferenceS3Prefix: 'batch-inference/',

  // データセット構造化分析ツール（既定: 有効）
  enableDatasetAnalysis: true,

  // データ分析エージェント連携設定（Runtime ARNを設定すると自動で有効化）
  dataAgentRuntimeArn: '', // 例: 'arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/xxx'
  // dataAgentRegion: 'ap-northeast-1', // 省略時はメインスタックのリージョンを使用

  // MCP Gateway設定（AgentCore Gateway）
  enableMcpGateway: false,
};

// 閉域の開発環境（Stage AIPersonaDevClosed、U1 閉域）。公開の dev の値を写し、
// envName を変えて公開の dev と名前（AgentCore Memory など）が重ならないようにする。
// 証明書の ARN と FQDN は空のまま置き、deploy.sh --closed（-c closedCertificateArn=...
// -c closedDomainName=...）で渡す。空のままなら閉域の Stage は作らない。
export const closedDevParameter: AppParameter = {
  ...devParameter,
  envName: 'dev-closed',
  deploymentMode: 'closed',
  closed: {
    certificateArn: '',
    domainName: '',
    allowedCidrs: [],
    createTestEnvironment: false,
    vpcCidr: '10.0.0.0/16',
  },
};

// 本番環境（v1.0.0 では Stage を作らない。本番の Stage は後で足す）
export const prodParameter: AppParameter = {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: 'ap-northeast-1',
  },
  envName: 'prod',

  deploymentMode: 'public',
  appDesiredCount: 2,

  containerCpu: '2048',
  containerMemory: '8192',

  // CloudFront + WAF設定
  enableWaf: true,

  agentCoreMemoryEventExpiryDays: 90,

  bedrockModelId: 'global.anthropic.claude-sonnet-5',
  agentModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  enableAdditionalPersonaModels: false,

  // マスアンケート機能設定
  batchInferenceModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
  surveyS3Prefix: 'survey-results/',
  batchInferenceS3Prefix: 'batch-inference/',

  // データセット構造化分析ツール（既定: 有効）
  enableDatasetAnalysis: true,

  // データ分析エージェント連携設定
  dataAgentRuntimeArn: '',

  // MCP Gateway設定（AgentCore Gateway）
  enableMcpGateway: false,
};
