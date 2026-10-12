import { Construct } from 'constructs';
import { Duration, Stack, Token } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';

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
}

/**
 * アプリの ECS Fargate サービスと内部 ALB（FR9.3、ecs-patterns は使わない）。
 *
 * - タスク 1 vCPU / 4 GB、デプロイのサーキットブレーカーと自動ロールバック、
 *   ローリング（最小 100%・最大 200%）、stopTimeout 120 秒
 * - 内部 ALB は HTTP 80、無操作タイムアウト 300 秒（SSE）。ヘルスチェック /health
 * - ALB は CloudFront のプレフィックスリストからの 80 番だけを受け、タスクは ALB からだけを受ける
 */
export class FargateAppService extends Construct {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly container: ecs.ContainerDefinition;
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: FargateAppServiceProps) {
    super(scope, id);

    const {
      vpc, repository, imageTag, cpu, memoryLimitMiB, desiredCount, logGroup, environment, secrets,
    } = props;
    const privateSubnets = { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS };

    this.cluster = new ecs.Cluster(this, 'Cluster', { vpc, containerInsightsV2: ecs.ContainerInsights.DISABLED });

    const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc,
      description: 'Load balancer of the AI Persona app (CloudFront VPC origin only)',
      allowAllOutbound: false,
    });
    albSecurityGroup.addIngressRule(
      ec2.Peer.prefixList(this.cloudFrontPrefixListId()),
      ec2.Port.tcp(CONTAINER_PORT),
      'CloudFront origin-facing',
    );

    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: false,
      vpcSubnets: privateSubnets,
      securityGroup: albSecurityGroup,
      idleTimeout: Duration.seconds(300),
    });
    const listener = this.loadBalancer.addListener('Http', {
      port: CONTAINER_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      open: false,
    });

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
      vpcSubnets: privateSubnets,
      circuitBreaker: { enable: true, rollback: true },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      healthCheckGracePeriod: Duration.seconds(60),
    });

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
