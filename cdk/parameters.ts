import { Environment } from 'aws-cdk-lib';

/** Public or closed-network deployment. The closed one is the Stage AIPersonaDevClosed. */
export type DeploymentMode = 'public' | 'closed';

/**
 * Where inference may be sent. global (the default) uses inference profiles that
 * route to any region; jp uses profiles that stay in Japan (Tokyo and Osaka).
 * Independent of the closed network: either Stage can use either value.
 */
export type InferenceScope = 'global' | 'jp';

/**
 * Input for building the closed-network Stage.
 *
 * Context values (-c closedCertificateArn=... and so on) are laid over the values of
 * closedDevParameter. The Stage is built only when both the certificate ARN and the
 * FQDN are given (one without the other is an error). The real values are not written
 * in the repository; they are passed as arguments of deploy.sh --closed.
 */
export interface ClosedStageParameters {
  /** Certificate of the closed ALB (ACM, in the Stage's region). Context closedCertificateArn. */
  certificateArn: string;
  /** FQDN users open (the Private Hosted Zone's name). Context closedDomainName. */
  domainName: string;
  /** Source CIDRs allowed to reach the ALB. Context closedAllowedCidrs (comma-separated). */
  allowedCidrs: string[];
  /** Whether to create a Windows EC2 for testing. Context closedCreateTestEnvironment. */
  createTestEnvironment: boolean;
  /** CIDR of the new VPC. Context closedVpcCidr. */
  vpcCidr: string;
  /**
   * Key of the Nemotron Parquet placed in advance (in the app's bucket).
   * Context closedNemotronDatasetKey. Left out or empty uses the default key
   * (persona-dataset/nemotron-personas-japan.parquet).
   */
  nemotronDatasetKey?: string;
  /**
   * Number of AZs for the interface endpoints (1 or 2). Context closedEndpointAzCount;
   * 2 when left out. With 1, the endpoints, the tasks and the test EC2 go in the VPC's
   * first AZ, while the internal ALB stays in 2 AZs.
   */
  endpointAzCount?: number;
  /**
   * Whether to create a Route 53 Resolver Inbound Endpoint that receives forwarding
   * from the corporate DNS. Context closedCreateResolverEndpoint; false (none) when
   * left out.
   */
  createResolverEndpoint?: boolean;
  /**
   * Sources the Resolver accepts (CIDRs of the corporate DNS). Context
   * closedResolverAllowedCidrs (comma-separated). One or more when
   * createResolverEndpoint is true, empty when it is false.
   */
  resolverAllowedCidrs?: string[];
}

export interface AppParameter {
  /** Where the Data and App stacks go (Edge is always us-east-1). */
  env?: Environment;
  envName: string;

  // Shape of the deployment (overridable with -c deploymentMode=... / -c appDesiredCount=...)
  deploymentMode?: DeploymentMode;
  /** Number of app tasks (1 to 10, default 1). No automatic scaling. */
  appDesiredCount?: number;

  // Size of the ECS Fargate task (CPU units, memory MiB)
  containerCpu: string;
  containerMemory: string;

  // CloudFront + WAF
  enableWaf?: boolean;
  allowedIpAddresses?: string[]; // CIDRs, e.g. ['203.0.113.0/24', '198.51.100.1/32']

  // AgentCore Memory (made by the Data stack; the app looks up the Memory ID and Strategy ID at run time)
  agentCoreMemoryEventExpiryDays?: number;

  // Bedrock
  /**
   * Inference scope. The context -c inferenceScope=<global|jp> wins; global when left
   * out. To keep using jp, write it here (a deploy.sh run without the argument goes back
   * to global and widens IAM).
   */
  inferenceScope?: InferenceScope;
  /**
   * The model IDs may be left out: then the scope's defaults (SCOPE_DEFAULTS in
   * deployment-profile.ts) are used. A value given is used as is, without checking it
   * against the scope, and the permission to call it is added to IAM.
   */
  bedrockModelId?: string;
  agentModelId?: string;
  // Whether the additional persona base models (GPT-5.6 Terra/Luna, Gemma 4 31B: non-Anthropic
  // models called through Bedrock Mantle) can be chosen (default false).
  // When on, the task role gets the bedrock-mantle permissions.
  enableAdditionalPersonaModels?: boolean;

  // Mass survey (on-demand inference from Step Functions). All three may be left out
  /** Model that writes the answers. Left out: the scope's default (Haiku 5.5). Only global.* or jp.* inference profile IDs. */
  surveyModelId?: string;
  /** Parallelism of one execution (Distributed Map MaxConcurrency, 1 to 1,000, default 64). */
  surveyMaxConcurrency?: number;
  /** Time limit of one execution (seconds, 600 to 86,400, default 7,200). Set together with the parallelism (cdk/README.md). */
  surveyExecutionTimeoutSeconds?: number;
  surveyS3Prefix?: string;

  // Kill switch of the structured dataset analysis tool (analyze_dataset), default on.
  // When off, personas get no analyze_dataset tool and no prompt for it.
  enableDatasetAnalysis?: boolean;

  // Kill switch of advanced generation (beta), default on. Redeploying with false makes the
  // app hide the advanced generation tab while keeping the Code Interpreter, the tables and
  // CORS. Always off in the closed Stage.
  enableAdvancedGeneration?: boolean;

  // Data analysis agent integration
  dataAgentRuntimeArn?: string;
  dataAgentRegion?: string;

  // MCP Gateway (AgentCore Gateway). Cannot be true in v1.0.0
  enableMcpGateway?: boolean;

  // Input of the closed network (read only by a Stage whose deploymentMode is closed)
  closed?: ClosedStageParameters;
}

// Development (Stage AIPersonaDev)
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

  // CloudFront + WAF (default on; false to save cost)
  enableWaf: true,

  agentCoreMemoryEventExpiryDays: 30,

  // Inference scope and model IDs left out (scope global, the scope's default models)
  enableAdditionalPersonaModels: false,

  // Mass survey
  surveyS3Prefix: 'survey-results/',

  // Structured dataset analysis tool (default on)
  enableDatasetAnalysis: true,

  // Data analysis agent integration (on as soon as a Runtime ARN is set)
  dataAgentRuntimeArn: '', // e.g. 'arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/xxx'
  // dataAgentRegion: 'ap-northeast-1', // the main stack's region when left out

  // MCP Gateway (AgentCore Gateway)
  enableMcpGateway: false,
};

// Closed-network development (Stage AIPersonaDevClosed). Copies the public dev values and
// changes envName so its names (AgentCore Memory and so on) do not collide with public dev.
// The certificate ARN and the FQDN stay empty here and are passed with deploy.sh --closed
// (-c closedCertificateArn=... -c closedDomainName=...). While empty, the Stage is not built.
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
    // dev puts the interface endpoints in 1 AZ (about half the monthly cost). For 2 AZs,
    // deploy.sh --endpoint-az-count 2 (-c closedEndpointAzCount=2)
    endpointAzCount: 1,
  },
};

// Production (no Stage is built in v1.0.0; the production Stage comes later)
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

  // CloudFront + WAF
  enableWaf: true,

  agentCoreMemoryEventExpiryDays: 90,

  // Inference scope and model IDs left out (scope global, the scope's default models)
  enableAdditionalPersonaModels: false,

  // Mass survey
  surveyS3Prefix: 'survey-results/',

  // Structured dataset analysis tool (default on)
  enableDatasetAnalysis: true,

  // Data analysis agent integration
  dataAgentRuntimeArn: '',

  // MCP Gateway (AgentCore Gateway)
  enableMcpGateway: false,
};
