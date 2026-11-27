import { Construct, IConstruct } from 'constructs';
import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  AwsSdkCall,
  PhysicalResourceId,
} from 'aws-cdk-lib/custom-resources';
import { AppParameter } from '../parameters';
import { bedrockInferenceArns, surveyModelArns } from './bedrock-arns';
import { allowIngressFromCidrs, ClosedNetwork } from './constructs/closed-network';
import { CloudFrontDistribution } from './constructs/cloudfront';
import { AnalysisCodeInterpreter } from './constructs/code-interpreter';
import { PASSWORD_POLICY } from './constructs/cognito';
import { FargateAppService } from './constructs/fargate-service';
import { SurveyRunStateMachine } from './constructs/survey-run-state-machine';
import { ValidationEnvironment } from './constructs/validation-environment';
import { Vpc } from './constructs/vpc';
import { DataStack } from './data-stack';
import {
  AuthMode,
  CODE_INTERPRETER_EXECUTION_ROLE,
  ResolvedProfile,
  resolveDeploymentProfile,
} from './deployment-profile';
import { ecrLayerBucketArn, EndpointAccessScope, TASK_LIST_ACTIONS } from './endpoint-policies';
import { acknowledgeAll, iam5ExportedResourceId, iam5ResourceId, NAG_REASONS } from './nag-reasons';
import { resolveSurveyRunSettings, SurveyRunSettings } from './survey-run';

/**
 * AwsCustomResource's singleton Lambda construct ID. CDK builds it from the public constant
 * PROVIDER_FUNCTION_UUID with hyphens removed and 'AWS' prepended.
 */
const AWS_CUSTOM_RESOURCE_PROVIDER_ID = `AWS${AwsCustomResource.PROVIDER_FUNCTION_UUID.replace(/-/g, '')}`;

/**
 * Same actions grantReadWriteData adds to a table (read, write, DescribeTable). It is replaced by
 * explicit statements so that `<table ARN>/index/*` is not granted.
 */
const TABLE_READ_WRITE_ACTIONS = [
  'dynamodb:BatchGetItem',
  'dynamodb:Query',
  'dynamodb:GetItem',
  'dynamodb:Scan',
  'dynamodb:ConditionCheckItem',
  'dynamodb:GetRecords',
  'dynamodb:GetShardIterator',
  'dynamodb:BatchWriteItem',
  'dynamodb:PutItem',
  'dynamodb:UpdateItem',
  'dynamodb:DeleteItem',
  'dynamodb:DescribeTable',
];
/** Same actions grantReadWriteData adds to a customer-managed key (the Sessions key). */
const TABLE_KEY_READ_WRITE_ACTIONS = [
  'kms:Decrypt',
  'kms:DescribeKey',
  'kms:Encrypt',
  'kms:ReEncrypt*',
  'kms:GenerateDataKey*',
];

/** The CfnPolicy of a role's default policy (created by the first grant). */
function defaultPolicyOf(role: iam.IRole): IConstruct {
  const policy = role.node.tryFindChild('DefaultPolicy');
  if (!policy) {
    throw new Error(`${role.node.path} has no default policy to acknowledge cdk-nag findings on.`);
  }
  return policy;
}

/** Tag used when no imageTag is passed. deploy.sh stops before step 3 if it is still this value. */
export const UNSET_IMAGE_TAG = 'unset';

/** Metrics for authentication events (metric filters). */
const AUTH_METRICS: { name: string; event: string; dimensions?: Record<string, string> }[] = [
  { name: 'LoginFailed', event: 'auth.login_failed' },
  { name: 'SessionExpired', event: 'auth.session_expired' },
  { name: 'RefreshFailed', event: 'auth.refresh_failed', dimensions: { reason: '$.reason' } },
  { name: 'CsrfRejected', event: 'auth.csrf_rejected' },
  { name: 'CircuitOpened', event: 'auth.circuit_opened' },
  { name: 'DecryptFailed', event: 'auth.decrypt_failed' },
  // Authorization denials (not_found / denied / hidden / role_required). No alarm is created
  { name: 'AuthzDenied', event: 'authz.denied', dimensions: { reason: '$.reason' } },
];

/**
 * Metrics for advanced generation (beta) events. The event and status values match the app log
 * (the two events in src/managers/shared/advanced_generation_log.py).
 * Dimension values are enum values only (stage names and error codes). No alarms (public Stage only).
 */
const ADVANCED_GENERATION_METRICS: {
  name: string;
  event: string;
  status: string;
  dimensions?: Record<string, string>;
}[] = [
  { name: 'GenerationCompleted', event: 'advanced_generation.finished', status: 'done' },
  {
    name: 'GenerationFailed',
    event: 'advanced_generation.finished',
    status: 'failed',
    dimensions: { stage: '$.stage', error_code: '$.error_code' },
  },
  {
    name: 'UploadFailed',
    event: 'advanced_generation.upload_completed',
    status: 'error',
    dimensions: { error_code: '$.error_code' },
  },
  // Files that should have been deleted but remain in S3, and runs stopped by a reused copy
  {
    name: 'SourceDeleteFailed',
    event: 'advanced_generation.source_deleted',
    status: 'failed',
    dimensions: { error_code: '$.error_code' },
  },
  {
    name: 'SourceRefreshFailed',
    event: 'advanced_generation.source_refreshed',
    status: 'failed',
    dimensions: { error_code: '$.error_code' },
  },
  // Detailed analysis (run_code) disabled (5 failures, failed cancellation) and tampering detected.
  // The app log also carries status (disabled, detected) for these two. No alarms
  {
    name: 'RunCodeDisabled',
    event: 'advanced_generation.run_code_disabled',
    status: 'disabled',
    dimensions: { reason: '$.reason' },
  },
  { name: 'TamperDetected', event: 'advanced_generation.tamper_detected', status: 'detected' },
];

