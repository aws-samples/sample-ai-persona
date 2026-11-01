import * as cdk from 'aws-cdk-lib';
import { Annotations, Match, Template } from 'aws-cdk-lib/assertions';
import { AppStack } from '../lib/app-stack';
import { DataStack } from '../lib/data-stack';
import { EdgeStack } from '../lib/edge-stack';
import { AppParameter, devParameter } from '../parameters';

/**
 * App スタック（Fargate・内部 ALB・CloudFront・アプリクライアント・ログ）の検査。
 * FR1.8、FR9.3、NFR3.1、NFR3.3、NFR4.5、NFR4.7、NFR4.8、NFR5.1、NFR5.3、NFR8.1、
 * NFR8.3〜NFR8.5、NFR9.1、NFR9.3、NFR9.9、BR1.20。
 * express-service.test.ts の ENABLE_DATASET_ANALYSIS の検査もここへ移した。
 */

const TOKYO = { account: '123456789012', region: 'ap-northeast-1' };
const VIRGINIA = { account: '123456789012', region: 'us-east-1' };

// AWS 管理のキャッシュ・オリジンリクエストポリシーの ID
const CACHING_DISABLED = '4135ea2d-6df8-44a3-9df3-4b5a84be39ad';
const CACHING_OPTIMIZED = '658327ea-f89d-4fab-a63d-7e88639e58f6';
const ALL_VIEWER_EXCEPT_HOST = 'b689b0a8-53d0-40ab-baf2-68738e2966ac';

/**
 * cdk.json の機能フラグ（cdk deploy が使う設定）。U6: タスクロールの方針の大きさは
 * `@aws-cdk/aws-iam:minimizePolicies` で文がまとまった形で決まるため、テストも同じ設定で組む
 * （フラグなしでは文がまとまらず、上限を超えた分が別の管理ポリシーへ溢れる）。
 */
// eslint-disable-next-line @typescript-eslint/no-require-imports
const FEATURE_FLAGS: Record<string, unknown> = require('../cdk.json').context;

function build(
  options: { context?: Record<string, string>; parameter?: Partial<AppParameter> } = {},
): { stack: AppStack; template: Template } {
  const app = new cdk.App({ context: { ...FEATURE_FLAGS, ...(options.context ?? {}) } });
  const parameter: AppParameter = { ...devParameter, ...(options.parameter ?? {}) };
  const data = new DataStack(app, 'Data', { env: TOKYO, parameter });
  const edge = new EdgeStack(app, 'Edge', {
    env: VIRGINIA,
    envName: 'dev',
    enableWaf: true,
    crossRegionReferences: true,
  });
  const stack = new AppStack(app, 'App', {
    env: TOKYO,
    parameter,
    data,
    webAclArn: edge.webAclArn,
    appDesiredCount: 1,
    crossRegionReferences: true,
  });
  return { stack, template: Template.fromStack(stack) };
}

const { stack, template } = build({ context: { imageTag: 'abc1234' } });

function container(t: Template): any {
  const defs = Object.values(t.findResources('AWS::ECS::TaskDefinition'));
  expect(defs).toHaveLength(1);
  return defs[0].Properties.ContainerDefinitions[0];
}

function envOf(t: Template): Record<string, any> {
  return Object.fromEntries(
    (container(t).Environment as { Name: string; Value: any }[]).map((e) => [e.Name, e.Value]),
  );
}

describe('AppStack: ネットワーク', () => {
  test('VPC は 2 AZ・NAT 1 台・DynamoDB と S3 のゲートウェイ型エンドポイント', () => {
    template.resourceCountIs('AWS::EC2::VPC', 1);
    template.resourceCountIs('AWS::EC2::NatGateway', 1);
    const privateSubnets = Object.values(template.findResources('AWS::EC2::Subnet')).filter(
      (s) => s.Properties.MapPublicIpOnLaunch === false,
    );
    expect(privateSubnets).toHaveLength(2);
    const gateways = Object.values(template.findResources('AWS::EC2::VPCEndpoint')).filter(
      (e) => e.Properties.VpcEndpointType === 'Gateway',
    );
    const services = JSON.stringify(gateways.map((g) => g.Properties.ServiceName));
    expect(services).toContain('dynamodb');
    expect(services).toContain('s3');
  });

  test('VPC に DESTROY を掛けない', () => {
    for (const vpc of Object.values(template.findResources('AWS::EC2::VPC'))) {
      expect(vpc.DeletionPolicy).not.toBe('Delete');
    }
  });

  test('ALB は内部向け、無操作タイムアウト 300 秒、HTTP 80', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
      LoadBalancerAttributes: Match.arrayWith([
        { Key: 'idle_timeout.timeout_seconds', Value: '300' },
      ]),
    });
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::Listener', {
      Port: 80,
      Protocol: 'HTTP',
    });
  });

  test('ターゲットのヘルスチェックは /health・30 秒・正常 2・異常 3、登録解除の遅延 30 秒', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      TargetType: 'ip',
      Port: 80,
      HealthCheckPath: '/health',
      HealthCheckIntervalSeconds: 30,
      HealthyThresholdCount: 2,
      UnhealthyThresholdCount: 3,
      TargetGroupAttributes: Match.arrayWith([
        { Key: 'deregistration_delay.timeout_seconds', Value: '30' },
      ]),
    });
  });

  test('ALB は CloudFront のプレフィックスリストからの 80 番だけを受ける', () => {
    const groups = template.findResources('AWS::EC2::SecurityGroup');
    const [albGroupId, albGroup] = Object.entries(groups).find(([, g]) =>
      (g.Properties.GroupDescription as string).includes('Load balancer'),
    )!;
    // インラインの受け入れと、別リソースの受け入れの両方を集める
    const separate = Object.values(template.findResources('AWS::EC2::SecurityGroupIngress'))
      .map((r) => r.Properties)
      .filter((p) => JSON.stringify(p.GroupId).includes(albGroupId));
    const ingress = [...(albGroup.Properties.SecurityGroupIngress ?? []), ...separate];
    expect(ingress).toHaveLength(1);
    expect(ingress[0]).toMatchObject({ IpProtocol: 'tcp', FromPort: 80, ToPort: 80 });
    expect(ingress[0].SourcePrefixListId).toBe('pl-58a04531');
    expect(ingress[0].CidrIp).toBeUndefined();
  });
});

