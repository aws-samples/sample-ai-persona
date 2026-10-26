import { Construct } from 'constructs';
import { Stack, Token } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { acknowledgeAll, acknowledgeCfnValidate, NAG_REASONS } from '../nag-reasons';

/** 作る VPC エンドポイント 1 つ（EndpointSpec、entities.md）。 */
export interface EndpointSpec {
  /** AWS のサービス名の短い形（`com.amazonaws.<region>.` の後ろ）。 */
  readonly service: string;
  /** construct ID。 */
  readonly id: string;
  readonly purpose: string;
  /** 検証用 EC2 を作るときだけ作る。 */
  readonly onlyWithTestEnvironment: boolean;
  readonly awsService: ec2.InterfaceVpcEndpointAwsService;
}

/**
 * 閉域のインターフェイス型 VPC エンドポイント（U1 閉域、BR4.2、NFR3.7）。一覧はここにだけ持つ。
 *
 * bedrock-agentcore-control は aws-cdk-lib に定数がないため、名前から作る（アプリの
 * src/services/strategy_lookup.py が呼ぶ。NFR Design レビュー R-03）。ゲートウェイ型の
 * s3・dynamodb は VPC の gatewayEndpoints で作る。エンドポイントのポリシーは U2 で絞る。
 */
export const CLOSED_ENDPOINTS: readonly EndpointSpec[] = [
  { service: 'ecr.api', id: 'EcrApi', purpose: 'イメージの取得（認可・マニフェスト）', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.ECR },
  { service: 'ecr.dkr', id: 'EcrDocker', purpose: 'イメージの取得（レイヤーは S3 のゲートウェイ型）', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER },
  { service: 'logs', id: 'Logs', purpose: 'コンテナのログ', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS },
  { service: 'secretsmanager', id: 'SecretsManager', purpose: 'タスクの起動時の秘密', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER },
  { service: 'cognito-idp', id: 'CognitoIdp', purpose: '管理 API のログイン（AdminInitiateAuth）と利用者の一覧', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.COGNITO_IDP },
  { service: 'bedrock-runtime', id: 'BedrockRuntime', purpose: '推論（ペルソナの生成・議論）', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME },
  { service: 'bedrock', id: 'Bedrock', purpose: 'バッチ推論・モデルの一覧（U3 のアンケートで使う）', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK },
  { service: 'bedrock-agent-runtime', id: 'BedrockAgentRuntime', purpose: 'ナレッジベースの検索（Retrieve）', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME },
  { service: 'bedrock-agentcore', id: 'BedrockAgentCore', purpose: 'AgentCore Memory のデータプレーン', onlyWithTestEnvironment: false, awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE },
  { service: 'bedrock-agentcore-control', id: 'BedrockAgentCoreControl', purpose: 'AgentCore Memory の Strategy ID の取得（GetMemory）', onlyWithTestEnvironment: false, awsService: new ec2.InterfaceVpcEndpointAwsService('bedrock-agentcore-control') },
  { service: 'ssm', id: 'Ssm', purpose: '検証用 EC2 の Fleet Manager', onlyWithTestEnvironment: true, awsService: ec2.InterfaceVpcEndpointAwsService.SSM },
  { service: 'ssmmessages', id: 'SsmMessages', purpose: '検証用 EC2 の Fleet Manager（セッション）', onlyWithTestEnvironment: true, awsService: ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES },
  { service: 'ec2messages', id: 'Ec2Messages', purpose: '検証用 EC2 の SSM エージェント', onlyWithTestEnvironment: true, awsService: ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES },
];

/**
 * AWS 管理のプレフィックスリスト com.amazonaws.<region>.s3 / dynamodb の ID（NFR3.3）。
 * タスクの SG の外向き（ゲートウェイ型エンドポイントへの 443）に使う。fromLookup は synth の
 * たびに AWS へ問い合わせるため（CI は AWS に触れない）、リージョンごとの ID を持つ。
 * デプロイ前に次で確かめる:
 *   aws ec2 describe-managed-prefix-lists --region <region> \
 *     --filters Name=prefix-list-name,Values=com.amazonaws.<region>.s3,com.amazonaws.<region>.dynamodb
 */
export const S3_PREFIX_LISTS: Record<string, string> = {
  'ap-northeast-1': 'pl-61a54008',
};
export const DYNAMODB_PREFIX_LISTS: Record<string, string> = {
  'ap-northeast-1': 'pl-78a54011',
};

/**
 * 閉域の VPC が使う 2 つの AZ（Infrastructure Design レビュー R-05）。
 *
 * bedrock・bedrock-agent-runtime・bedrock-agentcore・bedrock-agentcore-control のエンドポイントは
 * ap-northeast-1 では 1a・1c・1d でだけ提供される（dev のアカウントで DescribeVpcEndpointServices
 * により確かめた）。maxAzs に任せると AZ の並びによっては作成が失敗しうるため、AZ を固定する。
 * AZ の名前はアカウントごとに割り当てが違うため、別のアカウントに置くときは次で確かめる:
 *   aws ec2 describe-vpc-endpoint-services --region <region> \
 *     --service-names com.amazonaws.<region>.bedrock-agentcore-control \
 *     --query 'ServiceDetails[].AvailabilityZones'
 */
