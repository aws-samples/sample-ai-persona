import { Construct } from 'constructs';
import { Stack, Token } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53resolver from 'aws-cdk-lib/aws-route53resolver';
import { DEFAULT_ENDPOINT_AZ_COUNT, EndpointAzCount, ResolverSettings } from '../deployment-profile';
import { EndpointAccessScope, endpointPolicyStatements, RestrictedEndpointService } from '../endpoint-policies';
import { acknowledgeAll, acknowledgeCfnValidate, NAG_REASONS } from '../nag-reasons';

/** エンドポイントのポリシー（閉域対応 U2）: restricted はアプリの資源に絞り、open は付けない（すべて許可）。 */
export type EndpointPolicyMode = 'restricted' | 'open';

/** 作る VPC エンドポイント 1 つ（EndpointSpec、entities.md）。 */
export interface EndpointSpec {
  /** AWS のサービス名の短い形（`com.amazonaws.<region>.` の後ろ）。 */
  readonly service: string;
  /** construct ID。 */
  readonly id: string;
  readonly purpose: string;
  /** 検証用 EC2 を作るときだけ作る。 */
  readonly onlyWithTestEnvironment: boolean;
  /** restricted は endpoint-policies.ts の表のポリシーを付ける（BR1.1）。open は付けない（BR1.7）。 */
  readonly policy: EndpointPolicyMode;
  readonly awsService: ec2.InterfaceVpcEndpointAwsService;
}

/** ゲートウェイ型の VPC エンドポイント 1 つ（VPC の gatewayEndpoints で作る）。 */
export interface GatewayEndpointSpec {
  readonly service: RestrictedEndpointService;
  /** construct ID（VPC の子）。 */
  readonly id: string;
  readonly awsService: ec2.GatewayVpcEndpointAwsService;
}

/** 閉域のゲートウェイ型 VPC エンドポイント（U1 閉域）。どちらもポリシーで絞る（閉域対応 U2 BR1.1）。 */
export const CLOSED_GATEWAY_ENDPOINTS: readonly GatewayEndpointSpec[] = [
  { service: 'dynamodb', id: 'DynamoDb', awsService: ec2.GatewayVpcEndpointAwsService.DYNAMODB },
  { service: 's3', id: 'S3', awsService: ec2.GatewayVpcEndpointAwsService.S3 },
];

/**
 * 閉域のインターフェイス型 VPC エンドポイント（U1 閉域、BR4.2、NFR3.7）。一覧はここにだけ持つ。
 *
 * bedrock-agentcore-control は aws-cdk-lib に定数がないため、名前から作る（アプリの
 * src/services/strategy_lookup.py が呼ぶ。NFR Design レビュー R-03）。ゲートウェイ型の
 * s3・dynamodb は CLOSED_GATEWAY_ENDPOINTS。アプリが使う 10 種はポリシーで絞り、検証用 EC2 だけが
 * 使う SSM の 3 つは絞らない（閉域対応 U2 BR1.1、BR1.7）。
 */
