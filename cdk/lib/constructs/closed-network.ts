import { Construct } from 'constructs';
import { Stack, Token } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53resolver from 'aws-cdk-lib/aws-route53resolver';
import { DEFAULT_ENDPOINT_AZ_COUNT, EndpointAzCount, ResolverSettings } from '../deployment-profile';
import { EndpointAccessScope, endpointPolicyStatements, RestrictedEndpointService } from '../endpoint-policies';
import { acknowledgeAll, acknowledgeCfnValidate, NAG_REASONS } from '../nag-reasons';

/** Endpoint policy mode: restricted limits access to the app's resources; open attaches no policy (allow all). */
export type EndpointPolicyMode = 'restricted' | 'open';

/** One VPC interface endpoint to create. */
export interface EndpointSpec {
  /** Short AWS service name (the part after `com.amazonaws.<region>.`). */
  readonly service: string;
  /** Construct ID. */
  readonly id: string;
  readonly purpose: string;
  /** Created only when the test EC2 environment is enabled. */
  readonly onlyWithTestEnvironment: boolean;
  /** restricted attaches the policy from the table in endpoint-policies.ts; open attaches none. */
  readonly policy: EndpointPolicyMode;
  readonly awsService: ec2.InterfaceVpcEndpointAwsService;
}

/** One gateway VPC endpoint (created through the VPC's gatewayEndpoints). */
export interface GatewayEndpointSpec {
  readonly service: RestrictedEndpointService;
  /** Construct ID (child of the VPC). */
  readonly id: string;
  readonly awsService: ec2.GatewayVpcEndpointAwsService;
}

/** Gateway VPC endpoints of the closed network. Both are restricted by policy. */
export const CLOSED_GATEWAY_ENDPOINTS: readonly GatewayEndpointSpec[] = [
  { service: 'dynamodb', id: 'DynamoDb', awsService: ec2.GatewayVpcEndpointAwsService.DYNAMODB },
  { service: 's3', id: 'S3', awsService: ec2.GatewayVpcEndpointAwsService.S3 },
];

/**
 * Interface VPC endpoints of the closed network. This is the only place the list is kept.
 *
 * bedrock-agentcore-control has no constant in aws-cdk-lib, so it is built from the name (the
 * app's src/services/strategy_lookup.py calls it). The gateway endpoints for s3 and dynamodb are
 * in CLOSED_GATEWAY_ENDPOINTS. The 10 endpoints the app uses are restricted by policy; the 3 SSM
 * endpoints used only by the test EC2 are left open.
 */
