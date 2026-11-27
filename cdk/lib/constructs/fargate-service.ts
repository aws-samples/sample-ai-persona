import { Construct } from 'constructs';
import { Duration, Stack, Token } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as logs from 'aws-cdk-lib/aws-logs';
import { acknowledgeAll, NAG_REASONS } from '../nag-reasons';

/**
 * ID of the AWS managed prefix list com.amazonaws.global.cloudfront.origin-facing.
 * Traffic from a CloudFront VPC origin comes from this range.
 * fromLookup queries AWS on every synth (CI never touches AWS), so keep the per-region
 * IDs here. Verify before deploying with:
 *   aws ec2 describe-managed-prefix-lists --region <region> \
 *     --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing
 */
export const CLOUDFRONT_ORIGIN_FACING_PREFIX_LISTS: Record<string, string> = {
  'ap-northeast-1': 'pl-58a04531',
  'us-east-1': 'pl-3b927c52',
};

/** ALB listener for the public configuration (HTTP from the CloudFront VPC origin). */
const HTTP_LISTENER_PORT = 80;
/** App container port. The container runs as non-root, so it is 1024 or above (keep in line with EXPOSE in the Dockerfile). */
const CONTAINER_PORT = 8080;
const HTTPS_PORT = 443;

/**
 * Shape of the internal ALB entry point.
 *
 * - cloudfront: HTTP 80 from the CloudFront VPC origin (public configuration)
 * - closed: HTTPS 443 only (ACM certificate, TLS 1.3 and 1.2). The App stack adds the allowed
 *   sources and the task egress to the SGs (AppStack.wireClosedSecurityGroups)
 */
export type FargateIngress = { kind: 'cloudfront' } | { kind: 'closed'; certificateArn: string };

export interface FargateAppServiceProps {
  vpc: ec2.IVpc;
  repository: ecr.IRepository;
  imageTag: string;
  /** CPU units (1024 = 1 vCPU). */
  cpu: number;
  /** Memory (MiB). */
  memoryLimitMiB: number;
  desiredCount: number;
  logGroup: logs.ILogGroup;
  environment: Record<string, string>;
  secrets: Record<string, ecs.Secret>;
  /** Entry point shape (default cloudfront). */
  ingress?: FargateIngress;
  /** Subnet type for the ALB and service (default PRIVATE_WITH_EGRESS; PRIVATE_ISOLATED for closed). */
  subnetType?: ec2.SubnetType;
  /**
   * Subnets for the service (tasks) only. Defaults to the subnets of the same subnetType as the ALB.
   * Passed in the closed configuration with one AZ to place tasks in the same AZ as the endpoints.
   */
  serviceSubnets?: ec2.SubnetSelection;
}

/**
 * The app ECS Fargate service and internal ALB (ecs-patterns is not used).
 *
 * - Task 1 vCPU / 4 GB, deployment circuit breaker with automatic rollback,
 *   rolling update (min 100%, max 200%), stopTimeout 120 s
 * - Internal ALB on HTTP 80, idle timeout 300 s (SSE). Health check /health
 * - Target (container) on HTTP 8080. The container runs as non-root (USER in the Dockerfile)
 * - The ALB accepts only port 80 from the CloudFront prefix list, and tasks accept only 8080 from the ALB
 * - With the closed entry point (ingress.kind = closed), only an HTTPS 443 listener is created and the
 *   task SG is created without egress
 */
export class FargateAppService extends Construct {
  public readonly cluster: ecs.Cluster;
  public readonly service: ecs.FargateService;
  public readonly container: ecs.ContainerDefinition;
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly loadBalancerSecurityGroup: ec2.SecurityGroup;
  /** Task SG (created by FargateService for public, created without egress for closed). */
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
        ec2.Port.tcp(HTTP_LISTENER_PORT),
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
          port: HTTP_LISTENER_PORT,
          protocol: elbv2.ApplicationProtocol.HTTP,
          open: false,
        })
        : this.loadBalancer.addListener('Https', {
          port: HTTPS_PORT,
          protocol: elbv2.ApplicationProtocol.HTTPS,
          certificates: [elbv2.ListenerCertificate.fromArn(ingress.certificateArn)],
          // ELBSecurityPolicy-TLS13-1-2-2021-06: TLS 1.3 and 1.2 only
          sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
          open: false,
        });
    // Closed tasks have no route out. The App stack adds 443 to the endpoints
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

    // cdk-nag suppressions; reasons are in nag-reasons.ts
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