export const CLOSED_ENDPOINTS: readonly EndpointSpec[] = [
  { service: 'ecr.api', id: 'EcrApi', purpose: 'イメージの取得（認可・マニフェスト）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.ECR },
  { service: 'ecr.dkr', id: 'EcrDocker', purpose: 'イメージの取得（レイヤーは S3 のゲートウェイ型）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER },
  { service: 'logs', id: 'Logs', purpose: 'コンテナのログ', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS },
  { service: 'secretsmanager', id: 'SecretsManager', purpose: 'タスクの起動時の秘密', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER },
  { service: 'cognito-idp', id: 'CognitoIdp', purpose: '管理 API のログイン（AdminInitiateAuth）と利用者の一覧', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.COGNITO_IDP },
  { service: 'bedrock-runtime', id: 'BedrockRuntime', purpose: '推論（ペルソナの生成・議論）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME },
  { service: 'bedrock', id: 'Bedrock', purpose: 'バッチ推論・モデルの一覧（U3 のアンケートで使う）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK },
  { service: 'bedrock-agent-runtime', id: 'BedrockAgentRuntime', purpose: 'ナレッジベースの検索（Retrieve）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME },
  { service: 'bedrock-agentcore', id: 'BedrockAgentCore', purpose: 'AgentCore Memory のデータプレーン', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE },
  { service: 'bedrock-agentcore-control', id: 'BedrockAgentCoreControl', purpose: 'AgentCore Memory の Strategy ID の取得（GetMemory）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: new ec2.InterfaceVpcEndpointAwsService('bedrock-agentcore-control') },
  { service: 'ssm', id: 'Ssm', purpose: '検証用 EC2 の Fleet Manager', onlyWithTestEnvironment: true, policy: 'open', awsService: ec2.InterfaceVpcEndpointAwsService.SSM },
  { service: 'ssmmessages', id: 'SsmMessages', purpose: '検証用 EC2 の Fleet Manager（セッション）', onlyWithTestEnvironment: true, policy: 'open', awsService: ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES },
  { service: 'ec2messages', id: 'Ec2Messages', purpose: '検証用 EC2 の SSM エージェント', onlyWithTestEnvironment: true, policy: 'open', awsService: ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES },
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
  /** インターフェイス型エンドポイントを置く AZ の数（閉域対応 U2 BR2.1、既定 2）。 */
  endpointAzCount?: EndpointAzCount;
  /** Resolver Inbound Endpoint を作るときだけ渡す（閉域対応 U2 BR3.1）。 */
  resolver?: ResolverSettings;
}

/**
 * 資源をどの AZ のサブネットに置くか（閉域対応 U2、entities.md の NetworkPlacement）。
 *
 * VPC は常に 2 つの AZ のサブネットを持つ。AZ の数 1 では、インターフェイス型エンドポイント・タスク・
 * 検証用 EC2 を先頭の AZ に置き、内部 ALB と Resolver は 2 AZ のまま（BR2.3、BR2.4、BR3.5）。
 */
export interface NetworkPlacement {
  readonly vpcAzs: readonly string[];
  readonly endpointAzs: readonly string[];
  /** タスクと検証用 EC2 の AZ（endpointAzs と同じ。Q7 A）。 */
  readonly computeAzs: readonly string[];
  readonly loadBalancerAzs: readonly string[];
  readonly resolverAzs: readonly string[];
  readonly endpointSubnets: ec2.SubnetSelection;
  readonly computeSubnets: ec2.SubnetSelection;
  readonly loadBalancerSubnets: ec2.SubnetSelection;
}

/** The subnets of `azs`; every VPC AZ is the plain PRIVATE_ISOLATED selection (U1 unchanged). */
function isolatedSubnetsIn(azs: readonly string[], vpcAzs: readonly string[]): ec2.SubnetSelection {
  const subnetType = ec2.SubnetType.PRIVATE_ISOLATED;
  return azs.length === vpcAzs.length ? { subnetType } : { subnetType, availabilityZones: [...azs] };
}

/** Where to put each resource for an endpoint AZ count (BR2.3, BR2.4, BR3.5). */
export function networkPlacement(vpcAzs: readonly string[], endpointAzCount: EndpointAzCount): NetworkPlacement {
  const endpointAzs = vpcAzs.slice(0, endpointAzCount);
  const endpointSubnets = isolatedSubnetsIn(endpointAzs, vpcAzs);
  return Object.freeze({
    vpcAzs: [...vpcAzs],
    endpointAzs,
    computeAzs: endpointAzs,
    loadBalancerAzs: [...vpcAzs],
    resolverAzs: [...vpcAzs],
    endpointSubnets,
    computeSubnets: endpointSubnets,
    loadBalancerSubnets: isolatedSubnetsIn(vpcAzs, vpcAzs),
  });
}

/** DNS（TCP・UDP の 53）。Resolver の受け入れ（BR3.4）。 */
const DNS_PORTS = [ec2.Port.tcp(53), ec2.Port.udp(53)];

/**
 * Allow `ports` from each CIDR into `securityGroup` (CIDR by CIDR, then port by port). Used for
 * the closed ALB's allowed clients (AppStack) and the Resolver's corporate DNS servers (BR3.4).
 */
export function allowIngressFromCidrs(
  securityGroup: ec2.ISecurityGroup,
  cidrs: readonly string[],
  ports: readonly ec2.Port[],
  describe: (cidr: string) => string,
): void {
  for (const cidr of cidrs) {
    for (const port of ports) {
      securityGroup.addIngressRule(ec2.Peer.ipv4(cidr), port, describe(cidr));
    }
  }
}

/**
 * 閉域のネットワーク（U1 閉域、ADR-004、BR4.1〜BR4.4、App スタック）。
 *
 * - 新規 VPC: 2 AZ、PRIVATE_ISOLATED の /24 だけ。NAT・IGW・パブリックサブネットなし
 * - VPC エンドポイント: ゲートウェイ型 2（s3・dynamodb）、インターフェイス型 10（検証用に +3）。
 *   インターフェイス型は placement.endpointAzs のサブネットに置く（閉域対応 U2 BR2.3）
 * - インターフェイス型の共通の SG（受け入れ・外向きのルールなし。結線は AppStack が行う）
 * - FQDN の Private Hosted Zone（ALB への Alias は AppStack が足す）
 * - Resolver Inbound Endpoint（指定したときだけ。閉域対応 U2 BR3.1、BR3.4、BR3.5）
 *
 * エンドポイントのポリシー（閉域対応 U2 BR1.1）は、アクセス先の資源が App スタックにできてから
 * AppStack が restrictEndpoints で付ける。Flow Logs は作らない（BR4.1）。既存 VPC に置くオプションは
 * 後続のリリースで検討する（BR4.2）。
 */
export class ClosedNetwork extends Construct {
  public readonly vpc: ec2.Vpc;
  public readonly endpointSecurityGroup: ec2.SecurityGroup;
  public readonly interfaceEndpoints: Record<string, ec2.InterfaceVpcEndpoint> = {};
  public readonly hostedZone: route53.PrivateHostedZone;
  public readonly s3PrefixListId: string;
  public readonly dynamoDbPrefixListId: string;
  public readonly placement: NetworkPlacement;
  /** 指定したときだけある（閉域対応 U2 BR3.1）。 */
  public readonly resolverEndpoint?: route53resolver.CfnResolverEndpoint;
  public readonly resolverSecurityGroup?: ec2.SecurityGroup;
  private restricted = false;

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
      gatewayEndpoints: Object.fromEntries(
        CLOSED_GATEWAY_ENDPOINTS.map((spec) => [spec.id, { service: spec.awsService }]),
      ),
    });
    acknowledgeAll(this.vpc, ['VPC7'], NAG_REASONS.closedFlowLogsNotCreated);
    for (const subnet of this.vpc.isolatedSubnets) {
      acknowledgeCfnValidate(subnet, ['W3010'], NAG_REASONS.pinnedAvailabilityZones);
    }
    this.placement = networkPlacement(availabilityZones, props.endpointAzCount ?? DEFAULT_ENDPOINT_AZ_COUNT);

    this.endpointSecurityGroup = new ec2.SecurityGroup(this, 'EndpointSecurityGroup', {
      vpc: this.vpc,
      description: 'VPC interface endpoints of the AI Persona closed network (rules added by the app stack)',
      allowAllOutbound: false,
    });

    for (const spec of CLOSED_ENDPOINTS) {
      if (spec.onlyWithTestEnvironment && !props.createTestEnvironment) {
        continue;
      }
      this.interfaceEndpoints[spec.service] = this.vpc.addInterfaceEndpoint(spec.id, {
        service: spec.awsService,
        privateDnsEnabled: true,
        subnets: this.placement.endpointSubnets,
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

    if (props.resolver) {
      this.resolverSecurityGroup = this.createResolverSecurityGroup(props.resolver);
      this.resolverEndpoint = new route53resolver.CfnResolverEndpoint(this, 'ResolverInboundEndpoint', {
        name: `ai-persona-v1-${props.envName}-inbound`,
        direction: 'INBOUND',
        // BR3.5: 2 つの AZ のサブネットに 1 つずつ（AZ の数 1 でも 2 AZ）。アドレスは AWS が割り当てる
        ipAddresses: this.vpc
          .selectSubnets(isolatedSubnetsIn(this.placement.resolverAzs, availabilityZones))
          .subnets.map((subnet) => ({ subnetId: subnet.subnetId })),
        securityGroupIds: [this.resolverSecurityGroup.securityGroupId],
      });
    }
  }

  /**
   * BR3.4: 社内 DNS の CIDR から TCP・UDP の 53 だけを受ける。SG は応答を通すため外向きのルールは
   * 足さない（CDK の既定はすべての外向きを許すため、allowAllOutbound: false を明示する）。
   */
  private createResolverSecurityGroup(resolver: ResolverSettings): ec2.SecurityGroup {
    const securityGroup = new ec2.SecurityGroup(this, 'ResolverSecurityGroup', {
      vpc: this.vpc,
      description: 'Route 53 Resolver inbound endpoint of the AI Persona closed network (DNS from the corporate network only)',
      allowAllOutbound: false,
    });
    allowIngressFromCidrs(securityGroup, resolver.allowedCidrs, DNS_PORTS, (cidr) => `Corporate DNS ${cidr}`);
    return securityGroup;
  }

  /**
   * アプリが使う 12 種のエンドポイントに、アクセス先を絞るポリシーを付ける（閉域対応 U2 BR1.1、BR1.7）。
   *
   * AppStack が、タスクロールの権限と同じ参照から作った EndpointAccessScope を渡す（BR1.3）。
   * 検証用 EC2 だけが使う SSM の 3 つ（policy: open）には付けない。1 度だけ呼べる。
   */
  public restrictEndpoints(access: EndpointAccessScope): void {
    if (this.restricted) {
      throw new Error(`${this.node.path}: restrictEndpoints() was already called.`);
    }
    this.restricted = true;
    type Target = readonly [RestrictedEndpointService, ec2.GatewayVpcEndpoint | ec2.InterfaceVpcEndpoint | undefined];
    const targets: Target[] = [
      ...CLOSED_GATEWAY_ENDPOINTS.map(
        (spec): Target => [spec.service, this.vpc.node.tryFindChild(spec.id) as ec2.GatewayVpcEndpoint | undefined],
      ),
      ...CLOSED_ENDPOINTS.filter((spec) => spec.policy === 'restricted').map(
        (spec): Target => [spec.service as RestrictedEndpointService, this.interfaceEndpoints[spec.service]],
      ),
    ];
    for (const [service, endpoint] of targets) {
      if (!endpoint) {
        throw new Error(`${this.node.path}: the ${service} endpoint was not found to attach its policy to.`);
      }
      for (const statement of endpointPolicyStatements(service, access)) {
        endpoint.addToPolicy(statement);
      }
    }
  }
}

function lookup<T>(table: Record<string, T>, region: string, what: string, tableName: string): T {
  const value = Token.isUnresolved(region) ? undefined : table[region];
  if (value === undefined) {
    throw new Error(`No ${what} is known for region ${region}. Add it to ${tableName}.`);
  }
  return value;
}
