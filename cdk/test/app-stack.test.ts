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

function build(
  options: { context?: Record<string, string>; parameter?: Partial<AppParameter> } = {},
): { stack: AppStack; template: Template } {
  const app = new cdk.App({ context: options.context ?? {} });
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
      'PERSONA_DATASET_BINDINGS_TABLE_NAME',
      'SURVEY_TEMPLATES_TABLE_NAME',
      'SURVEYS_TABLE_NAME',
      'KNOWLEDGE_BASES_TABLE_NAME',
      'PERSONA_KB_BINDINGS_TABLE_NAME',
      'JOBS_TABLE_NAME',
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
    expect(dynamoWrites.length).toBeGreaterThanOrEqual(11);
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

  test('/static/* だけはキャッシュする', () => {
    const behaviors = distribution().CacheBehaviors as any[];
    expect(behaviors).toHaveLength(1);
    expect(behaviors[0].PathPattern).toBe('/static/*');
    expect(behaviors[0].CachePolicyId).toBe(CACHING_OPTIMIZED);
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
    const jobs = tables.DatabaseJobs6F06B67C;
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

test('出力に CloudFront の URL とサービスの名前がある（deploy.sh が読む）', () => {
  const outputs = Object.keys(template.toJSON().Outputs ?? {});
  for (const name of ['AppUrl', 'ClusterName', 'ServiceName']) {
    expect(outputs).toContain(name);
  }
  expect(stack.region).toBe('ap-northeast-1');
});