export const CLOSED_ENDPOINTS: readonly EndpointSpec[] = [
  { service: 'ecr.api', id: 'EcrApi', purpose: 'イメージの取得（認可・マニフェスト）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.ECR },
  { service: 'ecr.dkr', id: 'EcrDocker', purpose: 'イメージの取得（レイヤーは S3 のゲートウェイ型）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER },
  { service: 'logs', id: 'Logs', purpose: 'コンテナのログ', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS },
  { service: 'secretsmanager', id: 'SecretsManager', purpose: 'タスクの起動時の秘密', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER },
  { service: 'cognito-idp', id: 'CognitoIdp', purpose: '管理 API のログイン（AdminInitiateAuth）と利用者の一覧', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.COGNITO_IDP },
  { service: 'bedrock-runtime', id: 'BedrockRuntime', purpose: '推論（ペルソナの生成・議論）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME },
  // Start, check and stop survey runs. Step Functions itself calls Bedrock and S3, and that traffic does
  // not go through the VPC. The bedrock (control plane) endpoint is no longer needed, so it was removed.
  { service: 'states', id: 'StepFunctions', purpose: 'アンケートの実行（StartExecution・DescribeExecution・StopExecution）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS },
  { service: 'bedrock-agent-runtime', id: 'BedrockAgentRuntime', purpose: 'ナレッジベースの検索（Retrieve）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME },
  { service: 'bedrock-agentcore', id: 'BedrockAgentCore', purpose: 'AgentCore Memory のデータプレーン', onlyWithTestEnvironment: false, policy: 'restricted', awsService: ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE },
  { service: 'bedrock-agentcore-control', id: 'BedrockAgentCoreControl', purpose: 'AgentCore Memory の Strategy ID の取得（GetMemory）', onlyWithTestEnvironment: false, policy: 'restricted', awsService: new ec2.InterfaceVpcEndpointAwsService('bedrock-agentcore-control') },
  { service: 'ssm', id: 'Ssm', purpose: '検証用 EC2 の Fleet Manager', onlyWithTestEnvironment: true, policy: 'open', awsService: ec2.InterfaceVpcEndpointAwsService.SSM },
  { service: 'ssmmessages', id: 'SsmMessages', purpose: '検証用 EC2 の Fleet Manager（セッション）', onlyWithTestEnvironment: true, policy: 'open', awsService: ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES },
  { service: 'ec2messages', id: 'Ec2Messages', purpose: '検証用 EC2 の SSM エージェント', onlyWithTestEnvironment: true, policy: 'open', awsService: ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES },
];

/**
 * IDs of the AWS-managed prefix lists com.amazonaws.<region>.s3 / dynamodb.
 * Used for the task SG egress (443 to the gateway endpoints). fromLookup would query AWS on every
 * synth (CI must not touch AWS), so the ID is kept per region.
 * Verify before deploying:
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
 * The two AZs used by the closed VPC.
 *
 * All 13 endpoint services used in the closed network are available in every usable AZ of
 * ap-northeast-1 (1a, 1c, 1d).
 * The two AZs are fixed so that placement does not change on every synth. Check which AZs offer
 * the endpoints with:
 *   aws ec2 describe-vpc-endpoint-services --region <region> \
 *     --service-names com.amazonaws.<region>.bedrock-agentcore-control \
 *     --query 'ServiceDetails[].AvailabilityZones'
 */
export const CLOSED_AVAILABILITY_ZONES: Record<string, string[]> = {
  'ap-northeast-1': ['ap-northeast-1a', 'ap-northeast-1c'],
};

export interface ClosedNetworkProps {
  envName: string;
  /** CIDR of the new VPC. */
  vpcCidr: string;
  /** Name of the Private Hosted Zone (the FQDN users open). */
  domainName: string;
  /** Also create the endpoints for the test EC2 (ssm, ssmmessages, ec2messages). */
  createTestEnvironment: boolean;
  /** Number of AZs for the interface endpoints (default 2). */
  endpointAzCount?: EndpointAzCount;
  /** Pass only when a Resolver inbound endpoint should be created. */
  resolver?: ResolverSettings;
}

/**
 * Which AZ subnets each resource is placed in.
 *
 * The VPC always has subnets in two AZs. With an endpoint AZ count of 1, interface endpoints, tasks
 * and the test EC2 go to the first AZ, while the internal ALB and the Resolver stay on both AZs.
 */
export interface NetworkPlacement {
  readonly vpcAzs: readonly string[];
  readonly endpointAzs: readonly string[];
  /** AZs for tasks and the test EC2 (same as endpointAzs). */
  readonly computeAzs: readonly string[];
  readonly loadBalancerAzs: readonly string[];
  readonly resolverAzs: readonly string[];
  readonly endpointSubnets: ec2.SubnetSelection;
  readonly computeSubnets: ec2.SubnetSelection;
  readonly loadBalancerSubnets: ec2.SubnetSelection;
}

/** The subnets of `azs`; every VPC AZ is the plain PRIVATE_ISOLATED selection. */
function isolatedSubnetsIn(azs: readonly string[], vpcAzs: readonly string[]): ec2.SubnetSelection {
  const subnetType = ec2.SubnetType.PRIVATE_ISOLATED;
  return azs.length === vpcAzs.length ? { subnetType } : { subnetType, availabilityZones: [...azs] };
}

/** Where to put each resource for an endpoint AZ count. */
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

/** DNS (TCP and UDP 53), accepted by the Resolver. */
const DNS_PORTS = [ec2.Port.tcp(53), ec2.Port.udp(53)];

/**
 * Allow `ports` from each CIDR into `securityGroup` (CIDR by CIDR, then port by port). Used for
 * the closed ALB's allowed clients (AppStack) and the Resolver's corporate DNS servers.
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
 * Network of the closed environment (used by the App stack).
 *
 * - New VPC: 2 AZs, PRIVATE_ISOLATED /24 subnets only. No NAT, IGW or public subnets
 * - VPC endpoints: 2 gateway (s3, dynamodb), 10 interface (+3 for the test EC2).
 *   Interface endpoints go in the subnets of placement.endpointAzs
 * - A shared SG for the interface endpoints (no ingress/egress rules; AppStack wires them)
 * - A Private Hosted Zone for the FQDN (AppStack adds the Alias to the ALB)
 * - A Resolver inbound endpoint (only when requested)
 *
 * Endpoint policies are attached by AppStack through restrictEndpoints, after the resources they
 * grant access to exist in the App stack. Flow Logs are not created. Placing the network in an
 * existing VPC may be considered in a later release.
 */
export class ClosedNetwork extends Construct {
  public readonly vpc: ec2.Vpc;
  public readonly endpointSecurityGroup: ec2.SecurityGroup;
  public readonly interfaceEndpoints: Record<string, ec2.InterfaceVpcEndpoint> = {};
  public readonly hostedZone: route53.PrivateHostedZone;
  public readonly s3PrefixListId: string;
  public readonly dynamoDbPrefixListId: string;
  public readonly placement: NetworkPlacement;
  /** Present only when requested. */
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
        // The default (open: true) adds 443 from the VPC CIDR. AppStack adds ingress through the SG instead
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
        // One per AZ subnet, always two AZs even with an endpoint AZ count of 1. AWS assigns the addresses
        ipAddresses: this.vpc
          .selectSubnets(isolatedSubnetsIn(this.placement.resolverAzs, availabilityZones))
          .subnets.map((subnet) => ({ subnetId: subnet.subnetId })),
        securityGroupIds: [this.resolverSecurityGroup.securityGroupId],
      });
    }
  }

  /**
   * Accept only TCP and UDP 53 from the corporate DNS CIDRs. The SG passes responses, so no egress
   * rules are added (allowAllOutbound: false is explicit because the CDK default allows all egress).
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
   * Attach policies that limit access targets to the 12 endpoints the app uses.
   *
   * AppStack passes an EndpointAccessScope built from the same references as the task role's permissions.
   * The 3 SSM endpoints used only by the test EC2 (policy: open) get none. Can be called only once.
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