describe('AppStack: Fargate', () => {
  test('1 vCPU / 4 GB、台数、サーキットブレーカーと自動ロールバック、最小 100%・最大 200%', () => {
    template.hasResourceProperties('AWS::ECS::TaskDefinition', {
      Cpu: '1024',
      Memory: '4096',
      RequiresCompatibilities: ['FARGATE'],
    });
    template.hasResourceProperties('AWS::ECS::Service', {
      DesiredCount: 1,
      LaunchType: 'FARGATE',
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: { Enable: true, Rollback: true },
        MinimumHealthyPercent: 100,
        MaximumPercent: 200,
      }),
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: 'DISABLED' }),
      },
    });
  });

  test('stopTimeout 120 秒、コンテナポート 80', () => {
    expect(container(template).StopTimeout).toBe(120);
    expect(container(template).PortMappings[0].ContainerPort).toBe(80);
  });

  test('環境変数: AUTH_MODE=oidc、APP_BASE_URL、Cognito、テーブルごとの名前', () => {
    const env = envOf(template);
    expect(env.AUTH_MODE).toBe('oidc');
    expect(JSON.stringify(env.APP_BASE_URL)).toContain('https://');
    for (const name of [
      'COGNITO_USER_POOL_ID',
      'COGNITO_CLIENT_ID',
      'COGNITO_DOMAIN',
      'SESSIONS_TABLE_NAME',
      'PERSONAS_TABLE_NAME',
      'DISCUSSIONS_TABLE_NAME',
      'UPLOADED_FILES_TABLE_NAME',
      'DATASETS_TABLE_NAME',
      'SURVEY_TEMPLATES_TABLE_NAME',
      'SURVEYS_TABLE_NAME',
      'KNOWLEDGE_BASES_TABLE_NAME',
      'JOBS_TABLE_NAME',
      'PERSONA_CANDIDATES_TABLE_NAME',
      'PERSONA_BINDINGS_TABLE_NAME',
      'S3_BUCKET_NAME',
      'AGENTCORE_MEMORY_ID',
      'DYNAMODB_REGION',
    ]) {
      expect(env[name]).toBeDefined();
    }
    // 秘密を環境変数に平文で置かない
    for (const secret of ['COGNITO_CLIENT_SECRET', 'TOKEN_ENCRYPTION_KEYS', 'LOGIN_COOKIE_SIGNING_KEY']) {
      expect(env[secret]).toBeUndefined();
    }
  });

  test('秘密は ECS のシークレットとして渡す', () => {
    const names = (container(template).Secrets as { Name: string }[]).map((s) => s.Name).sort();
    expect(names).toEqual(['COGNITO_CLIENT_SECRET', 'LOGIN_COOKIE_SIGNING_KEY', 'TOKEN_ENCRYPTION_KEYS']);
  });

  test.each([
    [true, 'true'],
    [false, 'false'],
    [undefined, 'true'],
  ])('enableDatasetAnalysis=%s → ENABLE_DATASET_ANALYSIS=%s（"undefined" にならない）', (value, expected) => {
    const { template: t } = build({
      context: { imageTag: 'abc1234' },
      parameter: { enableDatasetAnalysis: value },
    });
    expect(envOf(t).ENABLE_DATASET_ANALYSIS).toBe(expected);
  });

  test('タスクロールの DynamoDB の権限はテーブルの ARN に絞る', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy'));
    const statements = policies.flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
    const dynamoWrites = statements.filter((s) =>
      [s.Action].flat().some((a: string) => a === 'dynamodb:PutItem'),
    );
    // minimizePolicies で同じ操作の文はまとまるため、文の数ではなく、書き込むテーブルが
    // どれも文の対象に入っていることを確かめる
    const resources = JSON.stringify(dynamoWrites.map((s) => s.Resource));
    for (const table of [
      'Personas', 'Discussions', 'UploadedFiles', 'Datasets', 'SurveyTemplates', 'Surveys',
      'KnowledgeBases', 'Jobs', 'Sessions', 'ResourceShares', 'PersonaCandidates',
      'PersonaBindings', 'DiscussionMessages', 'DiscussionReports', 'InterviewSessions',
      'InterviewMessages',
    ]) {
      expect(resources).toMatch(new RegExp(`Database${table}[0-9A-F]{8}Arn`));
    }
    for (const s of dynamoWrites) {
      expect([s.Resource].flat()).not.toContain('*');
    }
    const kms = statements.filter((s) => [s.Action].flat().includes('kms:Decrypt'));
    expect(kms.length).toBeGreaterThan(0);
  });
});

describe('AppStack: イメージのタグ（Infrastructure Design レビュー R-09）', () => {
  test('imageTag を渡せばそのタグの ECR のイメージを使う', () => {
    expect(JSON.stringify(container(template).Image)).toContain(':abc1234');
  });

  test('imageTag がなくても synth は通り、unset と warning', () => {
    const { stack: untagged, template: t } = build();
    expect(JSON.stringify(container(t).Image)).toContain(':unset');
    Annotations.fromStack(untagged).hasWarning('*', Match.stringLikeRegexp('imageTag'));
  });
});