/** Local app origins added to the dev Stage bucket CORS. */
const LOCAL_APP_ORIGIN = 'http://localhost:8000';

const HTTPS = ec2.Port.tcp(443);

/** Lifetime of the Cognito app client tokens (same for public and closed). */
const TOKEN_VALIDITY = {
  // Revocation takes up to 61 minutes to propagate, and sessions last at most 7 days
  accessTokenValidity: Duration.minutes(60),
  idTokenValidity: Duration.minutes(60),
  refreshTokenValidity: Duration.days(7),
} as const;

/**
 * Password requirements passed to the closed-network app. Built from the User Pool policy
 * (PASSWORD_POLICY) so the values are not duplicated. The app reads them with
 * PasswordPolicy.from_settings in src/models/password_policy.py.
 */
const PASSWORD_POLICY_ENVIRONMENT: Record<string, string> = {
  PASSWORD_MIN_LENGTH: String(PASSWORD_POLICY.minLength),
  PASSWORD_REQUIRE_UPPERCASE: String(PASSWORD_POLICY.requireUppercase),
  PASSWORD_REQUIRE_LOWERCASE: String(PASSWORD_POLICY.requireLowercase),
  PASSWORD_REQUIRE_DIGITS: String(PASSWORD_POLICY.requireDigits),
  PASSWORD_REQUIRE_SYMBOLS: String(PASSWORD_POLICY.requireSymbols),
};

/** Environment variables whose values depend on the login mode (built by authEnvironment). */
interface AuthEnvironment {
  AUTH_MODE: AuthMode;
  /** oidc only (Managed Login domain). */
  COGNITO_DOMAIN?: string;
  /** admin_api only (the five password requirements). */
  passwordPolicy?: Record<string, string>;
}

/** App URL and Cognito app client, decided per entry point. */
interface AppEntry {
  appBaseUrl: string;
  client: cognito.UserPoolClient;
}

export interface AppStackProps extends StackProps {
  parameter: AppParameter;
  data: DataStack;
  /** ARN of the Edge stack WebACL (undefined when WAF is disabled; not passed in the closed network). */
  webAclArn?: string;
  /** Number of tasks (1 to 10, validated in the Stage). */
  appDesiredCount: number;
  /** Resolved Stage settings (public by default). */
  profile?: ResolvedProfile;
}

/**
 * App stack: everything that holds no state.
 *
 * VPC, internal ALB, ECS Fargate, CloudFront, the Cognito app client, and the app log group and
 * metric filters. References come from the Data and Edge stacks, and this stack can be destroyed
 * and recreated. The image uses the tag from the imageTag context (never latest).
 *
 * In closed mode (profile.deploymentMode=closed) it uses the ClosedNetwork VPC and an HTTPS-only
 * internal ALB, and creates no CloudFront or Managed Login. The app client is for admin API login
 * only, and SG wiring, the Private Hosted Zone Alias and the test EC2 are added.
 * The public template must not change (test/public-stage-invariants.test.ts).
 */
