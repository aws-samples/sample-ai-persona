import { Construct, IConstruct } from 'constructs';
import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, StackProps } from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { AwsCustomResource } from 'aws-cdk-lib/custom-resources';
import { AppParameter } from '../parameters';
import { bedrockInferenceArns } from './bedrock-arns';
import { BedrockBatchRole } from './constructs/bedrock-batch-role';
import { ClosedNetwork } from './constructs/closed-network';
import { CloudFrontDistribution } from './constructs/cloudfront';
import { PASSWORD_POLICY } from './constructs/cognito';
import { FargateAppService } from './constructs/fargate-service';
import { ValidationEnvironment } from './constructs/validation-environment';
import { Vpc } from './constructs/vpc';
import { DataStack } from './data-stack';
import { AuthMode, PUBLIC_PROFILE, ResolvedProfile } from './deployment-profile';
import { acknowledgeAll, iam5ExportedResourceId, iam5ResourceId, NAG_REASONS } from './nag-reasons';

/**
 * AwsCustomResource のシングルトンの Lambda の construct ID。CDK は公開の定数
 * PROVIDER_FUNCTION_UUID のハイフンを除いた値に 'AWS' を前に付けて作る。
 */
const AWS_CUSTOM_RESOURCE_PROVIDER_ID = `AWS${AwsCustomResource.PROVIDER_FUNCTION_UUID.replace(/-/g, '')}`;

/**
 * grantReadWriteData がテーブルに付ける操作と同じもの（読み・書き・DescribeTable）。U9 で
 * `<テーブルの ARN>/index/*` を付けないため、grantReadWriteData を明示の文に置き換えた。
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
/** grantReadWriteData がカスタマー管理キーに付ける操作と同じもの（Sessions のキー）。 */
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

const HTTPS = ec2.Port.tcp(443);

/** Cognito のアプリクライアントのトークンの有効期間（公開・閉域で同じ。BR3.3）。 */
const TOKEN_VALIDITY = {
  // 反映の遅れ（最大 61 分）と最長 7 日のセッションの前提（logical-components.md）
  accessTokenValidity: Duration.minutes(60),
  idTokenValidity: Duration.minutes(60),
  refreshTokenValidity: Duration.days(7),
} as const;

/**
 * 閉域のアプリに渡すパスワードの条件（U1 閉域、BR3.6）。User Pool の条件（PASSWORD_POLICY）から
 * 作り、値を二重に持たない。アプリは src/models/password_policy.py の PasswordPolicy.from_settings で読む。
 */
const PASSWORD_POLICY_ENVIRONMENT: Record<string, string> = {
  PASSWORD_MIN_LENGTH: String(PASSWORD_POLICY.minLength),
  PASSWORD_REQUIRE_UPPERCASE: String(PASSWORD_POLICY.requireUppercase),
  PASSWORD_REQUIRE_LOWERCASE: String(PASSWORD_POLICY.requireLowercase),
  PASSWORD_REQUIRE_DIGITS: String(PASSWORD_POLICY.requireDigits),
  PASSWORD_REQUIRE_SYMBOLS: String(PASSWORD_POLICY.requireSymbols),
};

/** ログイン方式で値が変わる環境変数（authEnvironment が作る）。 */
interface AuthEnvironment {
  AUTH_MODE: AuthMode;
  /** oidc だけ（Managed Login のドメイン）。 */
  COGNITO_DOMAIN?: string;
  /** admin_api だけ（パスワードの条件 5 つ）。 */
  passwordPolicy?: Record<string, string>;
}

/** 入口ごとに決まる、アプリの URL と Cognito のアプリクライアント。 */
interface AppEntry {
  appBaseUrl: string;
  client: cognito.UserPoolClient;
}

export interface AppStackProps extends StackProps {
  parameter: AppParameter;
  data: DataStack;
  /** Edge スタックの WebACL の ARN（WAF が無効なら undefined。閉域では渡さない）。 */
  webAclArn?: string;
  /** タスクの台数（1〜10。Stage で検査する）。 */
  appDesiredCount: number;
  /** Stage の解決済みの設定（既定は公開。U1 閉域、ADR-002）。 */
  profile?: ResolvedProfile;
}