describe('AppStack: Cognito のアプリクライアント', () => {
  test('シークレットあり、コード、openid email profile、トークン 60 分・更新 7 日、失効あり', () => {
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      GenerateSecret: true,
      AllowedOAuthFlows: ['code'],
      AllowedOAuthFlowsUserPoolClient: true,
      AllowedOAuthScopes: Match.arrayWith(['openid', 'email', 'profile']),
      AccessTokenValidity: 60,
      IdTokenValidity: 60,
      RefreshTokenValidity: 7 * 24 * 60,
      TokenValidityUnits: { AccessToken: 'minutes', IdToken: 'minutes', RefreshToken: 'minutes' },
      EnableTokenRevocation: true,
    });
  });

  test('コールバックは /auth/callback、ログアウトの戻り先は /login', () => {
    const client = Object.values(template.findResources('AWS::Cognito::UserPoolClient'))[0];
    expect(JSON.stringify(client.Properties.CallbackURLs)).toContain('/auth/callback');
    expect(JSON.stringify(client.Properties.LogoutURLs)).toContain('/login');
  });

  test('U8 BR4.2: 公開構成のクライアントは ADMIN_USER_PASSWORD_AUTH を許さない', () => {
    const clients = Object.values(template.findResources('AWS::Cognito::UserPoolClient'));
    expect(clients).toHaveLength(1);
    const flows: string[] = clients[0].Properties.ExplicitAuthFlows ?? [];
    expect(flows).not.toContain('ALLOW_ADMIN_USER_PASSWORD_AUTH');
    expect(flows).not.toContain('ADMIN_NO_SRP_AUTH');
  });

  test('Managed Login のブランディングは Cognito の既定の見た目（R-07）', () => {
    template.hasResourceProperties('AWS::Cognito::ManagedLoginBranding', {
      UseCognitoProvidedValues: true,
    });
  });
});

describe('AppStack: CloudFront', () => {
  const distribution = () =>
    Object.values(template.findResources('AWS::CloudFront::Distribution'))[0].Properties
      .DistributionConfig;

  test('Lambda@Edge はなく、アプリの URL はキャッシュしない', () => {
    const config = distribution();
    expect(config.DefaultCacheBehavior.LambdaFunctionAssociations).toBeUndefined();
    expect(config.DefaultCacheBehavior.CachePolicyId).toBe(CACHING_DISABLED);
    expect(config.DefaultCacheBehavior.OriginRequestPolicyId).toBe(ALL_VIEWER_EXCEPT_HOST);
    expect(config.DefaultCacheBehavior.ViewerProtocolPolicy).toBe('redirect-to-https');
    expect(config.WebACLId).toBeDefined();
  });

  test('/static/* だけはキャッシュし、版（クエリの v）をキャッシュの鍵に含める', () => {
    const behaviors = distribution().CacheBehaviors as any[];
    expect(behaviors).toHaveLength(1);
    expect(behaviors[0].PathPattern).toBe('/static/*');
    // 管理ポリシー CachingOptimized はクエリを鍵に含めず、デプロイ後も古い JS・CSS を
    // 最大 1 日返していた。v（中身のハッシュ）を鍵に含める自前のポリシーにする
    expect(behaviors[0].CachePolicyId).not.toBe(CACHING_OPTIMIZED);
    template.hasResourceProperties('AWS::CloudFront::CachePolicy', {
      CachePolicyConfig: Match.objectLike({
        ParametersInCacheKeyAndForwardedToOrigin: Match.objectLike({
          EnableAcceptEncodingGzip: true,
          EnableAcceptEncodingBrotli: true,
          QueryStringsConfig: { QueryStringBehavior: 'whitelist', QueryStrings: ['v'] },
          CookiesConfig: { CookieBehavior: 'none' },
          HeadersConfig: { HeaderBehavior: 'none' },
        }),
      }),
    });
  });

  test('VPC オリジンは内部 ALB へ HTTP、読み取り 120 秒', () => {
    template.hasResourceProperties('AWS::CloudFront::VpcOrigin', {
      VpcOriginEndpointConfig: Match.objectLike({
        HTTPPort: 80,
        OriginProtocolPolicy: 'http-only',
      }),
    });
    const origin = distribution().Origins[0];
    expect(origin.VpcOriginConfig.OriginReadTimeout).toBe(120);
  });
});

describe('AppStack: ログとメトリクス', () => {
  test('ロググループは 30 日で RETAIN', () => {
    template.hasResource('AWS::Logs::LogGroup', {
      DeletionPolicy: 'Retain',
      Properties: { RetentionInDays: 30 },
    });
  });

  test('メトリクスフィルタ 7 つ（名前空間 AIPersona/Auth、RefreshFailed は reason の次元）', () => {
    const filters = Object.values(template.findResources('AWS::Logs::MetricFilter'));
    expect(filters).toHaveLength(7);
    const byName = Object.fromEntries(
      filters.map((f) => [f.Properties.MetricTransformations[0].MetricName, f.Properties]),
    );
    expect(Object.keys(byName).sort()).toEqual([
      'AuthzDenied',
      'CircuitOpened',
      'CsrfRejected',
      'DecryptFailed',
      'LoginFailed',
      'RefreshFailed',
      'SessionExpired',
    ]);
    for (const props of Object.values(byName) as any[]) {
      expect(props.MetricTransformations[0].MetricNamespace).toBe('AIPersona/Auth');
      expect(props.MetricTransformations[0].MetricValue).toBe('1');
      expect(props.MetricTransformations[0].DefaultValue).toBeUndefined();
    }
    expect(byName.LoginFailed.FilterPattern).toBe('{ $.event = "auth.login_failed" }');
    expect(byName.RefreshFailed.MetricTransformations[0].Dimensions).toEqual([
      { Key: 'reason', Value: '$.reason' },
    ]);
  });

  test('AuthzDenied: authz.denied を reason の次元で数える（U2、NFR8.7）。アラームなし', () => {
    const filter = Object.values(template.findResources('AWS::Logs::MetricFilter')).find(
      (f) => f.Properties.MetricTransformations[0].MetricName === 'AuthzDenied',
    )!;
    expect(filter.Properties.FilterPattern).toBe('{ $.event = "authz.denied" }');
    const transformation = filter.Properties.MetricTransformations[0];
    expect(transformation.MetricNamespace).toBe('AIPersona/Auth');
    expect(transformation.DefaultValue).toBeUndefined();
    expect(transformation.Dimensions).toEqual([{ Key: 'reason', Value: '$.reason' }]);
    template.resourceCountIs('AWS::CloudWatch::Alarm', 0);
  });
});

