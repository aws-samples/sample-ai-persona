import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as s3 from 'aws-cdk-lib/aws-s3';

export interface ExpressServiceProps {
  vpc: ec2.IVpc;
  ecrRepository: ecr.IRepository;
  envName: string;
  containerCpu: string;
  containerMemory: string;
  dynamoDbTables: dynamodb.ITable[];
  dynamoDbTablePrefix: string;
  awsRegion: string;
  bedrockModelId: string;
  agentModelId: string;
  agentCoreMemoryId?: string;
  summaryMemoryStrategyId?: string;
  semanticMemoryStrategyId?: string;
  uploadBucket: s3.IBucket;
  bedrockBatchRoleArn: string;
  batchInferenceModelId: string;
  imageTag?: string;
  surveyS3Prefix?: string;
  batchInferenceS3Prefix?: string;
}

export class ExpressService extends Construct {
  public readonly service: ecs.CfnExpressGatewayService;
  public readonly endpoint: string;
  public readonly loadBalancerArn: string;
  public readonly listenerArn: string;
  public readonly certificateArn: string;

  constructor(scope: Construct, id: string, props: ExpressServiceProps) {
    super(scope, id);

    const {
      vpc, ecrRepository, envName, containerCpu, containerMemory,
      dynamoDbTables, dynamoDbTablePrefix, awsRegion,
      bedrockModelId, agentModelId,
      agentCoreMemoryId, summaryMemoryStrategyId, semanticMemoryStrategyId,
      uploadBucket, bedrockBatchRoleArn, batchInferenceModelId,
      surveyS3Prefix = 'survey-results/',
      batchInferenceS3Prefix = 'batch-inference/',
    } = props;

    // Task Execution Role
    const executionRole = new iam.Role(this, 'ExecutionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy'),
      ],
    });
    ecrRepository.grantPull(executionRole);

    // Task Role
    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
    });
    for (const table of dynamoDbTables) {
      table.grantReadWriteData(taskRole);
    }
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:ListTables'],
      resources: ['*'],
    }));
    uploadBucket.grantReadWrite(taskRole);
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream',
        'bedrock:ListFoundationModels', 'bedrock:GetFoundationModel',
        'bedrock:ListInferenceProfiles', 'bedrock:GetInferenceProfile',
        'bedrock:TagResource', 'bedrock:UntagResource', 'bedrock:ListTagsForResource',
        'bedrock:CreateModelInvocationJob', 'bedrock:GetModelInvocationJob',
        'bedrock:ListModelInvocationJobs', 'bedrock:StopModelInvocationJob',
        'bedrock:Retrieve',
      ],
      resources: ['*'],
    }));
    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [bedrockBatchRoleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' } },
    }));
    if (agentCoreMemoryId) {
      taskRole.addToPolicy(new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:GetMemory', 'bedrock-agentcore:ListMemories',
          'bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents', 'bedrock-agentcore:GetEvent',
          'bedrock-agentcore:CreateMemoryRecord', 'bedrock-agentcore:BatchCreateMemoryRecords',
          'bedrock-agentcore:ListMemoryRecords', 'bedrock-agentcore:GetMemoryRecord',
          'bedrock-agentcore:DeleteMemoryRecord', 'bedrock-agentcore:SearchMemoryRecords',
          'bedrock-agentcore:RetrieveMemoryRecords',
          'bedrock-agentcore:InvokeMemory', 'bedrock-agentcore:RetrieveMemory',
        ],
        resources: ['*'],
      }));
    }

    // Infrastructure Role
    const infrastructureRole = new iam.Role(this, 'InfrastructureRole', {
      assumedBy: new iam.ServicePrincipal('ecs.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSInfrastructureRoleforExpressGatewayServices'),
      ],
    });

    // Environment variables
    const environment: { name: string; value: string }[] = [
      { name: 'AWS_REGION', value: awsRegion },
      { name: 'DATABASE_BACKEND', value: 'dynamodb' },
      { name: 'DYNAMODB_TABLE_PREFIX', value: dynamoDbTablePrefix },
      { name: 'DYNAMODB_REGION', value: awsRegion },
      { name: 'BEDROCK_MODEL_ID', value: bedrockModelId },
      { name: 'AGENT_MODEL_ID', value: agentModelId },
      { name: 'ENABLE_LONG_TERM_MEMORY', value: agentCoreMemoryId ? 'true' : 'false' },
      { name: 'S3_BUCKET_NAME', value: uploadBucket.bucketName },
      { name: 'BEDROCK_BATCH_ROLE_ARN', value: bedrockBatchRoleArn },
      { name: 'BATCH_INFERENCE_MODEL_ID', value: batchInferenceModelId },
      { name: 'SURVEY_S3_PREFIX', value: surveyS3Prefix },
      { name: 'BATCH_INFERENCE_S3_PREFIX', value: batchInferenceS3Prefix },
    ];
    if (agentCoreMemoryId) {
      environment.push({ name: 'AGENTCORE_MEMORY_ID', value: agentCoreMemoryId });
      environment.push({ name: 'AGENTCORE_MEMORY_REGION', value: awsRegion });
    }
    if (summaryMemoryStrategyId) environment.push({ name: 'SUMMARY_MEMORY_STRATEGY_ID', value: summaryMemoryStrategyId });
    if (semanticMemoryStrategyId) environment.push({ name: 'SEMANTIC_MEMORY_STRATEGY_ID', value: semanticMemoryStrategyId });

    // ECS Cluster
    const cluster = new ecs.Cluster(this, 'Cluster', {
      clusterName: `ai-persona-cluster-${envName}`,
      vpc,
    });

    // ECS Express Gateway Service — Private Subnet → Internal ALB (HTTPS auto-managed)
    this.service = new ecs.CfnExpressGatewayService(this, 'Default', {
      cluster: cluster.clusterName,
      serviceName: `ai-persona-${envName}`,
      executionRoleArn: executionRole.roleArn,
      infrastructureRoleArn: infrastructureRole.roleArn,
      taskRoleArn: taskRole.roleArn,
      cpu: containerCpu,
      memory: containerMemory,
      healthCheckPath: '/health',
      networkConfiguration: {
        subnets: vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds,
      },
      primaryContainer: {
        image: `${ecrRepository.repositoryUri}:${props.imageTag ?? 'latest'}`,
        containerPort: 80,
        environment,
      },
      scalingTarget: {
        minTaskCount: 1,
        maxTaskCount: 10,
      },
    });

    // Expose managed resource ARNs for manual Cognito auth setup
    this.endpoint = this.service.attrEndpoint;
    this.loadBalancerArn = this.service.attrEcsManagedResourceArnsIngressPathLoadBalancerArn;
    this.listenerArn = this.service.attrEcsManagedResourceArnsIngressPathListenerArn;
    this.certificateArn = this.service.attrEcsManagedResourceArnsIngressPathCertificateArn;
  }
}