/**
 * App スタック: 状態を持たないもの（FR9.1、FR9.3、ADR-001）。
 *
 * VPC・内部 ALB・ECS Fargate・CloudFront・Cognito のアプリクライアント・
 * アプリのロググループとメトリクスフィルタ。Data・Edge から参照で受け取り、
 * 壊して作り直せる。イメージはコンテキスト imageTag のタグを使う（latest は使わない）。
 *
 * 閉域構成（profile.deploymentMode=closed、U1 閉域）では、ClosedNetwork の VPC と
 * HTTPS だけの内部 ALB を使い、CloudFront と Managed Login を作らない。アプリクライアントは
 * 管理 API のログインだけにし、SG の結線・Private Hosted Zone の Alias・検証用 EC2 を足す。
 * 公開構成のテンプレートは変えない（test/public-stage-invariants.test.ts、BR2.2）。
 */
export class AppStack extends Stack {
  constructor(scope: Construct, id: string, props: AppStackProps) {
    super(scope, id, props);

    const { parameter, data, webAclArn, appDesiredCount } = props;
    const profile = props.profile ?? PUBLIC_PROFILE;
    const closed = profile.closedNetwork;
    const imageTag = this.resolveImageTag();

    const network = closed
      ? new ClosedNetwork(this, 'Network', {
        envName: parameter.envName,
        vpcCidr: closed.vpcCidr,
        domainName: closed.domainName,
        createTestEnvironment: closed.createTestEnvironment,
      })
      : undefined;
    const vpc = network ? network.vpc : new Vpc(this, 'Vpc', { envName: parameter.envName }).vpc;

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
      environment: this.commonEnvironment(
        parameter,
        data,
        batchRole.role.roleArn,
        this.authEnvironment(profile, data),
      ),
      secrets: {
        TOKEN_ENCRYPTION_KEYS: ecs.Secret.fromSecretsManager(data.tokenEncryptionKeys),
        LOGIN_COOKIE_SIGNING_KEY: ecs.Secret.fromSecretsManager(data.loginCookieSigningKey),
      },
      ...(closed
        ? {
          ingress: { kind: 'closed' as const, certificateArn: closed.certificateArn },
          subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
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

    this.grantTaskRole(app.taskDefinition.taskRole, parameter, data, batchRole.role.roleArn, profile);

    // cdk-nag の抑止（U9、NFR7.15、NFR3.14）。理由は nag-reasons.ts
    acknowledgeAll(clientSecret, ['SMG4'], NAG_REASONS.cognitoClientSecret);
    this.acknowledgeCustomResourceRole();
    this.acknowledgeIamWildcards(app, batchRole, data, parameter);

    new CfnOutput(this, 'AppUrl', { value: appBaseUrl });
    new CfnOutput(this, 'ClusterName', { value: app.cluster.clusterName });
    new CfnOutput(this, 'ServiceName', { value: app.service.serviceName });
  }

  /** 公開の入口: CloudFront（VPC オリジン）と Managed Login のアプリクライアント。 */
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
   * 閉域の入口（U1 閉域）: 管理 API のログインのアプリクライアント、検証用 EC2（指定したとき
   * だけ）、SG の結線、Private Hosted Zone の頂点の ALB への Alias。CloudFront は作らない（BR5.5）。
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
    const validation = closed.createTestEnvironment
      ? new ValidationEnvironment(this, 'Validation', { vpc: network.vpc })
      : undefined;
    this.wireClosedSecurityGroups(app, network, closed.allowedCidrs, validation);
    // アプリは起動時に一度だけ GetMemory を引き、失敗すると長期記憶を無効にしたまま動く。
    // エンドポイントとタスクの SG のルールができてからサービスを作る（CloudFormation は
    // 依存のないリソースを並べて作るため）。
    app.service.node.addDependency(network, app.taskSecurityGroup);
    // FQDN（zone の頂点）を内部 ALB に向ける（BR4.4）
    new route53.ARecord(this, 'AppAlias', {
      zone: network.hostedZone,
      target: route53.RecordTarget.fromAlias(new route53Targets.LoadBalancerTarget(app.loadBalancer)),
    });
    return { appBaseUrl, client };
  }

  /**
   * 閉域の SG の結線（U1 閉域、security-design.md §3 の表、BR5.3・BR5.4、NFR3.2・NFR3.6）。
   *
   * ALB からタスクへの 80 は FargateAppService（listener.addTargets）が両側に足す。ここでは
   * 送り元から ALB への 443、タスク・検証用 EC2 からエンドポイントへの 443、タスクから
   * S3・DynamoDB のゲートウェイ型エンドポイント（プレフィックスリスト）への 443 を足す。
   * SG どうしのルールは別のリソース（SecurityGroupIngress / Egress）になり、循環しない。
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

    for (const cidr of allowedCidrs) {
      alb.addIngressRule(ec2.Peer.ipv4(cidr), HTTPS, `Allowed client ${cidr}`);
    }
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
   * IAM4: client.userPoolClientSecret が作る AwsCustomResource の Lambda（スタックに 1 つの
   * シングルトン）のロール。CDK の内部の作るもの。
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
   * IAM5（ワイルドカード）の抑止をロールごと・指摘ごとに付ける（NFR7.16）。
   *
   * Data スタックから受け取る ARN（バケット・Memory）の指摘の ID には、CDK が論理 ID から
   * 作る出力名が入る。その名前はコードに書かず、iam5ExportedResourceId で参照から求める。
   * テーブルの GSI は名前で付けるため `/index/*` の指摘は出ない。バケットの接頭辞を足す
   * 変更は、ここと test/cdk-nag.test.ts を一緒に直す。
   */
  private acknowledgeIamWildcards(
    app: FargateAppService,
    batchRole: BedrockBatchRole,
    data: DataStack,
    parameter: AppParameter,
  ): void {
    const bucketObjects = (...suffixes: string[]) =>
      suffixes.map((suffix) => iam5ExportedResourceId(data, data.uploadBucket.bucket.bucketArn, suffix));
    const inference = bedrockInferenceArns(this).map((arn) => iam5ResourceId(this, arn));

    const taskPolicy = defaultPolicyOf(app.taskDefinition.taskRole);
    acknowledgeAll(taskPolicy, ['IAM5[Resource::*]'], NAG_REASONS.wildcardListApis);
    acknowledgeAll(taskPolicy, bucketObjects('/*', '/uploads/*', '/candidates/*'), NAG_REASONS.bucketObjects);
    acknowledgeAll(taskPolicy, inference, NAG_REASONS.bedrockInference);
    acknowledgeAll(
      taskPolicy,
      ['model-invocation-job/*', 'knowledge-base/*'].map((resource) =>
        iam5ResourceId(this, `arn:${this.partition}:bedrock:${this.region}:${this.account}:${resource}`),
      ),
      NAG_REASONS.bedrockAccountResources,
    );
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

    const batchPolicy = defaultPolicyOf(batchRole.role);
    acknowledgeAll(
      batchPolicy,
      bucketObjects('/batch-inference/*', '/batch-inference/output/*'),
      NAG_REASONS.bucketObjects,
    );
    acknowledgeAll(batchPolicy, inference, NAG_REASONS.bedrockInference);
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

  /**
   * ログイン方式で値が変わる環境変数（U1 閉域、BR3.6）。モードで分岐してから domainHost を読む
   * （閉域の User Pool にはドメインがなく、読むとエラーになるため。Functional Design レビュー）。
   */
  private authEnvironment(profile: ResolvedProfile, data: DataStack): AuthEnvironment {
    if (profile.authMode === 'admin_api') {
      return { AUTH_MODE: 'admin_api', passwordPolicy: PASSWORD_POLICY_ENVIRONMENT };
    }
    return { AUTH_MODE: 'oidc', COGNITO_DOMAIN: data.cognito.domainHost };
  }

  /**
   * タスクの環境変数。ログイン方式の値（auth）は今と同じ位置に置く: AUTH_MODE は先頭、
   * COGNITO_DOMAIN は COGNITO_USER_POOL_ID の次、パスワードの条件は末尾。並び順はタスク定義の
   * テンプレートに出るため、公開構成のテンプレートを変えないよう順番を保つ（BR2.2）。
   */
  private commonEnvironment(
    parameter: AppParameter,
    data: DataStack,
    batchRoleArn: string,
    auth: AuthEnvironment,
  ): Record<string, string> {
    const region = this.region;
    const tableNames = Object.fromEntries(
      Object.entries(data.database.applicationTables).map(([name, table]) => [name, table.tableName]),
    );
    const environment: Record<string, string> = {
      // ログイン方式（DeploymentProfile が決める。public は oidc、closed は admin_api）
      AUTH_MODE: auth.AUTH_MODE,
      AWS_REGION: region,
      DYNAMODB_REGION: region,
      COGNITO_USER_POOL_ID: data.cognito.userPool.userPoolId,
      ...(auth.COGNITO_DOMAIN !== undefined ? { COGNITO_DOMAIN: auth.COGNITO_DOMAIN } : {}),
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
      ...(auth.passwordPolicy ?? {}),
    };
    if (parameter.dataAgentRuntimeArn) {
      environment.ENABLE_DATA_AGENT = 'true';
      environment.DATA_AGENT_RUNTIME_ARN = parameter.dataAgentRuntimeArn;
      environment.DATA_AGENT_REGION = parameter.dataAgentRegion ?? region;
    }
    return environment;
  }

  /** 公開のアプリクライアント: Managed Login（OIDC の認可コード）でログインする。 */
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
   * 閉域のアプリクライアント（U1 閉域、BR3.3）: アプリが管理 API（AdminInitiateAuth）で
   * ログインする。認証フローは ADMIN_USER_PASSWORD_AUTH だけ（REFRESH_TOKEN_AUTH は CDK が
   * 足す）。OAuth を無効にし、Managed Login のブランディングは作らない。シークレット・
   * トークンの失効・有効期間は公開と同じ。
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
    profile: ResolvedProfile,
  ): void {
    // テーブルの読み書きはテーブルの ARN と、アプリが Query する GSI の ARN（名前で付ける）に
    // 絞る（U9）。操作は grantReadWriteData と同じ。Sessions はカスタマー管理キーの権限も付ける
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
    // リソースを指定できない一覧の API だけを * で付ける（U9、NFR7.16。例外の一覧は
    // test/iam-wildcards.test.ts）。DatabaseService は起動時に ListTables で接続を確かめる
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'dynamodb:ListTables',
        'bedrock:ListFoundationModels',
        'bedrock:ListInferenceProfiles',
        'bedrock:ListModelInvocationJobs',
        'bedrock-agentcore:ListMemories',
      ],
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
    // 紐付け（U5、ADR-007）。テーブルと TargetIndex（名前で付ける）に、Scan を除く 7 つの操作
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
    // 議論の発言・レポートとインタビュー（U6、NFR1.64）。Scan を除く 8 つの操作を 4 テーブル
    // を並べた 1 つの文で付ける（GSI はないため、インデックスの ARN は並ばない）。ポリシーの上限（10,240 文字）に近いため
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
      resources: childTables.flatMap((table) => [
        table.tableArn,
        ...data.database.queriedIndexArns(table),
      ]),
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
    // 閉域の管理 API のログイン（U1 閉域、BR3.4、NFR1.1）。この Stage の User Pool だけ。
    // 公開（oidc）には付けない（BR3.5）
    if (profile.authMode === 'admin_api') {
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['cognito-idp:AdminInitiateAuth', 'cognito-idp:AdminRespondToAuthChallenge'],
        resources: [data.cognito.userPool.userPoolArn],
      }));
    }
    // Bedrock（U9、NFR7.16）: 操作は v0 と同じで、リソースを種類ごとに絞る。
    // minimizePolicies は同じリソースの文をまとめるため、種類ごとに文を分ける
    const { partition, account } = Stack.of(this);
    const inferenceArns = bedrockInferenceArns(this);
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream',
        'bedrock:GetFoundationModel', 'bedrock:GetInferenceProfile',
      ],
      resources: inferenceArns,
    }));
    // バッチ推論（アンケート）。CreateModelInvocationJob はジョブの ARN と使うモデルの ARN の
    // 両方で認可される（Bedrock ユーザーガイド「Required permissions for batch inference」）
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:CreateModelInvocationJob', 'bedrock:GetModelInvocationJob',
        'bedrock:StopModelInvocationJob',
        'bedrock:TagResource', 'bedrock:UntagResource', 'bedrock:ListTagsForResource',
      ],
      resources: [
        `arn:${partition}:bedrock:${this.region}:${account}:model-invocation-job/*`,
        ...inferenceArns,
      ],
    }));
    // ナレッジベースの検索は、このアカウント・リージョンの KB だけ
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['bedrock:Retrieve'],
      resources: [`arn:${partition}:bedrock:${this.region}:${account}:knowledge-base/*`],
    }));
    taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [batchRoleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' } },
    }));
    // AgentCore Memory は、この Stage の Memory だけ（ListMemories は上の一覧の文）。
    // ListEvents などの子の操作のため <ARN>/* も並べる
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
      // 追加ペルソナベースモデル（Bedrock Mantle 経由の非 Anthropic モデル）。CreateInference は
      // このアカウントの project に絞る。リージョンは *（アプリの resolve_call_region が非対応の
      // リージョンから us-east-1 へ送るため、スタックのリージョンと呼び先が食い違う）
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:CreateInference'],
        resources: [`arn:${partition}:bedrock-mantle:*:${account}:project/*`],
      }));
      // CallWithBearerToken はリソースの種類を持たないため * のまま（例外）
      taskRole.addToPrincipalPolicy(new iam.PolicyStatement({
        actions: ['bedrock-mantle:CallWithBearerToken'],
        resources: ['*'],
      }));
    }
  }
}