describe('AppStack: 公開構成に閉域構成のログインの権限を付けない（U8、BR4.2、NFR3.18）', () => {
  const ADMIN_ACTIONS = ['cognito-idp:AdminInitiateAuth', 'cognito-idp:AdminRespondToAuthChallenge'];

  function actionsOf(t: Template): string[] {
    const policies = [
      ...Object.values(t.findResources('AWS::IAM::Policy')),
      ...Object.values(t.findResources('AWS::IAM::ManagedPolicy')),
    ];
    return policies.flatMap((p) =>
      (p.Properties.PolicyDocument.Statement as any[]).flatMap((s) => [s.Action].flat()),
    );
  }

  test('どの方針にも AdminInitiateAuth・AdminRespondToAuthChallenge がない', () => {
    const actions = actionsOf(template);
    expect(actions.length).toBeGreaterThan(0);
    for (const action of ADMIN_ACTIONS) {
      expect(actions).not.toContain(action);
    }
    // ワイルドカードでも付けない
    for (const action of actions) {
      expect(action).not.toMatch(/^cognito-idp:(\*|Admin\*)$/);
      expect(action).not.toBe('*');
    }
  });

  test('App は AUTH_MODE=oidc でパスワードの条件の環境変数を持たない', () => {
    const env = envOf(template);
    expect(env.AUTH_MODE).toBe('oidc');
    for (const name of Object.keys(env)) {
      expect(name.startsWith('PASSWORD_')).toBe(false);
    }
  });
});

describe('AppStack: 認可（U2）', () => {
  function statements(t: Template): any[] {
    return Object.values(t.findResources('AWS::IAM::Policy')).flatMap(
      (p) => p.Properties.PolicyDocument.Statement as any[],
    );
  }

  test('タスクロールは ListGroups を Data スタックの User Pool の ARN だけに持つ（NFR3.8）', () => {
    const listGroups = statements(template).filter((s) =>
      [s.Action].flat().includes('cognito-idp:ListGroups'),
    );
    expect(listGroups).toHaveLength(1);
    const resources = [listGroups[0].Resource].flat();
    expect(resources).toHaveLength(1);
    expect(resources[0]).not.toBe('*');
    const reference = JSON.stringify(resources[0]);
    expect(reference).toContain('UserPool');
    expect(reference).toContain('Arn');
  });

  test('ListUsers は ListGroups と同じ文で、Data スタックの User Pool の ARN だけ（U4、NFR2.7）', () => {
    const listUsers = statements(template).filter((s) =>
      [s.Action].flat().includes('cognito-idp:ListUsers'),
    );
    expect(listUsers).toHaveLength(1);
    expect([listUsers[0].Action].flat().sort()).toEqual([
      'cognito-idp:ListGroups',
      'cognito-idp:ListUsers',
    ]);
    const resources = [listUsers[0].Resource].flat();
    expect(resources).toHaveLength(1);
    expect(JSON.stringify(resources[0])).toContain('UserPool');
  });

  test('ResourceShares への権限は 7 つの操作だけで Scan を含まない（U4、NFR1.21）', () => {
    const touchesShares = (s: any) =>
      JSON.stringify(s.Resource ?? '').includes('DatabaseResourceShares');
    const shareStatements = statements(template).filter(touchesShares);
    expect(shareStatements.length).toBeGreaterThan(0);
    const actions = new Set(shareStatements.flatMap((s) => [s.Action].flat()));
    expect([...actions].sort()).toEqual([
      'dynamodb:BatchGetItem',
      'dynamodb:ConditionCheckItem',
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    ]);
    expect(actions.has('dynamodb:Scan')).toBe(false);
  });

  test('コンテナに RESOURCE_SHARES_TABLE_NAME をテーブルの参照で渡す（約束 C10）', () => {
    const value = JSON.stringify(envOf(template).RESOURCE_SHARES_TABLE_NAME);
    expect(value).toContain('DatabaseResourceShares');
  });

  test('Jobs テーブルの論理 ID・キー・GSI・TTL は U1 から変わらない（約束 C14）', () => {
    const app = new cdk.App();
    const data = new DataStack(app, 'Data', { env: TOKYO, parameter: devParameter });
    const tables = Template.fromStack(data).findResources('AWS::DynamoDB::GlobalTable');
    const jobs = tables[data.getLogicalId(data.database.jobsTable.node.defaultChild as cdk.CfnElement)];
    expect(jobs).toBeDefined();
    expect(jobs.DeletionPolicy).toBe('Retain');
    expect(jobs.Properties.KeySchema).toEqual([{ AttributeName: 'id', KeyType: 'HASH' }]);
    expect(jobs.Properties.GlobalSecondaryIndexes).toBeUndefined();
    expect(jobs.Properties.TimeToLiveSpecification).toEqual({
      AttributeName: 'expires_at',
      Enabled: true,
    });
  });
});

