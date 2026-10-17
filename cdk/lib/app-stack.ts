import { Construct } from 'constructs';
import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { AppParameter } from '../parameters';
import { BedrockBatchRole } from './constructs/bedrock-batch-role';
import { CloudFrontDistribution } from './constructs/cloudfront';
import { FargateAppService } from './constructs/fargate-service';
import { Vpc } from './constructs/vpc';
import { DataStack } from './data-stack';

/** imageTag が渡されなかったときのタグ。deploy.sh は ③ の前にこの値なら止める（R-09）。 */
export const UNSET_IMAGE_TAG = 'unset';

/** 認証の出来事の件数のメトリクス（infrastructure-specification.md「メトリクスフィルタ」）。 */
const AUTH_METRICS: { name: string; event: string; dimensions?: Record<string, string> }[] = [
  { name: 'LoginFailed', event: 'auth.login_failed' },
  { name: 'SessionExpired', event: 'auth.session_expired' },
  { name: 'RefreshFailed', event: 'auth.refresh_failed', dimensions: { reason: '$.reason' } },
  { name: 'CsrfRejected', event: 'auth.csrf_rejected' },
  { name: 'CircuitOpened', event: 'auth.circuit_opened' },
  { name: 'DecryptFailed', event: 'auth.decrypt_failed' },
  // U2: 認可で拒否した件数（not_found / denied / hidden / role_required）。アラームは作らない
  { name: 'AuthzDenied', event: 'authz.denied', dimensions: { reason: '$.reason' } },
];

export interface AppStackProps extends StackProps {
  parameter: AppParameter;
  data: DataStack;
  /** Edge スタックの WebACL の ARN（WAF が無効なら undefined）。 */
  webAclArn?: string;
  /** タスクの台数（1〜10。Stage で検査する）。 */
  appDesiredCount: number;
}

/**
 * App スタック: 状態を持たないもの（FR9.1、FR9.3、ADR-001）。
 *
 * VPC・内部 ALB・ECS Fargate・CloudFront・Cognito のアプリクライアント・
 * アプリのロググループとメトリクスフィルタ。Data・Edge から参照で受け取り、
 * 壊して作り直せる。イメージはコンテキスト imageTag のタグを使う（latest は使わない）。
 */
