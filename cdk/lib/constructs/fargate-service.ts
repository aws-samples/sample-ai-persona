import { Construct } from 'constructs';
import { Duration, Stack, Token } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

/**
 * AWS 管理のプレフィックスリスト com.amazonaws.global.cloudfront.origin-facing の ID。
 * CloudFront の VPC オリジンの通信はこの範囲から来る（infrastructure-specification.md）。
 * fromLookup は synth のたびに AWS へ問い合わせるため（CI は AWS に触れない）、リージョンごとの
 * ID を持つ。デプロイ前に次で確かめる:
 *   aws ec2 describe-managed-prefix-lists --region <region> \
 *     --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing
 */
export const CLOUDFRONT_ORIGIN_FACING_PREFIX_LISTS: Record<string, string> = {
  'ap-northeast-1': 'pl-58a04531',
  'us-east-1': 'pl-3b927c52',
};

const CONTAINER_PORT = 80;
const HTTPS_PORT = 443;

/**
 * 内部 ALB の入口の形（U1 閉域、ADR-005、NFR3.4）。
 *
 * - cloudfront: CloudFront の VPC オリジンからの HTTP 80（公開構成。今のまま）
 * - closed: HTTPS 443 だけ（ACM の証明書、TLS 1.3・1.2）。受け入れの送り元とタスクの外向きは
 *   App スタックが SG に足す（AppStack.wireClosedSecurityGroups）
 */
export type FargateIngress = { kind: 'cloudfront' } | { kind: 'closed'; certificateArn: string };

export interface FargateAppServiceProps {
  vpc: ec2.IVpc;
  repository: ecr.IRepository;
  imageTag: string;
  /** CPU ユニット（1024 = 1 vCPU）。 */
  cpu: number;
  /** メモリ（MiB）。 */
  memoryLimitMiB: number;
  desiredCount: number;
  logGroup: logs.ILogGroup;
  environment: Record<string, string>;
  secrets: Record<string, ecs.Secret>;
  /** 入口の形（既定 cloudfront）。 */
  ingress?: FargateIngress;
  /** ALB とサービスを置くサブネットの種類（既定 PRIVATE_WITH_EGRESS。閉域は PRIVATE_ISOLATED）。 */
  subnetType?: ec2.SubnetType;
  /**
   * サービス（タスク）だけを置くサブネット（閉域対応 U2 BR2.4）。省略時は ALB と同じ subnetType の
   * サブネット。閉域で AZ の数 1 のとき、タスクをエンドポイントと同じ AZ に置くために渡す。
   */
  serviceSubnets?: ec2.SubnetSelection;
}

/**
 * アプリの ECS Fargate サービスと内部 ALB（FR9.3、ecs-patterns は使わない）。
 *
 * - タスク 1 vCPU / 4 GB、デプロイのサーキットブレーカーと自動ロールバック、
 *   ローリング（最小 100%・最大 200%）、stopTimeout 120 秒
 * - 内部 ALB は HTTP 80、無操作タイムアウト 300 秒（SSE）。ヘルスチェック /health
 * - ALB は CloudFront のプレフィックスリストからの 80 番だけを受け、タスクは ALB からだけを受ける
 * - 閉域の入口（ingress.kind = closed）では HTTPS 443 のリスナーだけを作り、タスクの SG を
 *   外向きなしで作る（U1 閉域、BR5.1・BR5.2・BR5.4）
 */
export class FargateAppService extends Construct {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly container: ecs.ContainerDefinition;
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly loadBalancerSecurityGroup: ec2.SecurityGroup;
  /** タスクの SG（公開は FargateService が作るもの、閉域は外向きなしで作るもの）。 */
  public readonly taskSecurityGroup: ec2.ISecurityGroup;