describe('AppStack: ペルソナに付随するもの（U5、NFR1.33、NFR1.40、NFR1.41）', () => {
  /** Statements of the task role's policies (the Bedrock batch role is separate). */
  function taskStatements(t: Template): any[] {
    return Object.values(t.findResources('AWS::IAM::Policy'))
      .filter((p) => JSON.stringify(p.Properties.Roles).includes('TaskRole'))
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  }
  const actionsOf = (statements: any[]) =>
    new Set(statements.flatMap((s) => [s.Action].flat() as string[]));
  const touching = (logicalIdPart: string) =>
    taskStatements(template).filter((s) => JSON.stringify(s.Resource ?? '').includes(logicalIdPart));

  test('新しい 2 テーブルの名前をテーブルの参照で渡す（約束 C10）', () => {
    const env = envOf(template);
    expect(JSON.stringify(env.PERSONA_CANDIDATES_TABLE_NAME)).toContain('DatabasePersonaCandidates');
    expect(JSON.stringify(env.PERSONA_BINDINGS_TABLE_NAME)).toContain('DatabasePersonaBindings');
  });

  test('古い 2 つの紐付けのテーブルの環境変数と権限はない（Q1 A）', () => {
    const env = envOf(template);
    expect(env.PERSONA_DATASET_BINDINGS_TABLE_NAME).toBeUndefined();
    expect(env.PERSONA_KB_BINDINGS_TABLE_NAME).toBeUndefined();
    expect(touching('DatabasePersonaDatasetBindings')).toEqual([]);
    expect(touching('DatabasePersonaKBBindings')).toEqual([]);
  });

  test('PersonaCandidates は 6 つの操作だけで Scan と Query を含まない', () => {
    const actions = actionsOf(touching('DatabasePersonaCandidates'));
    expect([...actions].sort()).toEqual([
      'dynamodb:BatchGetItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ]);
  });

  test('PersonaBindings はテーブルと TargetIndex（名前で付ける）に 7 つの操作だけで Scan を含まない', () => {
    const statements = touching('DatabasePersonaBindings');
    const actions = actionsOf(statements);
    expect([...actions].sort()).toEqual([
      'dynamodb:BatchGetItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    ]);
    expect(JSON.stringify(statements)).toContain('/index/TargetIndex');
    expect(JSON.stringify(statements)).not.toContain('/index/*');
  });

  test('S3 の方針に古いバージョンの操作がない', () => {
    const actions = actionsOf(taskStatements(template));
    for (const forbidden of [
      's3:GetObjectVersion',
      's3:GetObjectVersionTagging',
      's3:ListBucketVersions',
      's3:DeleteObjectVersion',
      's3:PutObjectVersionTagging',
    ]) {
      expect(actions.has(forbidden)).toBe(false);
    }
    for (const action of actions) {
      expect(action).not.toMatch(/^s3:.*Version/);
      expect(action).not.toBe('s3:*');
      expect(action).not.toMatch(/^s3:(GetObject|DeleteObject)\*$/);
    }
  });

  test('今のコードが使う S3 の操作はある（足りないことを見落とさない）', () => {
    const actions = actionsOf(taskStatements(template));
    for (const needed of [
      's3:GetObject',
      's3:PutObject',
      's3:DeleteObject',
      's3:ListBucket',
      's3:AbortMultipartUpload',
    ]) {
      expect(actions.has(needed)).toBe(true);
    }
  });

  test('タグの 3 操作は uploads/* と candidates/* だけが対象', () => {
    const tagging = taskStatements(template).filter((s) =>
      [s.Action].flat().some((a: string) => a.endsWith('ObjectTagging')),
    );
    expect(tagging).toHaveLength(1);
    expect([tagging[0].Action].flat().sort()).toEqual([
      's3:DeleteObjectTagging',
      's3:GetObjectTagging',
      's3:PutObjectTagging',
    ]);
    const resources = JSON.stringify(tagging[0].Resource);
    expect(resources).toContain('/uploads/*');
    expect(resources).toContain('/candidates/*');
    expect([tagging[0].Resource].flat()).toHaveLength(2);
  });
});

describe('AppStack: 議論の発言・レポートとインタビュー（U6、NFR1.64、NFR2.20）', () => {
  const NEW_TABLES = [
    'DatabaseDiscussionMessages',
    'DatabaseDiscussionReports',
    'DatabaseInterviewSessions',
    'DatabaseInterviewMessages',
  ];
  function taskPolicies(t: Template): any[] {
    return Object.values(t.findResources('AWS::IAM::Policy')).filter((p) =>
      JSON.stringify(p.Properties.Roles).includes('TaskRole'),
    );
  }
  const taskStatements = (t: Template) =>
    taskPolicies(t).flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);

  test('新しい 4 テーブルの操作は 1 つの文で、8 つの操作だけ（Scan なし）。GSI がないためインデックスを指さない', () => {
    const touching = taskStatements(template).filter((s) =>
      NEW_TABLES.some((id) => JSON.stringify(s.Resource ?? '').includes(id)),
    );
    expect(touching).toHaveLength(1);
    const statement = touching[0];
    expect([statement.Action].flat().sort()).toEqual([
      'dynamodb:BatchGetItem',
      'dynamodb:BatchWriteItem',
      'dynamodb:ConditionCheckItem',
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:Query',
      'dynamodb:UpdateItem',
    ]);
    const resources = JSON.stringify(statement.Resource);
    for (const id of NEW_TABLES) {
      expect(resources).toContain(id);
    }
    expect(resources).not.toContain('/index/');
  });

  test('新しい 4 テーブルの名前をテーブルの参照で渡す（約束 C10、FR9.2）', () => {
    const env = envOf(template);
    const expected: Record<string, string> = {
      DISCUSSION_MESSAGES_TABLE_NAME: 'DatabaseDiscussionMessages',
      DISCUSSION_REPORTS_TABLE_NAME: 'DatabaseDiscussionReports',
      INTERVIEW_SESSIONS_TABLE_NAME: 'DatabaseInterviewSessions',
      INTERVIEW_MESSAGES_TABLE_NAME: 'DatabaseInterviewMessages',
    };
    for (const [name, logicalId] of Object.entries(expected)) {
      expect(JSON.stringify(env[name])).toContain(logicalId);
    }
  });

  test('ALB の無操作タイムアウトは 300 秒のまま（SSE の議論が途中で切れない）', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      LoadBalancerAttributes: Match.arrayWith([
        { Key: 'idle_timeout.timeout_seconds', Value: '300' },
      ]),
    });
  });

  test('タスクロールのポリシーの JSON は 9,000 文字未満（インラインポリシーの上限 10,240 文字）', () => {
    const policies = taskPolicies(template);
    expect(policies).toHaveLength(1);
    const size = JSON.stringify(policies[0].Properties.PolicyDocument).length;
    expect(size).toBeLessThan(9000);
  });
});

