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