export class AppStack extends Stack {
  constructor(scope: Construct, id: string, props: AppStackProps) {
    super(scope, id, props);

    const { parameter, data, webAclArn, appDesiredCount } = props;
    const imageTag = this.resolveImageTag();

    const { vpc } = new Vpc(this, 'Vpc', { envName: parameter.envName });

    // アプリのログ（NFR8.4: dev で 30 日、RETAIN）と認証の出来事のメトリクス（NFR8.3）
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

    const batchRole = new BedrockBatchRole(this, 'BedrockBatchRole', {
      bucket: data.uploadBucket.bucket,
    });

    const app = new FargateAppService(this, 'App', {
      vpc,
      repository: data.ecr.repository,
      imageTag,
      cpu: Number(parameter.containerCpu),
      memoryLimitMiB: Number(parameter.containerMemory),
      desiredCount: appDesiredCount,
      logGroup,
      environment: this.baseEnvironment(parameter, data, batchRole.role.roleArn),
      secrets: {
        TOKEN_ENCRYPTION_KEYS: ecs.Secret.fromSecretsManager(data.tokenEncryptionKeys),
        LOGIN_COOKIE_SIGNING_KEY: ecs.Secret.fromSecretsManager(data.loginCookieSigningKey),
      },
    });

    const cdn = new CloudFrontDistribution(this, 'CloudFront', {
      loadBalancer: app.loadBalancer,
      webAclArn,
    });
    const appBaseUrl = `https://${cdn.domainName}`;

    const client = this.createAppClient(data, cdn.domainName);
    const clientSecret = new secretsmanager.Secret(this, 'CognitoClientSecret', {
      description: 'AI Persona v1 Cognito app client secret',
      secretStringValue: client.userPoolClientSecret,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    app.container.addEnvironment('APP_BASE_URL', appBaseUrl);
    app.container.addEnvironment('COGNITO_CLIENT_ID', client.userPoolClientId);
    app.container.addSecret('COGNITO_CLIENT_SECRET', ecs.Secret.fromSecretsManager(clientSecret));

    this.grantTaskRole(app.taskDefinition.taskRole, parameter, data, batchRole.role.roleArn);

    new CfnOutput(this, 'AppUrl', { value: appBaseUrl });
    new CfnOutput(this, 'ClusterName', { value: app.cluster.clusterName });
    new CfnOutput(this, 'ServiceName', { value: app.service.serviceName });
  }

  private resolveImageTag(): string {
    const tag = this.node.tryGetContext('imageTag') as string | undefined;
    if (tag) {
      return tag;
    }
    // synth・cdk diff・Data / Edge だけのデプロイを止めないため、エラーにしない（R-09）
    Annotations.of(this).addWarning(
      'imageTag context is not set; the App stack uses the placeholder tag "unset". ' +
        'Deploy the App stack only through deploy.sh (it passes -c imageTag=<git short hash>).',
    );
    return UNSET_IMAGE_TAG;
  }

  private baseEnvironment(
    parameter: AppParameter,
    data: DataStack,
    batchRoleArn: string,
  ): Record<string, string> {
    const region = this.region;
    const tableNames = Object.fromEntries(
      Object.entries(data.database.applicationTables).map(([name, table]) => [name, table.tableName]),
    );
    const environment: Record<string, string> = {
      // deploymentMode=public のログイン方式（closed は Stage で止める）
      AUTH_MODE: 'oidc',
      AWS_REGION: region,
      DYNAMODB_REGION: region,
      COGNITO_USER_POOL_ID: data.cognito.userPool.userPoolId,
      COGNITO_DOMAIN: data.cognito.domainHost,
      SESSIONS_TABLE_NAME: data.database.sessionsTable.tableName,
      // 部署ごとの共有の索引（U4、src/config.py の RESOURCE_SHARES_TABLE_NAME）
      RESOURCE_SHARES_TABLE_NAME: data.database.resourceSharesTable.tableName,
      // 生成候補と紐付け（U5、src/config.py の PERSONA_CANDIDATES / PERSONA_BINDINGS）
      PERSONA_CANDIDATES_TABLE_NAME: data.database.personaCandidatesTable.tableName,
      PERSONA_BINDINGS_TABLE_NAME: data.database.personaBindingsTable.tableName,
      // 議論の発言・レポートとインタビュー（U6、src/config.py の DISCUSSION_* / INTERVIEW_*）
      ...Object.fromEntries(
        Object.entries(data.database.discussionChildTables).map(([name, table]) => [
          name,
          table.tableName,
        ]),
      ),
      // カスタムペルソナデータの登録情報（U7、src/config.py の CUSTOM_PERSONA_DATA_TABLE_NAME）
      CUSTOM_PERSONA_DATA_TABLE_NAME: data.database.customPersonaDataTable.tableName,
      ...tableNames,
      S3_BUCKET_NAME: data.uploadBucket.bucket.bucketName,
      // Strategy ID はアプリが起動時に GetMemory で引く（手で書き写さない）
      ENABLE_LONG_TERM_MEMORY: 'true',
      AGENTCORE_MEMORY_ID: data.memory.memoryId,
      AGENTCORE_MEMORY_REGION: region,
      BEDROCK_MODEL_ID: parameter.bedrockModelId,
      AGENT_MODEL_ID: parameter.agentModelId,
      ENABLE_ADDITIONAL_PERSONA_MODELS: String(parameter.enableAdditionalPersonaModels ?? false),
      // optional のため未指定時は既定 true。String(undefined) で "undefined" が渡るのを防ぐ
      ENABLE_DATASET_ANALYSIS: String(parameter.enableDatasetAnalysis ?? true),
      BEDROCK_BATCH_ROLE_ARN: batchRoleArn,
      BATCH_INFERENCE_MODEL_ID: parameter.batchInferenceModelId,
      SURVEY_S3_PREFIX: parameter.surveyS3Prefix ?? 'survey-results/',
      BATCH_INFERENCE_S3_PREFIX: parameter.batchInferenceS3Prefix ?? 'batch-inference/',
    };
    if (parameter.dataAgentRuntimeArn) {
      environment.ENABLE_DATA_AGENT = 'true';
      environment.DATA_AGENT_RUNTIME_ARN = parameter.dataAgentRuntimeArn;
      environment.DATA_AGENT_REGION = parameter.dataAgentRegion ?? region;
    }
    return environment;
  }

  private createAppClient(data: DataStack, domainName: string): cognito.UserPoolClient {
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
      // 反映の遅れ（最大 61 分）と最長 7 日のセッションの前提（logical-components.md）
      accessTokenValidity: Duration.minutes(60),
      idTokenValidity: Duration.minutes(60),
      refreshTokenValidity: Duration.days(7),
      enableTokenRevocation: true,
      preventUserExistenceErrors: true,
    });
    // Managed Login のブランディング（Cognito の既定の見た目、Infrastructure Design R-07）。
    // クライアントごとの設定のため、クライアントと同じ App スタックに置く。
    new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: data.cognito.userPool.userPoolId,
      clientId: client.userPoolClientId,
      useCognitoProvidedValues: true,
    });
    return client;
  }

  /**
   * アップロード用バケットの権限（U5、NFR1.33、infrastructure-specification.md）。
   *
   * grantReadWrite を明示的な文に置き換え、古いバージョンの操作（GetObjectVersion*・
   * ListBucketVersions・DeleteObjectVersion）を付けない。消したものは古いバージョンとして
   * 7 日残るが、アプリからは読めない。タグの操作は紐付けの印を付け外しする
   * uploads/* と candidates/* だけ。
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
      resources: [bucket.arnForObjects('uploads/*'), bucket.arnForObjects('candidates/*')],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['s3:ListBucket', 's3:ListBucketMultipartUploads'],
      resources: [bucket.bucketArn],
    }));
  }

  private grantTaskRole(
    taskRole: iam.IRole,
    parameter: AppParameter,
    data: DataStack,
    batchRoleArn: string,
  ): void {
    // テーブルの読み書きはテーブルの ARN に絞る。Sessions はカスタマー管理キーの権限も付く
    for (const table of [...Object.values(data.database.applicationTables), data.database.sessionsTable]) {
      table.grantReadWriteData(taskRole);
    }
    // DatabaseService は起動時に ListTables で接続を確かめる
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:ListTables'],
      resources: ['*'],
    }));
    // 部署ごとの共有の索引（U4、security-design.md §8）。Scan は付けない（member の一覧で
    // Scan をしない、BR3.6）。トランザクションはこれらの操作の権限で動く
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
    // 生成候補（U5、NFR1.40）。押さえは条件付きの UpdateItem。Scan・Query は付けない
    data.database.personaCandidatesTable.grant(
      taskRole,
      'dynamodb:GetItem',
      'dynamodb:BatchGetItem',
      'dynamodb:PutItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
    );
    // 紐付け（U5、ADR-007）。テーブルと TargetIndex に、Scan を除く 7 つの操作
    data.database.personaBindingsTable.grant(
      taskRole,
      'dynamodb:GetItem',
      'dynamodb:BatchGetItem',
      'dynamodb:Query',
      'dynamodb:PutItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:UpdateItem',
      'dynamodb:DeleteItem',
    );
    // 議論の発言・レポートとインタビュー（U6、NFR1.64）。Scan を除く 8 つの操作を 4 テーブル
    // （とそのインデックス）を並べた 1 つの文で付ける。ポリシーの上限（10,240 文字）に近いため
    // テーブルごとに文を足さない。後始末のスクリプトはタスクロールでは動かさない
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
      resources: childTables.flatMap((table) => [table.tableArn, `${table.tableArn}/index/*`]),
    }));
    // カスタムペルソナデータの登録情報（U7、NFR1.80、NFR1.81）。名前の押さえはトランザクション
    // （ConditionCheckItem）、一覧は件数の小さい Scan。GSI がないため Query とインデックスは付けない
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
    // 部署の一覧（DepartmentDirectory、ADR-009）と所有者の名前の写し（OwnerDirectory、U4）。
    // v1 の User Pool の ListGroups・ListUsers だけ（NFR3.8、NFR2.7）
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['cognito-idp:ListGroups', 'cognito-idp:ListUsers'],
      resources: [data.cognito.userPool.userPoolArn],
    }));
    // Bedrock・AgentCore Memory は v0 と同じ範囲（* の絞り込みは U9）
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
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
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [batchRoleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' } },
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
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
    if (parameter.dataAgentRuntimeArn) {
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [parameter.dataAgentRuntimeArn, `${parameter.dataAgentRuntimeArn}/*`],
      }));
    }
    if (parameter.enableAdditionalPersonaModels) {
      // 追加ペルソナベースモデル（Bedrock Mantle 経由の非 Anthropic モデル）
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:CreateInference', 'bedrock-mantle:CallWithBearerToken'],
        resources: ['*'],
      }));
    }
  }
}