describe('AppStack: カスタムデータの登録情報（U7、NFR1.80、NFR1.81）', () => {
  const TABLE = 'DatabaseCustomPersonaData';
  function taskStatements(t: Template): any[] {
    return Object.values(t.findResources('AWS::IAM::Policy'))
      .filter((p) => JSON.stringify(p.Properties.Roles).includes('TaskRole'))
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
  }

  test('CustomPersonaData への操作は 1 つの文で、7 つの操作だけ（Query なし）。GSI がないためインデックスを指さない', () => {
    const touching = taskStatements(template).filter((s) =>
      JSON.stringify(s.Resource ?? '').includes(TABLE),
    );
    expect(touching).toHaveLength(1);
    const statement = touching[0];
    expect([statement.Action].flat().sort()).toEqual([
      'dynamodb:BatchGetItem',
      'dynamodb:ConditionCheckItem',
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:Scan',
      'dynamodb:UpdateItem',
    ]);
    expect(JSON.stringify(statement.Resource)).not.toContain('/index/');
  });

  test('環境変数 CUSTOM_PERSONA_DATA_TABLE_NAME をテーブルの参照で渡す（約束 C10）', () => {
    expect(JSON.stringify(envOf(template).CUSTOM_PERSONA_DATA_TABLE_NAME)).toContain(TABLE);
  });

  test('アンケートの 2 テーブルの OwnerIndex は名前で付けたインデックスの ARN で読める', () => {
    const statements = taskStatements(template).filter((s) =>
      JSON.stringify(s.Resource ?? '').includes('DatabaseSurveys'),
    );
    const actions = statements.flatMap((s) => [s.Action].flat());
    expect(actions).toContain('dynamodb:Query');
    const resources = JSON.stringify(statements.map((s) => s.Resource));
    expect(resources).toContain('/index/OwnerIndex');
    expect(resources).not.toContain('/index/*');
  });
});

test('出力に CloudFront の URL とサービスの名前がある（deploy.sh が読む）', () => {
  const outputs = Object.keys(template.toJSON().Outputs ?? {});
  for (const name of ['AppUrl', 'ClusterName', 'ServiceName']) {
    expect(outputs).toContain(name);
  }
  expect(stack.region).toBe('ap-northeast-1');
});

/** Statements of the IAM policies whose roles' logical IDs start with `rolePrefix`. */
function statementsOfRole(t: Template, rolePrefix: string): any[] {
  return Object.values(t.findResources('AWS::IAM::Policy'))
    .filter((p) =>
      (p.Properties.Roles as { Ref: string }[]).some((r) => String(r.Ref).startsWith(rolePrefix)),
    )
    .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
}
const actionsIn = (statements: any[]) => statements.flatMap((s) => [s.Action].flat() as string[]);
const resourcesText = (s: any) => JSON.stringify(s.Resource);

describe('AppStack: 推論の範囲とモデル ID（閉域対応 U5 BR1.7、BR1.10、BR2.3、BR2.4）', () => {
  /** `bedrock:InvokeModel` を持つ文の Resource（タスクロール）。 */
  function invokeModelResources(t: Template): string[] {
    const statement = statementsOfRole(t, 'AppTaskDefinitionTaskRole').find((s) =>
      ([] as string[]).concat(s.Action).includes('bedrock:InvokeModel'),
    );
    return ([] as unknown[])
      .concat(statement.Resource)
      .map((r) => (typeof r === 'string' ? r : JSON.stringify(r)))
      .sort();
  }

  test('公開の既定: AGENT_MODEL_ID は Haiku 5.5、BEDROCK_MODEL_ID は Sonnet 5.5。INFERENCE_SCOPE はない', () => {
    const env = envOf(template);
    expect(env.AGENT_MODEL_ID).toBe('global.anthropic.claude-haiku-5-5');
    expect(env.BEDROCK_MODEL_ID).toBe('global.anthropic.claude-sonnet-5-5');
    expect(env.SURVEY_MODEL_ID).toBe('global.anthropic.claude-haiku-5-5');
    expect(env).not.toHaveProperty('INFERENCE_SCOPE');
  });

  test('profile なしで作ると parameter の明示したモデル ID を使う（BR1.10）', () => {
    const { template: t } = build({ parameter: { agentModelId: 'global.anthropic.claude-sonnet-5' } });
    expect(envOf(t).AGENT_MODEL_ID).toBe('global.anthropic.claude-sonnet-5');
  });

  test('profile なしでも parameter の inferenceScope=jp を使い、INFERENCE_SCOPE=jp を渡す', () => {
    const { template: t } = build({ parameter: { inferenceScope: 'jp' } });
    const env = envOf(t);
    expect(env.INFERENCE_SCOPE).toBe('jp');
    expect(env.AGENT_MODEL_ID).toBe('jp.anthropic.claude-haiku-5-5');
  });

  test('jp で明示した global.* と基盤モデルの ID の権限をタスクロールに足す（BR2.3、BR2.4）', () => {
    const { template: t } = build({
      parameter: {
        inferenceScope: 'jp',
        bedrockModelId: 'global.anthropic.claude-sonnet-5',
        agentModelId: 'anthropic.claude-haiku-4-5-20251001-v1:0',
      },
    });
    const task = invokeModelResources(t);
    expect(task).toEqual(
      [
        'arn:aws:bedrock:ap-northeast-1::foundation-model/*',
        'arn:aws:bedrock:ap-northeast-3::foundation-model/*',
        'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.*',
        'arn:aws:bedrock:*:123456789012:application-inference-profile/*',
        'arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-sonnet-5',
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5',
        'arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0',
      ].sort(),
    );
    expect(envOf(t).BEDROCK_MODEL_ID).toBe('global.anthropic.claude-sonnet-5');
  });

  test('jp で ARN の形のモデル ID は synth で止まる（BR2.3）', () => {
    expect(() =>
      build({
        parameter: {
          inferenceScope: 'jp',
          agentModelId: 'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.x',
        },
      }),
    ).toThrow(/agentModelId/);
  });
});

// =============================================================================
// アンケートのステートマシン（基盤設計 2.2・3.1・3.2・4 章、BR2.6、BR3.3、BR3.5、BR3.6）
// =============================================================================

function stateMachineOf(t: Template): { logicalId: string; resource: any } {
  const machines = Object.entries(t.findResources('AWS::StepFunctions::StateMachine'));
  expect(machines).toHaveLength(1);
  return { logicalId: machines[0][0], resource: machines[0][1] };
}

function definitionOf(t: Template): any {
  const raw = stateMachineOf(t).resource.Properties.DefinitionString;
  expect(typeof raw).toBe('string');
  return JSON.parse(raw);
}