export class AppStack extends Stack {
  constructor(scope: Construct, id: string, props: AppStackProps) {
    super(scope, id, props);

    const { parameter, data, webAclArn, appDesiredCount } = props;
    // Without a profile (stacks created directly in tests), build a public profile from the parameters
    // so explicit model IDs and ranges are not silently dropped
    const profile =
      props.profile ??
      resolveDeploymentProfile({
        deploymentMode: 'public',
        region: this.region,
        inferenceScope: parameter.inferenceScope,
        bedrockModelId: parameter.bedrockModelId,
        agentModelId: parameter.agentModelId,
        surveyModelId: parameter.surveyModelId,
        enableAdditionalPersonaModels: parameter.enableAdditionalPersonaModels,
      });
    const closed = profile.closedNetwork;
    // Inference ARNs: the same list for the task role and the cdk-nag suppressions
    const inferenceArns = bedrockInferenceArns(this, profile);
    // Survey concurrency and timeout (synth fails when out of range)
    const surveySettings = resolveSurveyRunSettings(parameter);
    const imageTag = this.resolveImageTag();

    const network = closed
      ? new ClosedNetwork(this, 'Network', {
        envName: parameter.envName,
        vpcCidr: closed.vpcCidr,
        domainName: closed.domainName,
        createTestEnvironment: closed.createTestEnvironment,
        // Endpoint AZ count and Resolver settings
        endpointAzCount: closed.endpointAzCount,
        resolver: closed.resolver,
      })
      : undefined;
    const vpc = network ? network.vpc : new Vpc(this, 'Vpc', { envName: parameter.envName }).vpc;

    // App log (30 days in dev, RETAIN) and authentication event metrics
    const logGroup = new logs.LogGroup(this, 'AppLogGroup', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    for (const metric of AUTH_METRICS) {
      new logs.MetricFilter(this, `${metric.name}Filter`, {
        logGroup,
        metricNamespace: 'AIPersona/Auth',
        metricName: metric.name,
        filterPattern: logs.FilterPattern.stringValue('$.event', '=', metric.event),
        metricValue: '1',
        dimensions: metric.dimensions,
      });
    }

    // Survey state machine. Step Functions itself calls inference
    const { partition, account, region } = this;
    const surveyRun = new SurveyRunStateMachine(this, 'SurveyRun', {
      bucket: data.uploadBucket.bucket,
      modelArns: surveyModelArns({ partition, account, region }, profile.modelIds.surveyModelId),
      settings: surveySettings,
    });

    const app = new FargateAppService(this, 'App', {
      vpc,
      repository: data.ecr.repository,
      imageTag,
      cpu: Number(parameter.containerCpu),
      memoryLimitMiB: Number(parameter.containerMemory),
      desiredCount: appDesiredCount,
      logGroup,
      environment: {
        ...this.commonEnvironment(
          parameter,
          profile,
          data,
          { stateMachine: surveyRun, settings: surveySettings },
          this.authEnvironment(profile, data),
        ),
        // Appended at the end for closed tasks only, so the public environment variable order does not change
        ...(profile.closedFeatureEnvironment ?? {}),
        // Passed only for jp (the app default is global; keeps the public template unchanged)
        ...(profile.inferenceScope === 'jp' ? { INFERENCE_SCOPE: 'jp' } : {}),
      },
      secrets: {
        TOKEN_ENCRYPTION_KEYS: ecs.Secret.fromSecretsManager(data.tokenEncryptionKeys),
        LOGIN_COOKIE_SIGNING_KEY: ecs.Secret.fromSecretsManager(data.loginCookieSigningKey),
      },
      ...(network && closed
        ? {
          ingress: { kind: 'closed' as const, certificateArn: closed.certificateArn },
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
          // Tasks share the endpoint AZs; the internal ALB stays on both AZs
          serviceSubnets: network.placement.computeSubnets,
        }
        : {}),
    });

    const { appBaseUrl, client } = network
      ? this.createClosedEntry(app, network, profile, data)
      : this.createPublicEntry(app, data, webAclArn);
    const clientSecret = new secretsmanager.Secret(this, 'CognitoClientSecret', {
      description: 'AI Persona v1 Cognito app client secret',
      secretStringValue: client.userPoolClientSecret,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    app.container.addEnvironment('APP_BASE_URL', appBaseUrl);
    app.container.addEnvironment('COGNITO_CLIENT_ID', client.userPoolClientId);
    app.container.addSecret('COGNITO_CLIENT_SECRET', ecs.Secret.fromSecretsManager(clientSecret));

    this.grantTaskRole(app.taskDefinition.taskRole, parameter, data, surveyRun, profile, inferenceArns);
    // Advanced generation (beta) is not created in the closed Stage (the app stops on
    // ENABLE_ADVANCED_GENERATION=false from closedFeatureEnvironment)
    if (!network) {
      this.addAdvancedGeneration(app, logGroup, data, parameter, appBaseUrl);
    }
    // Attach endpoint policies after the resources they grant access to (log groups, secrets) exist,
    // using the same references as the task role
    if (network) {
      network.restrictEndpoints(
        this.endpointAccessScope(
          data,
          logGroup,
          [data.tokenEncryptionKeys, data.loginCookieSigningKey, clientSecret],
          inferenceArns,
          surveyRun,
        ),
      );
    }

    // cdk-nag suppressions; reasons are in nag-reasons.ts
    acknowledgeAll(clientSecret, ['SMG4'], NAG_REASONS.cognitoClientSecret);
    this.acknowledgeCustomResourceRole();
    this.acknowledgeIamWildcards(app, surveyRun, data, parameter, inferenceArns);

    new CfnOutput(this, 'AppUrl', { value: appBaseUrl });
    new CfnOutput(this, 'ClusterName', { value: app.cluster.clusterName });
    new CfnOutput(this, 'ServiceName', { value: app.service.serviceName });
    if (network?.resolverEndpoint) {
      // Find the IPs with: aws route53resolver list-resolver-endpoint-ip-addresses
      new CfnOutput(this, 'ResolverEndpointId', { value: network.resolverEndpoint.attrResolverEndpointId });
    }
  }

  /**
   * Resources of advanced generation (beta) that exist only in the public Stage:
   * the Code Interpreter with three task role actions and the app's CODE_INTERPRETER_ID, the bucket
   * CORS, and metric filters. The three tables, lifecycle rules and tag permissions are shared by both Stages.
   */
  private addAdvancedGeneration(
    app: FargateAppService,
    logGroup: logs.ILogGroup,
    data: DataStack,
    parameter: AppParameter,
    appBaseUrl: string,
  ): void {
    const codeInterpreter = new AnalysisCodeInterpreter(this, 'AnalysisCodeInterpreter', {
      envName: parameter.envName,
      executionRole: CODE_INTERPRETER_EXECUTION_ROLE,
    });
    app.container.addEnvironment('CODE_INTERPRETER_ID', codeInterpreter.codeInterpreterId);
    // Starting, invoking and stopping sessions is allowed on this Code Interpreter only
    app.taskDefinition.taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:StartCodeInterpreterSession',
        'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession',
      ],
      resources: [codeInterpreter.codeInterpreterArn],
    }));

    this.addUploadBucketCors(data.uploadBucket.bucket, [
      appBaseUrl,
      ...(parameter.envName === 'dev' ? [LOCAL_APP_ORIGIN] : []),
    ]);