  constructor(scope: Construct, id: string, props: FargateAppServiceProps) {
    super(scope, id);

    const {
      vpc, repository, imageTag, cpu, memoryLimitMiB, desiredCount, logGroup, environment, secrets,
    } = props;
    const ingress: FargateIngress = props.ingress ?? { kind: 'cloudfront' };
    const privateSubnets = { subnetType: props.subnetType ?? ec2.SubnetType.PRIVATE_WITH_EGRESS };

    this.cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsightsV2: ecs.ContainerInsights.DISABLED });

    this.loadBalancerSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc,
      description:
        ingress.kind === 'cloudfront'
          ? 'Load balancer of the AI Persona app (CloudFront VPC origin only)'
          : 'Load balancer of the AI Persona app (closed network, HTTPS only)',
      allowAllOutbound: false,
    });
    if (ingress.kind === 'cloudfront') {
      this.loadBalancerSecurityGroup.addIngressRule(
        ec2.Peer.prefixList(this.cloudFrontPrefixListId()),
        ec2.Port.tcp(CONTAINER_PORT),
        'CloudFront origin-facing',
      );
    }

    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: false,
      vpcSubnets: privateSubnets,
      securityGroup: this.loadBalancerSecurityGroup,
      idleTimeout: Duration.seconds(300),
    });
    const listener =
      ingress.kind === 'cloudfront'
        ? this.loadBalancer.addListener('Http', {
          port: CONTAINER_PORT,
          protocol: elbv2.ApplicationProtocol.HTTP,
          open: false,
        })
        : this.loadBalancer.addListener('Https', {
          port: HTTPS_PORT,
          protocol: elbv2.ApplicationProtocol.HTTPS,
          certificates: [elbv2.ListenerCertificate.fromArn(ingress.certificateArn)],
          // ELBSecurityPolicy-TLS13-1-2-2021-06: TLS 1.3 と 1.2 だけ（NFR4.2）
          sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
          open: false,
        });
    // 閉域のタスクは外へ出る経路を持たない。エンドポイントへの 443 は App スタックが足す（BR5.4）
    const closedTaskSecurityGroup =
      ingress.kind === 'closed'
        ? new ec2.SecurityGroup(this, 'TaskSecurityGroup', {
          vpc,
          description: 'Tasks of the AI Persona app (closed network, VPC endpoints only)',
          allowAllOutbound: false,
        })
        : undefined;

    this.taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDefinition', {
      cpu,
      memoryLimitMiB,
    });
    this.container = this.taskDefinition.addContainer('App', {
      image: ecs.ContainerImage.fromEcrRepository(repository, imageTag),
      portMappings: [{ containerPort: CONTAINER_PORT }],
      environment,
      secrets,
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'app', logGroup }),
      stopTimeout: Duration.seconds(120),
    });

    this.service = new ecs.FargateService(this, 'Service', {
      cluster: this.cluster,
      taskDefinition: this.taskDefinition,
      desiredCount,
      assignPublicIp: false,
      vpcSubnets: props.serviceSubnets ?? privateSubnets,
      circuitBreaker: { enable: true, rollback: true },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      healthCheckGracePeriod: Duration.seconds(60),
      ...(closedTaskSecurityGroup ? { securityGroups: [closedTaskSecurityGroup] } : {}),
    });
    this.taskSecurityGroup = closedTaskSecurityGroup ?? this.service.connections.securityGroups[0];

    // cdk-nag の抑止（U9、NFR7.15）。理由は nag-reasons.ts
    acknowledgeAll(this.cluster, ['ECS4'], NAG_REASONS.containerInsights);
    acknowledgeAll(this.loadBalancer, ['ELB2'], NAG_REASONS.accessLogs);
    acknowledgeAll(this.taskDefinition, ['ECS2'], NAG_REASONS.ecsEnvironment);

    listener.addTargets('App', {
      port: CONTAINER_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [this.service],
      deregistrationDelay: Duration.seconds(30),
      healthCheck: {
        path: '/health',
        interval: Duration.seconds(30),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });
  }

  private cloudFrontPrefixListId(): string {
    const region = Stack.of(this).region;
    const id = Token.isUnresolved(region) ? undefined : CLOUDFRONT_ORIGIN_FACING_PREFIX_LISTS[region];
    if (!id) {
      throw new Error(
        `No CloudFront origin-facing prefix list is known for region ${region}. ` +
          'Add it to CLOUDFRONT_ORIGIN_FACING_PREFIX_LISTS.',
      );
    }
    return id;
  }
}