describe('AppStack: アンケートのステートマシン', () => {
  const definition = definitionOf(template);
  const map = definition.States[definition.StartAt];
  const child = map.ItemProcessor;

  test('Standard、名前は指定しない、RETAIN、ログは ERROR で入出力なし、トレースなし', () => {
    const { resource } = stateMachineOf(template);
    expect(resource.DeletionPolicy).toBe('Retain');
    expect(resource.UpdateReplacePolicy).toBe('Retain');
    expect(resource.Properties.StateMachineType).toBe('STANDARD');
    expect(resource.Properties.StateMachineName).toBeUndefined();
    expect(resource.Properties.LoggingConfiguration).toMatchObject({
      Level: 'ERROR',
      IncludeExecutionData: false,
    });
    expect(resource.Properties.TracingConfiguration?.Enabled ?? false).toBe(false);
  });

  test('実行全体の打ち切りは既定 7,200 秒（BR3.3）', () => {
    expect(definition.TimeoutSeconds).toBe(7200);
  });

  test('Distributed Map: S3 の JSONL を読み、子は Express、並列 64、失敗の許容 100%、結果は S3', () => {
    expect(map.Type).toBe('Map');
    expect(map.ItemReader).toEqual({
      Resource: 'arn:aws:states:::s3:getObject',
      ReaderConfig: { InputType: 'JSONL' },
      Parameters: { 'Bucket.$': '$.bucket', 'Key.$': '$.input_key' },
    });
    expect(child.ProcessorConfig).toEqual({ Mode: 'DISTRIBUTED', ExecutionType: 'EXPRESS' });
    expect(map.MaxConcurrency).toBe(64);
    expect(map.ToleratedFailurePercentage).toBe(100);
    expect(map.ResultWriter).toEqual({
      Resource: 'arn:aws:states:::s3:putObject',
      Parameters: { 'Bucket.$': '$.bucket', 'Prefix.$': '$.result_prefix' },
    });
    expect(map.End).toBe(true);
  });

  test('子の実行: Body が S3 にあるかで分け、bedrock:invokeModel を呼ぶ（BR6.3）', () => {
    const choice = child.States[child.StartAt];
    expect(choice.Type).toBe('Choice');
    expect(choice.Choices).toEqual([
      { Variable: '$.body_s3_uri', IsPresent: true, Next: 'InvokeWithBodyInS3' },
    ]);
    expect(choice.Default).toBe('InvokeWithBody');
    expect(child.States.InvokeWithBody.Parameters).toEqual({
      'ModelId.$': '$.model_id',
      'Body.$': '$.body',
      ContentType: 'application/json',
      Accept: 'application/json',
    });
    expect(child.States.InvokeWithBodyInS3.Parameters).toEqual({
      'ModelId.$': '$.model_id',
      Input: { 'S3Uri.$': '$.body_s3_uri' },
      ContentType: 'application/json',
      Accept: 'application/json',
    });
  });

  test.each(['InvokeWithBody', 'InvokeWithBodyInS3'])(
    '%s: 応答の本文だけを残し、30 秒で打ち切り、一時的なエラーを再試行する（BR3.5）',
    (name) => {
      const task = child.States[name];
      expect(task.Type).toBe('Task');
      expect(task.Resource).toBe('arn:aws:states:::bedrock:invokeModel');
      expect(task.ResultSelector).toEqual({ 'content.$': '$.Body.content' });
      expect(task.TimeoutSeconds).toBe(30);
      expect(task.End).toBe(true);
      expect(task.Retry).toEqual([
        {
          ErrorEquals: [
            'Bedrock.ThrottlingException',
            'Bedrock.ServiceUnavailableException',
            'Bedrock.ModelNotReadyException',
            'Bedrock.InternalServerException',
            'Bedrock.ModelTimeoutException',
            'States.Timeout',
          ],
          IntervalSeconds: 2,
          BackoffRate: 2,
          MaxAttempts: 6,
          MaxDelaySeconds: 16,
          JitterStrategy: 'FULL',
        },
      ]);
    },
  );

  test('再試行の最悪でも Express の 5 分に収まる（NFR3.15）', () => {
    const task = child.States.InvokeWithBody;
    const retry = task.Retry[0];
    let waits = 0;
    let delay = retry.IntervalSeconds;
    for (let i = 0; i < retry.MaxAttempts; i += 1) {
      waits += Math.min(delay, retry.MaxDelaySeconds);
      delay *= retry.BackoffRate;
    }
    expect((retry.MaxAttempts + 1) * task.TimeoutSeconds + waits).toBeLessThan(300);
  });

  test('ステートマシンのロググループは名前を指定せず、保持 1 か月、RETAIN', () => {
    const groups = Object.entries(template.findResources('AWS::Logs::LogGroup')).filter(([id]) =>
      id.startsWith('SurveyRun'),
    );
    expect(groups).toHaveLength(1);
    const [, group] = groups[0];
    expect(group.DeletionPolicy).toBe('Retain');
    expect(group.Properties.LogGroupName).toBeUndefined();
    expect(group.Properties.RetentionInDays).toBe(30);
  });

  test('実行ロールは推論・survey-runs/ の読み書き・自分の実行・ログの出力だけ（基盤設計 3.1）', () => {
    const statements = statementsOfRole(template, 'SurveyRun');
    const actions = new Set(actionsIn(statements));
    expect([...actions].sort()).toEqual(
      [
        'bedrock:InvokeModel',
        's3:GetObject',
        's3:PutObject',
        's3:AbortMultipartUpload',
        's3:ListMultipartUploadParts',
        'states:StartExecution',
        'states:DescribeExecution',
        'logs:CreateLogDelivery',
        'logs:GetLogDelivery',
        'logs:UpdateLogDelivery',
        'logs:DeleteLogDelivery',
        'logs:ListLogDeliveries',
        'logs:PutResourcePolicy',
        'logs:DescribeResourcePolicies',
        'logs:DescribeLogGroups',
      ].sort(),
    );
    for (const forbidden of ['s3:GetObjectVersion', 's3:ListBucket', 'states:StopExecution']) {
      expect(actions.has(forbidden)).toBe(false);
    }
    const read = statements.find((s) => [s.Action].flat().includes('s3:GetObject'));
    expect(resourcesText(read)).toContain('/survey-runs/*');
    expect(resourcesText(read)).not.toContain('/survey-results/');
    const write = statements.find((s) => [s.Action].flat().includes('s3:PutObject'));
    expect(resourcesText(write)).toContain('/survey-runs/*/results/*');
    const invoke = statements.find((s) => [s.Action].flat().includes('bedrock:InvokeModel'));
    expect([invoke.Resource].flat().sort()).toEqual(
      [
        'arn:aws:bedrock:*:123456789012:inference-profile/global.anthropic.claude-haiku-5-5',
        'arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5',
      ].sort(),
    );
    const roles = Object.entries(template.findResources('AWS::IAM::Role')).filter(([id]) =>
      id.startsWith('SurveyRun'),
    );
    expect(roles).toHaveLength(1);
    expect(JSON.stringify(roles[0][1].Properties.AssumeRolePolicyDocument)).toContain(
      'states.amazonaws.com',
    );
  });

  test('タスクロールは自分のステートマシンの開始・確認・停止と、担当者の記録の 4 つの操作（基盤設計 3.2）', () => {
    const task = statementsOfRole(template, 'AppTaskDefinitionTaskRole');
    const machine = stateMachineOf(template).logicalId;
    const start = task.filter((s) => [s.Action].flat().includes('states:StartExecution'));
    expect(start).toHaveLength(1);
    expect(start[0].Resource).toEqual({ Ref: machine });
    const follow = task.filter((s) => [s.Action].flat().includes('states:DescribeExecution'));
    expect(follow).toHaveLength(1);
    expect([follow[0].Action].flat().sort()).toEqual([
      'states:DescribeExecution',
      'states:StopExecution',
    ]);
    expect(resourcesText(follow[0])).toContain(':execution:');
    const lease = task.filter((s) => resourcesText(s).includes('DatabaseSurveyRunLease'));
    expect(lease).toHaveLength(1);
    expect([lease[0].Action].flat().sort()).toEqual([
      'dynamodb:DeleteItem',
      'dynamodb:GetItem',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ]);
    expect(resourcesText(lease[0])).not.toContain('/index/');
  });

  test('タスクロールの Surveys の文に RunQueueIndex を名前で付ける（NFR5.8）', () => {
    const task = statementsOfRole(template, 'AppTaskDefinitionTaskRole');
    const surveys = task.filter((s) => resourcesText(s).includes('DatabaseSurveys'));
    expect(JSON.stringify(surveys)).toContain('/index/RunQueueIndex');
  });

  test('バッチ推論のロール・権限・一覧の API がない（FR8.2、NFR5.10）', () => {
    const roles = Object.keys(template.findResources('AWS::IAM::Role'));
    expect(roles.filter((id) => id.startsWith('BedrockBatchRole'))).toEqual([]);
    const actions = new Set(actionsIn(statementsOfRole(template, 'AppTaskDefinitionTaskRole')));
    for (const removed of [
      'bedrock:CreateModelInvocationJob',
      'bedrock:GetModelInvocationJob',
      'bedrock:StopModelInvocationJob',
      'iam:PassRole',
      'bedrock:ListFoundationModels',
      'bedrock:ListInferenceProfiles',
      'bedrock:ListModelInvocationJobs',
    ]) {
      expect(actions.has(removed)).toBe(false);
    }
  });

  test('アプリへ実行基盤の 4 つの設定を渡し、バッチ推論の 3 つは渡さない（基盤設計 4 章）', () => {
    const env = envOf(template);
    expect(env.SURVEY_STATE_MACHINE_ARN).toEqual({ Ref: stateMachineOf(template).logicalId });
    expect(JSON.stringify(env.SURVEY_RUN_LEASE_TABLE_NAME)).toContain('DatabaseSurveyRunLease');
    expect(env.SURVEY_EXECUTION_TIMEOUT_SECONDS).toBe('7200');
    expect(env.SURVEY_MODEL_ID).toBe('global.anthropic.claude-haiku-5-5');
    for (const removed of [
      'BEDROCK_BATCH_ROLE_ARN',
      'BATCH_INFERENCE_MODEL_ID',
      'BATCH_INFERENCE_S3_PREFIX',
    ]) {
      expect(env).not.toHaveProperty(removed);
    }
  });

  test('並列数と打ち切り時間はパラメータで変えられ、アプリにも同じ値を渡す（FR3.4、NFR3.5）', () => {
    const { template: t } = build({
      parameter: { surveyMaxConcurrency: 32, surveyExecutionTimeoutSeconds: 3600 },
    });
    const d = definitionOf(t);
    expect(d.States[d.StartAt].MaxConcurrency).toBe(32);
    expect(d.TimeoutSeconds).toBe(3600);
    expect(envOf(t).SURVEY_EXECUTION_TIMEOUT_SECONDS).toBe('3600');
  });

  test.each([
    { surveyMaxConcurrency: 0 },
    { surveyMaxConcurrency: 1001 },
    { surveyMaxConcurrency: 1.5 },
    { surveyExecutionTimeoutSeconds: 599 },
    { surveyExecutionTimeoutSeconds: 86401 },
  ])('範囲外の %p は synth で止まる（NFR3.4、NFR3.5）', (parameter) => {
    expect(() => build({ parameter })).toThrow(/survey(MaxConcurrency|ExecutionTimeoutSeconds)/);
  });

  test('jp の範囲ではアンケートのモデルも jp の推論プロファイルと国内の基盤モデル', () => {
    const { template: t } = build({ parameter: { inferenceScope: 'jp' } });
    expect(envOf(t).SURVEY_MODEL_ID).toBe('jp.anthropic.claude-haiku-5-5');
    const invoke = statementsOfRole(t, 'SurveyRun').find((s) =>
      [s.Action].flat().includes('bedrock:InvokeModel'),
    );
    expect([invoke.Resource].flat().sort()).toEqual(
      [
        'arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.anthropic.claude-haiku-5-5',
        'arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-5-5',
        'arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-haiku-5-5',
      ].sort(),
    );
  });
});