    for (const metric of ADVANCED_GENERATION_METRICS) {
      new logs.MetricFilter(this, `${metric.name}Filter`, {
        logGroup,
        metricNamespace: 'AIPersona/AdvancedGeneration',
        metricName: metric.name,
        filterPattern: logs.FilterPattern.all(
          logs.FilterPattern.stringValue('$.event', '=', metric.event),
          logs.FilterPattern.stringValue('$.status', '=', metric.status),
        ),
        metricValue: '1',
        dimensions: metric.dimensions,
      });
    }
  }

  /**
   * CORS for browser PUTs of upload parts (see also the Edge origin).
   *
   * The bucket is in the Data stack, but the origin (the CloudFront domain) is decided in the App stack,
   * so a custom resource in the App stack sets it with PutBucketCors (no Data to App reference).
   * Only PUT is allowed, and the part ETag is exposed to the page. CORS is not authorization; signed
   * URLs protect writes. Deleting removes only the CORS configuration. The custom resource Lambda is
   * a per-stack singleton (shared with the app client secret lookup), and this resource only needs
   * s3:PutBucketCORS on this bucket (the DeleteBucketCors IAM action is also s3:PutBucketCORS).
   */
  private addUploadBucketCors(bucket: s3.IBucket, origins: string[]): void {
    const physicalResourceId = PhysicalResourceId.of('UploadBucketCors');
    const putCors: AwsSdkCall = {
      service: 'S3',
      action: 'putBucketCors',
      parameters: {
        Bucket: bucket.bucketName,
        CORSConfiguration: {
          CORSRules: [
            {
              AllowedMethods: ['PUT'],
              AllowedOrigins: origins,
              AllowedHeaders: ['content-type'],
              ExposeHeaders: ['ETag'],
              MaxAgeSeconds: 3000,
            },
          ],
        },
      },
      physicalResourceId,
    };
    new AwsCustomResource(this, 'UploadBucketCors', {
      resourceType: 'Custom::UploadBucketCors',
      onCreate: putCors,
      onUpdate: putCors,
      onDelete: {
        service: 'S3',
        action: 'deleteBucketCors',
        parameters: { Bucket: bucket.bucketName },
        physicalResourceId,
      },
      policy: AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({ actions: ['s3:PutBucketCORS'], resources: [bucket.bucketArn] }),
      ]),
      installLatestAwsSdk: false,
    });
  }

  /**
   * All tables the task role reads and writes. Used as the access targets of the endpoint policies.
   * The grantTaskRole statements differ per table, so they are listed as they are
   * (add a table to both; a miss fails the test in test/closed-stage.test.ts).
   */
  private taskTables(data: DataStack): dynamodb.ITableV2[] {
    const { database } = data;
    return [
      ...Object.values(database.applicationTables),
      database.sessionsTable,
      database.resourceSharesTable,
      database.personaCandidatesTable,
      database.personaBindingsTable,
      ...Object.values(database.discussionChildTables),
      database.customPersonaDataTable,
      database.surveyRunLeaseTable,
    ];
  }

  /**
   * Access targets allowed by the closed endpoint policies (EndpointAccessScope).
   * ARNs are not written by hand; they come from the same references as the task and execution role permissions.
   */
  private endpointAccessScope(
    data: DataStack,
    logGroup: logs.ILogGroup,
    secrets: secretsmanager.ISecret[],
    inferenceArns: string[],
    surveyRun: SurveyRunStateMachine,
  ): EndpointAccessScope {
    const { partition, account, region } = this;
    return {
      bucketArn: data.uploadBucket.bucket.bucketArn,
      ecrLayerBucketArn: ecrLayerBucketArn(partition, region),
      tableArns: this.taskTables(data).map((table) => table.tableArn),
      userPoolArn: data.cognito.userPool.userPoolArn,
      secretArns: secrets.map((secret) => secret.secretArn),
      // Log group ARN (CreateLogStream) and the CDK logGroupArn (<ARN>:*, which includes log streams)
      logGroupArns: [`arn:${partition}:logs:${region}:${account}:log-group:${logGroup.logGroupName}`, logGroup.logGroupArn],
      repositoryArn: data.ecr.repository.repositoryArn,
      memoryArn: data.memory.memoryArn,
      inferenceArns,
      knowledgeBaseArn: `arn:${partition}:bedrock:${region}:${account}:knowledge-base/*`,
      // Same reference as the task role states statement (grantTaskRole)
      stateMachineArn: surveyRun.stateMachine.stateMachineArn,
      stateMachineExecutionsArn: surveyRun.executionsArn,
    };
  }

  /** Public entry: CloudFront (VPC origin) and the Managed Login app client. */
  private createPublicEntry(app: FargateAppService, data: DataStack, webAclArn?: string): AppEntry {
    const cdn = new CloudFrontDistribution(this, 'CloudFront', {
      loadBalancer: app.loadBalancer,
      webAclArn,
    });
    return {
      appBaseUrl: `https://${cdn.domainName}`,
      client: this.createOidcClient(data, cdn.domainName),
    };
  }

  /**
   * Closed entry: the app client for admin API login, the test EC2 (only when requested), SG wiring,
   * and the Private Hosted Zone apex Alias to the ALB. No CloudFront is created.
   */
  private createClosedEntry(
    app: FargateAppService,
    network: ClosedNetwork,
    profile: ResolvedProfile,
    data: DataStack,
  ): AppEntry {
    const { closedNetwork: closed, appBaseUrl } = profile;
    if (!closed || !appBaseUrl) {
      throw new Error('The closed profile has no network settings or APP_BASE_URL.');
    }
    const client = this.createAdminApiClient(data);
    // The test EC2 is also placed in the endpoint AZs
    const validation = closed.createTestEnvironment
      ? new ValidationEnvironment(this, 'Validation', { vpc: network.vpc, subnets: network.placement.computeSubnets })
      : undefined;
    this.wireClosedSecurityGroups(app, network, closed.allowedCidrs, validation);
    // The app calls GetMemory once at startup and keeps running with long-term memory disabled if it fails.
    // Create the service after the endpoint and task SG rules exist (CloudFormation creates
    // independent resources in parallel).
    app.service.node.addDependency(network, app.taskSecurityGroup);
    // Point the FQDN (zone apex) at the internal ALB
    new route53.ARecord(this, 'AppAlias', {
      zone: network.hostedZone,
      target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(app.loadBalancer)),
    });
    return { appBaseUrl, client };
  }

  /**
   * SG wiring for the closed network.
   *
   * Port 80 from the ALB to the tasks is added on both sides by FargateAppService (listener.addTargets).
   * This adds 443 from the clients to the ALB, 443 from the tasks and test EC2 to the endpoints, and
   * 443 from the tasks to the S3 and DynamoDB gateway endpoints (prefix lists). Rules between SGs are
   * separate resources (SecurityGroupIngress / Egress), so there is no cycle.
   */
  private wireClosedSecurityGroups(
    app: FargateAppService,
    network: ClosedNetwork,
    allowedCidrs: readonly string[],
    validation?: ValidationEnvironment,
  ): void {
    const alb = app.loadBalancerSecurityGroup;
    const task = app.taskSecurityGroup;
    const endpoints = network.endpointSecurityGroup;
    const allowHttps = (from: ec2.ISecurityGroup, to: ec2.ISecurityGroup, description: string) => {
      from.addEgressRule(to, HTTPS, description);
      to.addIngressRule(from, HTTPS, description);
    };

    allowIngressFromCidrs(alb, allowedCidrs, [HTTPS], (cidr) => `Allowed client ${cidr}`);
    allowHttps(task, endpoints, 'App tasks to VPC interface endpoints');
    task.addEgressRule(ec2.Peer.prefixList(network.s3PrefixListId), HTTPS, 'S3 gateway endpoint');
    task.addEgressRule(
      ec2.Peer.prefixList(network.dynamoDbPrefixListId),
      HTTPS,
      'DynamoDB gateway endpoint',
    );
    if (validation) {
      allowHttps(validation.securityGroup, alb, 'Validation instance to the app load balancer');
      allowHttps(validation.securityGroup, endpoints, 'Validation instance to VPC interface endpoints');
    }
  }

  /**
   * IAM4: role of the AwsCustomResource Lambda created by client.userPoolClientSecret (a per-stack
   * singleton). CDK creates it internally.
   */
  private acknowledgeCustomResourceRole(): void {
    const provider = this.node.tryFindChild(AWS_CUSTOM_RESOURCE_PROVIDER_ID);
    const role = provider?.node.tryFindChild('ServiceRole');
    if (!role) {
      throw new Error(
        `The AwsCustomResource provider ${AWS_CUSTOM_RESOURCE_PROVIDER_ID} was not found; ` +
          'update the cdk-nag acknowledgement for its role.',
      );
    }
    acknowledgeAll(
      role,
      ['IAM4[Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole]'],
      NAG_REASONS.cdkCustomResourceManagedPolicy,
    );
  }

  /**
   * Attach IAM5 (wildcard) suppressions per role and per finding.
   *
   * For ARNs received from the Data stack (bucket, Memory), the finding ID contains an output name
   * CDK derives from the logical ID. That name is not written in code; iam5ExportedResourceId resolves
   * it from the reference. Table GSIs are granted by name, so no `/index/*` finding appears. When adding
   * a bucket prefix, update this and test/cdk-nag.test.ts together.
   */
  private acknowledgeIamWildcards(
    app: FargateAppService,
    surveyRun: SurveyRunStateMachine,
    data: DataStack,
    parameter: AppParameter,
    inferenceArns: string[],
  ): void {
    const bucketObjects = (...suffixes: string[]) =>
      suffixes.map((suffix) => iam5ExportedResourceId(data, data.uploadBucket.bucket.bucketArn, suffix));
    // Only ARNs containing `*` become IAM5 findings (an explicit model ID ARN may have no `*`)
    const inference = inferenceArns.filter((arn) => arn.includes('*')).map((arn) => iam5ResourceId(this, arn));

    const taskPolicy = defaultPolicyOf(app.taskDefinition.taskRole);
    acknowledgeAll(taskPolicy, ['IAM5[Resource::*]'], NAG_REASONS.wildcardListApis);
    acknowledgeAll(
      taskPolicy,
      bucketObjects('/*', '/uploads/*', '/candidates/*', '/analysis-records/*', '/sources/*'),
      NAG_REASONS.bucketObjects,
    );
    acknowledgeAll(taskPolicy, inference, NAG_REASONS.bedrockInference);
    acknowledgeAll(
      taskPolicy,
      [iam5ResourceId(this, `arn:${this.partition}:bedrock:${this.region}:${this.account}:knowledge-base/*`)],
      NAG_REASONS.bedrockAccountResources,
    );
    // Survey runs (start, check, stop) are limited to executions of this state machine
    acknowledgeAll(taskPolicy, [iam5ResourceId(this, surveyRun.executionsArn)], NAG_REASONS.surveyExecutions);
    acknowledgeAll(
      taskPolicy,
      [iam5ExportedResourceId(data, data.memory.memoryArn, '/*')],
      NAG_REASONS.memoryChildren,
    );
    acknowledgeAll(
      taskPolicy,
      ['IAM5[Action::kms:GenerateDataKey*]', 'IAM5[Action::kms:ReEncrypt*]'],
      NAG_REASONS.kmsDataKeyActions,
    );
    if (parameter.enableAdditionalPersonaModels) {
      acknowledgeAll(
        taskPolicy,
        [iam5ResourceId(this, `arn:${this.partition}:bedrock-mantle:*:${this.account}:project/*`)],
        NAG_REASONS.mantleProject,
      );
    }

    const executionRole = app.taskDefinition.executionRole;
    if (!executionRole) {
      throw new Error('The task definition has no execution role to acknowledge.');
    }
    acknowledgeAll(defaultPolicyOf(executionRole), ['IAM5[Resource::*]'], NAG_REASONS.wildcardListApis);

    this.acknowledgeSurveyRun(surveyRun, bucketObjects);
  }

  /** The survey state machine's cdk-nag findings. */
  private acknowledgeSurveyRun(
    surveyRun: SurveyRunStateMachine,
    bucketObjects: (...suffixes: string[]) => string[],
  ): void {
    acknowledgeAll(surveyRun.stateMachine, ['SF1'], NAG_REASONS.surveyStateMachineLogs);
    acknowledgeAll(surveyRun.stateMachine, ['SF2'], NAG_REASONS.surveyStateMachineTracing);
    const rolePolicy = defaultPolicyOf(surveyRun.role);
    acknowledgeAll(rolePolicy, ['IAM5[Resource::*]'], NAG_REASONS.logDeliveryApis);
    acknowledgeAll(
      rolePolicy,
      bucketObjects('/survey-runs/*', '/survey-runs/*/results/*'),
      NAG_REASONS.surveyRunObjects,
    );
    // Only ARNs containing `*` become IAM5 findings (the jp profile ARNs have no `*`)
    const modelArns = surveyRun.modelArns.filter((arn) => arn.includes('*'));
    acknowledgeAll(rolePolicy, modelArns.map((arn) => iam5ResourceId(this, arn)), NAG_REASONS.surveyModel);
    acknowledgeAll(
      surveyRun.executionPolicy,
      [iam5ResourceId(this, surveyRun.childExecutionsArn)],
      NAG_REASONS.surveyMapRuns,
    );
  }

  private resolveImageTag(): string {
    const tag = this.node.tryGetContext('imageTag') as string | undefined;
    if (tag) {
      return tag;
    }
    // Not an error, so synth, cdk diff and Data / Edge-only deploys are not blocked
    Annotations.of(this).addWarning(
      'imageTag context is not set; the App stack uses the placeholder tag "unset". ' +
        'Deploy the App stack only through deploy.sh (it passes -c imageTag=<git short hash>).',
    );
    return UNSET_IMAGE_TAG;
  }

  /**
   * Environment variables whose values depend on the login mode. Branch on the mode before reading
   * domainHost (the closed User Pool has no domain, and reading it throws).
   */
  private authEnvironment(profile: ResolvedProfile, data: DataStack): AuthEnvironment {
    if (profile.authMode === 'admin_api') {
      return { AUTH_MODE: 'admin_api', passwordPolicy: PASSWORD_POLICY_ENVIRONMENT };
    }
    return { AUTH_MODE: 'oidc', COGNITO_DOMAIN: data.cognito.domainHost };
  }

  /**
   * Task environment variables. The login-mode values (auth) keep their positions: AUTH_MODE first,
   * COGNITO_DOMAIN right after COGNITO_USER_POOL_ID, password requirements last. The order appears in
   * the task definition template, so it is kept to leave the public template unchanged.
   */
  private commonEnvironment(
    parameter: AppParameter,
    profile: ResolvedProfile,
    data: DataStack,
    survey: { stateMachine: SurveyRunStateMachine; settings: SurveyRunSettings },
    auth: AuthEnvironment,
  ): Record<string, string> {
    const region = this.region;
    const tableNames = Object.fromEntries(
      Object.entries(data.database.applicationTables).map(([name, table]) => [name, table.tableName]),
    );
    const environment: Record<string, string> = {
      // Login mode (decided by DeploymentProfile: oidc for public, admin_api for closed)
      AUTH_MODE: auth.AUTH_MODE,
      AWS_REGION: region,
      DYNAMODB_REGION: region,
      COGNITO_USER_POOL_ID: data.cognito.userPool.userPoolId,
      ...(auth.COGNITO_DOMAIN !== undefined ? { COGNITO_DOMAIN: auth.COGNITO_DOMAIN } : {}),
      SESSIONS_TABLE_NAME: data.database.sessionsTable.tableName,
      // Per-department sharing index (src/config.py RESOURCE_SHARES_TABLE_NAME)
      RESOURCE_SHARES_TABLE_NAME: data.database.resourceSharesTable.tableName,
      // Generation candidates and bindings (src/config.py PERSONA_CANDIDATES / PERSONA_BINDINGS)
      PERSONA_CANDIDATES_TABLE_NAME: data.database.personaCandidatesTable.tableName,
      PERSONA_BINDINGS_TABLE_NAME: data.database.personaBindingsTable.tableName,
      // Discussion messages, reports and interviews (src/config.py DISCUSSION_* / INTERVIEW_*)
      ...Object.fromEntries(
        Object.entries(data.database.discussionChildTables).map(([name, table]) => [
          name,
          table.tableName,
        ]),
      ),
      // Custom persona data registry (src/config.py CUSTOM_PERSONA_DATA_TABLE_NAME)
      CUSTOM_PERSONA_DATA_TABLE_NAME: data.database.customPersonaDataTable.tableName,
      ...tableNames,
      S3_BUCKET_NAME: data.uploadBucket.bucket.bucketName,
      // The app looks up the Strategy ID with GetMemory at startup (do not copy it by hand)
      ENABLE_LONG_TERM_MEMORY: 'true',
      AGENTCORE_MEMORY_ID: data.memory.memoryId,
      AGENTCORE_MEMORY_REGION: region,
      // The three model IDs come from ResolvedProfile (explicit values or the range defaults)
      BEDROCK_MODEL_ID: profile.modelIds.bedrockModelId,
      AGENT_MODEL_ID: profile.modelIds.agentModelId,
      ENABLE_ADDITIONAL_PERSONA_MODELS: String(parameter.enableAdditionalPersonaModels ?? false),
      // Optional, so default to true when unset. Avoids passing String(undefined) as "undefined"
      ENABLE_DATASET_ANALYSIS: String(parameter.enableDatasetAnalysis ?? true),
      // Kill switch for advanced generation (beta). The closed Stage overrides it with false from closedFeatureEnvironment
      ENABLE_ADVANCED_GENERATION: String(parameter.enableAdvancedGeneration ?? true),
      SURVEY_S3_PREFIX: parameter.surveyS3Prefix ?? 'survey-results/',
      // Survey runtime (src/config.py SURVEY_*). The timeout is the same value as the state machine
      SURVEY_STATE_MACHINE_ARN: survey.stateMachine.stateMachine.stateMachineArn,
      SURVEY_RUN_LEASE_TABLE_NAME: data.database.surveyRunLeaseTable.tableName,
      SURVEY_EXECUTION_TIMEOUT_SECONDS: String(survey.settings.timeoutSeconds),
      SURVEY_MODEL_ID: profile.modelIds.surveyModelId,
      ...(auth.passwordPolicy ?? {}),
    };
    if (parameter.dataAgentRuntimeArn) {
      environment.ENABLE_DATA_AGENT = 'true';
      environment.DATA_AGENT_RUNTIME_ARN = parameter.dataAgentRuntimeArn;
      environment.DATA_AGENT_REGION = parameter.dataAgentRegion ?? region;
    }
    return environment;
  }

  /** Public app client: logs in through Managed Login (OIDC authorization code). */
  private createOidcClient(data: DataStack, domainName: string): cognito.UserPoolClient {
    const client = new cognito.UserPoolClient(this, 'AppClient', {
      userPool: data.cognito.userPool,
      generateSecret: true,
      authFlows: {},
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: [`https://${domainName}/auth/callback`],
        logoutUrls: [`https://${domainName}/login`],
      },
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      ...TOKEN_VALIDITY,
      enableTokenRevocation: true,
      preventUserExistenceErrors: true,
    });
    // Managed Login branding (the Cognito default look).
    // It is a per-client setting, so it lives in the same App stack as the client.
    new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: data.cognito.userPool.userPoolId,
      clientId: client.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    return client;
  }

  /**
   * Closed app client: the app logs in through the admin API (AdminInitiateAuth). The only auth flow is
   * ADMIN_USER_PASSWORD_AUTH (CDK adds REFRESH_TOKEN_AUTH). OAuth is disabled and no Managed Login
   * branding is created. Secret, token revocation and token lifetimes are the same as public.
   */
  private createAdminApiClient(data: DataStack): cognito.UserPoolClient {
    return new cognito.UserPoolClient(this, 'AppClient', {
      userPool: data.cognito.userPool,
      generateSecret: true,
      authFlows: { adminUserPassword: true },
      disableOAuth: true,
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
      ...TOKEN_VALIDITY,
      enableTokenRevocation: true,
      preventUserExistenceErrors: true,
    });
  }

  /**
   * Permissions on the upload bucket.
   *
   * grantReadWrite is replaced by explicit statements, and reading old versions (GetObjectVersion*) is
   * not granted. Deleted objects remain as old versions for 7 days but the app cannot read them.
   * To purge content immediately when a user deletes a file, only s3:DeleteObjectVersion on sources/*
   * and a conditional s3:ListBucketVersions limited to the sources/ prefix are added (versions under
   * other prefixes are untouched). CopyObject back onto the same key at generation start needs only
   * s3:GetObject and s3:PutObject. Tagging actions apply only to uploads/* and candidates/* (binding
   * marks) and analysis-records/* (pre-save analysis records of advanced generation, written with tag
   * pending=true and copied untagged on save). Multipart upload (CreateMultipartUpload, UploadPart,
   * CompleteMultipartUpload) is covered by s3:PutObject, and ListParts by s3:ListMultipartUploadParts.
   */
  private grantUploadBucket(taskRole: iam.IRole, bucket: s3.IBucket): void {
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        's3:GetObject',
        's3:PutObject',
        's3:DeleteObject',
        's3:AbortMultipartUpload',
        's3:ListMultipartUploadParts',
      ],
      resources: [bucket.arnForObjects('*')],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObjectTagging', 's3:PutObjectTagging', 's3:DeleteObjectTagging'],
      resources: [
        bucket.arnForObjects('uploads/*'),
        bucket.arnForObjects('candidates/*'),
        bucket.arnForObjects('analysis-records/*'),
      ],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket', 's3:ListBucketMultipartUploads'],
      resources: [bucket.bucketArn],
    }));
    // Delete all versions of an uploaded file (S3Service.delete_all_versions)
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:DeleteObjectVersion'],
      resources: [bucket.arnForObjects('sources/*')],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucketVersions'],
      resources: [bucket.bucketArn],
      conditions: { StringLike: { 's3:prefix': 'sources/*' } },
    }));
  }

  private grantTaskRole(
    taskRole: iam.IRole,
    parameter: AppParameter,
    data: DataStack,
    surveyRun: SurveyRunStateMachine,
    profile: ResolvedProfile,
    inferenceArns: string[],
  ): void {
    // Table reads and writes are limited to the table ARN and the ARNs of the GSIs the app queries (by name).
    // Same actions as grantReadWriteData. Sessions also gets the customer-managed key permissions
    const { database } = data;
    const readWriteTables = [...Object.values(database.applicationTables), database.sessionsTable];
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: TABLE_READ_WRITE_ACTIONS,
      resources: readWriteTables.flatMap((table) => [
        table.tableArn,
        ...database.queriedIndexArns(table),
      ]),
    }));
    for (const table of readWriteTables) {
      table.encryptionKey?.grant(taskRole, ...TABLE_KEY_READ_WRITE_ACTIONS);
    }
    // Only list APIs that cannot take a resource are granted with * (exceptions are listed in
    // test/iam-wildcards.test.ts). DatabaseService checks the connection with ListTables at startup.
    // The closed endpoint policies use the same constant
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [...TASK_LIST_ACTIONS],
      resources: ['*'],
    }));
    // Per-department sharing index. No Scan (member listing does not scan).
    // Transactions run with the permissions of these actions
    data.database.resourceSharesTable.grant(
      taskRole,
      'dynamodb:GetItem',
      'dynamodb:BatchGetItem',
      'dynamodb:Query',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
      'dynamodb:ConditionCheckItem',
    );
    // Generation candidates. Claims use a conditional UpdateItem. No Scan or Query
    data.database.personaCandidatesTable.grant(
      taskRole,
      'dynamodb:GetItem',
      'dynamodb:BatchGetItem',
      'dynamodb:PutItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
    );
    // Bindings. Seven actions except Scan on the table and TargetIndex (by name)
    const bindings = data.database.personaBindingsTable;
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:BatchGetItem',
        'dynamodb:Query',
        'dynamodb:PutItem',
        'dynamodb:BatchWriteItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
      ],
      resources: [bindings.tableArn, ...data.database.queriedIndexArns(bindings)],
    }));
    // Discussion messages, reports and interviews. Eight actions except Scan in one statement listing the
    // four tables (no GSIs, so no index ARNs). The policy size limit (10,240 chars) is close, so do not
    // add a statement per table. Cleanup scripts do not run with the task role
    const childTables = Object.values(data.database.discussionChildTables);
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:BatchGetItem',
        'dynamodb:Query',
        'dynamodb:PutItem',
        'dynamodb:BatchWriteItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:ConditionCheckItem',
      ],
      resources: childTables.flatMap((table) => [
        table.tableArn,
        ...data.database.queriedIndexArns(table),
      ]),
    }));
    // Custom persona data registry. Name claims use a transaction (ConditionCheckItem) and listing is a
    // small Scan. There is no GSI, so no Query or index access
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:GetItem',
        'dynamodb:BatchGetItem',
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:DeleteItem',
        'dynamodb:ConditionCheckItem',
        'dynamodb:Scan',
      ],
      resources: [data.database.customPersonaDataTable.tableArn],
    }));
    this.grantUploadBucket(taskRole, data.uploadBucket.bucket);
    // Department listing (DepartmentDirectory) and the owner name copy (OwnerDirectory): only
    // ListGroups and ListUsers on the v1 User Pool
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListGroups', 'cognito-idp:ListUsers'],
      resources: [data.cognito.userPool.userPoolArn],
    }));
    // Closed-network admin API login, on this Stage's User Pool only.
    // Not granted for public (oidc)
    if (profile.authMode === 'admin_api') {
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['cognito-idp:AdminInitiateAuth', 'cognito-idp:AdminRespondToAuthChallenge'],
        resources: [data.cognito.userPool.userPoolArn],
      }));
    }
    // Bedrock: same actions as v0, with resources narrowed per type.
    // minimizePolicies merges statements with the same resource, so keep one statement per type
    const { partition, account } = Stack.of(this);
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream',
        'bedrock:GetFoundationModel', 'bedrock:GetInferenceProfile',
      ],
      resources: inferenceArns,
    }));
    // Knowledge base retrieval is limited to KBs in this account and region
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:Retrieve'],
      resources: [`arn:${partition}:bedrock:${this.region}:${account}:knowledge-base/*`],
    }));
    // Survey runs: start only on this state machine, check and stop only on its executions.
    // No batch inference job actions and no iam:PassRole
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['states:StartExecution'],
      resources: [surveyRun.stateMachine.stateMachineArn],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['states:DescribeExecution', 'states:StopExecution'],
      resources: [surveyRun.executionsArn],
    }));
    // Facilitator assignment record: only four conditional get, extend and release actions
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem'],
      resources: [data.database.surveyRunLeaseTable.tableArn],
    }));
    // AgentCore Memory: this Stage's Memory only (ListMemories is in the list statement above).
    // <ARN>/* is also listed for child actions such as ListEvents
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:GetMemory',
        'bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents', 'bedrock-agentcore:GetEvent',
        'bedrock-agentcore:CreateMemoryRecord', 'bedrock-agentcore:BatchCreateMemoryRecords',
        'bedrock-agentcore:ListMemoryRecords', 'bedrock-agentcore:GetMemoryRecord',
        'bedrock-agentcore:DeleteMemoryRecord', 'bedrock-agentcore:SearchMemoryRecords',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:InvokeMemory', 'bedrock-agentcore:RetrieveMemory',
      ],
      resources: [data.memory.memoryArn, `${data.memory.memoryArn}/*`],
    }));
    if (parameter.dataAgentRuntimeArn) {
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [parameter.dataAgentRuntimeArn, `${parameter.dataAgentRuntimeArn}/*`],
      }));
    }
    if (parameter.enableAdditionalPersonaModels) {
      // Additional persona base models (non-Anthropic models via Bedrock Mantle). CreateInference is limited
      // to this account's project. The region is * because the app's resolve_call_region sends unsupported
      // regions to us-east-1, so the stack region and the call region differ
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:CreateInference'],
        resources: [`arn:${partition}:bedrock-mantle:*:${account}:project/*`],
      }));
      // CallWithBearerToken has no resource type, so it stays * (exception)
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:CallWithBearerToken'],
        resources: ['*'],
      }));
    }
  }
}