export const CLOSED_AVAILABILITY_ZONES: Record<string, string[]> = {
  'ap-northeast-1': ['ap-northeast-1a', 'ap-northeast-1c'],
};

export interface ClosedNetworkProps {
  envName: string;
  /** 新規 VPC の CIDR。 */
  vpcCidr: string;
  /** Private Hosted Zone の名前（利用者が開く FQDN）。 */
  domainName: string;
  /** 検証用 EC2 のためのエンドポイント（ssm・ssmmessages・ec2messages）も作るか。 */
  createTestEnvironment: boolean;
}

/**
 * 閉域のネットワーク（U1 閉域、ADR-004、BR4.1〜BR4.4、App スタック）。
 *
 * - 新規 VPC: 2 AZ、PRIVATE_ISOLATED の /24 だけ。NAT・IGW・パブリックサブネットなし
 * - VPC エンドポイント: ゲートウェイ型 2（s3・dynamodb）、インターフェイス型 10（検証用に +3）
 * - インターフェイス型の共通の SG（受け入れ・外向きのルールなし。結線は AppStack が行う）
 * - FQDN の Private Hosted Zone（ALB への Alias は AppStack が足す）
 *
 * U2 で既存 VPC の取り込み・エンドポイントのポリシー・Flow Logs を足す。
 */
export class ClosedNetwork extends Construct {
  public readonly vpc: ec2.Vpc;
  public readonly endpointSecurityGroup: ec2.SecurityGroup;
  public readonly interfaceEndpoints: Record<string, ec2.InterfaceVpcEndpoint> = {};
  public readonly hostedZone: route53.PrivateHostedZone;
  public readonly s3PrefixListId: string;
  public readonly dynamoDbPrefixListId: string;

  constructor(scope: Construct, id: string, props: ClosedNetworkProps) {
    super(scope, id);

    const region = Stack.of(this).region;
    this.s3PrefixListId = lookup(S3_PREFIX_LISTS, region, 'S3 prefix list', 'S3_PREFIX_LISTS');
    this.dynamoDbPrefixListId = lookup(
      DYNAMODB_PREFIX_LISTS,
      region,
      'DynamoDB prefix list',
      'DYNAMODB_PREFIX_LISTS',
    );
    const availabilityZones = lookup(
      CLOSED_AVAILABILITY_ZONES,
      region,
      'availability zone list for the closed VPC',
      'CLOSED_AVAILABILITY_ZONES',
    );

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `ai-persona-v1-${props.envName}`,
      ipAddresses: ec2.IpAddresses.cidr(props.vpcCidr),
      availabilityZones,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'Isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
      gatewayEndpoints: {
        DynamoDb: { service: ec2.GatewayVpcEndpointAwsService.DYNAMODB },
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });
    acknowledgeAll(this.vpc, ['VPC7'], NAG_REASONS.closedFlowLogsUntilU2);
    for (const subnet of this.vpc.isolatedSubnets) {
      acknowledgeCfnValidate(subnet, ['W3010'], NAG_REASONS.pinnedAvailabilityZones);
    }

    this.endpointSecurityGroup = new ec2.SecurityGroup(this, 'EndpointSecurityGroup', {
      vpc: this.vpc,
      description: 'VPC interface endpoints of the AI Persona closed network (rules added by the app stack)',
      allowAllOutbound: false,
    });

    const subnets = { subnetType: ec2.SubnetType.PRIVATE_ISOLATED };
    for (const spec of CLOSED_ENDPOINTS) {
      if (spec.onlyWithTestEnvironment && !props.createTestEnvironment) {
        continue;
      }
      this.interfaceEndpoints[spec.service] = this.vpc.addInterfaceEndpoint(spec.id, {
        service: spec.awsService,
        privateDnsEnabled: true,
        subnets,
        securityGroups: [this.endpointSecurityGroup],
        // 既定（open: true）は VPC の CIDR からの 443 を足す。受け入れは AppStack が SG で足す
        open: false,
      });
    }

    this.hostedZone = new route53.PrivateHostedZone(this, 'HostedZone', {
      zoneName: props.domainName,
      vpc: this.vpc,
      comment: `AI Persona v1 ${props.envName} (closed network)`,
    });
  }
}

function lookup<T>(table: Record<string, T>, region: string, what: string, tableName: string): T {
  const value = Token.isUnresolved(region) ? undefined : table[region];
  if (value === undefined) {
    throw new Error(`No ${what} is known for region ${region}. Add it to ${tableName}.`);
  }
  return value;
}
