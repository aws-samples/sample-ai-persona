import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import { CLOSED_ENDPOINTS, CLOSED_GATEWAY_ENDPOINTS, ClosedNetwork } from '../lib/constructs/closed-network';
import { RESTRICTED_ENDPOINT_SERVICES } from '../lib/endpoint-policies';
import { NAG_REASONS } from '../lib/nag-reasons';
import { describePolicyValue, grantsOf, statementsAllow, TemplateStatement } from './helpers/policy-coverage';
import { Cognito, PASSWORD_POLICY } from '../lib/constructs/cognito';
import { FargateAppService } from '../lib/constructs/fargate-service';
import { ValidationEnvironment } from '../lib/constructs/validation-environment';
import {
  ACCOUNT,
  CLOSED_TEST_VALUES,
  FEATURE_FLAGS,
  REGION,
  synthBoth,
  synthClosed,
  synthPublic,
} from './helpers/stages';
import { exportedRef, exportTargets, flattenReference, pathOf } from './template-paths';

/**
 * 閉域構成の構成部品（U1 閉域、BR3〜BR6、NFR1・NFR3・NFR4）の検査。
 *
 * 構成部品は 1 つのスタックに単独で置いて確かめる。Stage としての組み立て（Data・App の
 * 結線、SG の結線、環境変数）は下の「閉域の Stage」の節で確かめる。
 */

jest.setTimeout(120_000);

/** 閉域のタスクにだけ足す U3 の環境変数（BR5.1）。 */
const CLOSED_FEATURE_KEYS = ['NEMOTRON_SOURCE', 'NEMOTRON_DATASET_KEY', 'DATA_AGENT_ALLOWED', 'ASSET_DELIVERY'];

/** Bedrock 系のインターフェイス型エンドポイントを提供する AZ（infrastructure-design レビュー R-05）。 */
const BEDROCK_ENDPOINT_AZS = ['ap-northeast-1a', 'ap-northeast-1c', 'ap-northeast-1d'];

/**
 * U1 のインターフェイス型エンドポイント 10 種（NFR3.7）。アンケートを Step Functions に移したため、
 * bedrock（コントロールプレーン）を外して states を足した（FR8.4、FR9.1、NFR6.1）。
 */
const INTERFACE_ENDPOINTS = [
  'bedrock-agent-runtime',
  'bedrock-agentcore',
  'bedrock-agentcore-control',
  'bedrock-runtime',
  'cognito-idp',
  'ecr.api',
  'ecr.dkr',
  'logs',
  'secretsmanager',
  'states',
];
/** 検証用 EC2 を作るときだけのエンドポイント（BR6.1）。 */
const VALIDATION_ENDPOINTS = ['ec2messages', 'ssm', 'ssmmessages'];

function newStack(region: string = REGION): cdk.Stack {
  const app = new cdk.App({ context: { ...FEATURE_FLAGS } });
  return new cdk.Stack(app, 'Test', { env: { account: ACCOUNT, region } });
}

function network(stack: cdk.Stack, createTestEnvironment = false): ClosedNetwork {
  return new ClosedNetwork(stack, 'Network', {
    envName: 'dev-closed',
    vpcCidr: '10.0.0.0/16',
    domainName: CLOSED_TEST_VALUES.domainName,
    createTestEnvironment,
  });
}

/** `com.amazonaws.<region>.<name>` of every endpoint of a type, as the short names. */
function endpointNames(template: Template, type: 'Interface' | 'Gateway'): string[] {
  const prefix = `com.amazonaws.${REGION}.`;
  return Object.values(template.findResources('AWS::EC2::VPCEndpoint'))
    .filter((e) => e.Properties.VpcEndpointType === type)
    .map((e) => {
      // ゲートウェイ型の名前は Fn::Join と Ref AWS::Region で出る
      const name = e.Properties.ServiceName;
      const text =
        typeof name === 'string'
          ? name
          : (name['Fn::Join'][1] as any[])
            .map((part) => (part?.Ref === 'AWS::Region' ? REGION : part))
            .join(name['Fn::Join'][0]);
      expect(text.startsWith(prefix)).toBe(true);
      return text.slice(prefix.length);
    })
    .sort();
}

/** Inline egress rules of a security group, without CDK's "disallow all" placeholder. */
function inlineEgress(sg: any): any[] {
  return (sg.Properties.SecurityGroupEgress ?? []).filter(
    (rule: any) => rule.CidrIp !== '255.255.255.255/32',
  );
}

/** Separate ingress / egress resources whose GroupId is the given security group. */
function separateRules(template: Template, type: string, groupLogicalId: string): any[] {
  return Object.values(template.findResources(type))
    .map((r) => r.Properties)
    .filter((p) => JSON.stringify(p.GroupId).includes(`"${groupLogicalId}"`));
}

function logicalIdOf(stack: cdk.Stack, construct: { node: { defaultChild?: unknown } } | cdk.CfnElement): string {
  const element = cdk.CfnElement.isCfnElement(construct)
    ? construct
    : (construct.node.defaultChild as cdk.CfnElement);
  return stack.resolve(element.logicalId) as string;
}

describe('Cognito（createDomain: false、BR3.1・BR3.2）', () => {
  const stack = newStack();
  const cognito = new Cognito(stack, 'Cognito', { envName: 'dev-closed', createDomain: false });
  const template = Template.fromStack(stack);

  test('UserPoolDomain を作らない', () => {
    template.resourceCountIs('AWS::Cognito::UserPoolDomain', 0);
    expect(cognito.domain).toBeUndefined();
  });

  test('メールでの回復を無効にする（AccountRecoverySetting は admin_only）。そのほかは公開と同じ', () => {
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      AccountRecoverySetting: { RecoveryMechanisms: [{ Name: 'admin_only', Priority: 1 }] },
      AdminCreateUserConfig: { AllowAdminCreateUserOnly: true },
      DeletionProtection: 'ACTIVE',
    });
    template.hasResource('AWS::Cognito::UserPool', { DeletionPolicy: 'Retain' });
  });

  test('domainHost を読むとエラー（黙って空文字を返さない）', () => {
    expect(() => cognito.domainHost).toThrow('この User Pool にはドメインがない（閉域構成）');
  });

  test('createDomain を省くと今と同じくドメインを作る', () => {
    const publicStack = newStack();
    const publicCognito = new Cognito(publicStack, 'Cognito', { envName: 'dev' });
    Template.fromStack(publicStack).resourceCountIs('AWS::Cognito::UserPoolDomain', 1);
    expect(publicCognito.domainHost).toContain('.auth.ap-northeast-1.amazoncognito.com');
  });
});

describe('ClosedNetwork（BR4.1〜BR4.4、NFR3.1・NFR3.3・NFR3.7）', () => {
  const stack = newStack();
  const closedNetwork = network(stack);
  const template = Template.fromStack(stack);

  test('NAT・IGW・パブリックサブネットがなく、PRIVATE_ISOLATED のサブネットが 2 つ（NFR3.1）', () => {
    template.resourceCountIs('AWS::EC2::NatGateway', 0);
    template.resourceCountIs('AWS::EC2::InternetGateway', 0);
    template.resourceCountIs('AWS::EC2::VPCGatewayAttachment', 0);
    const subnets = Object.values(template.findResources('AWS::EC2::Subnet'));
    expect(subnets).toHaveLength(2);
    for (const subnet of subnets) {
      expect(subnet.Properties.MapPublicIpOnLaunch).toBe(false);
    }
    expect(closedNetwork.vpc.isolatedSubnets).toHaveLength(2);
    expect(closedNetwork.vpc.publicSubnets).toHaveLength(0);
    expect(closedNetwork.vpc.privateSubnets).toHaveLength(0);
    template.hasResourceProperties('AWS::EC2::VPC', { CidrBlock: '10.0.0.0/16' });
  });

  test('VPC の AZ は Bedrock 系のエンドポイントを提供する AZ に含まれる（レビュー R-05）', () => {
    const azs = Object.values(template.findResources('AWS::EC2::Subnet')).map(
      (s) => s.Properties.AvailabilityZone,
    );
    expect(new Set(azs).size).toBe(2);
    for (const az of azs) {
      expect(BEDROCK_ENDPOINT_AZS).toContain(az);
    }
  });

  test('インターフェイス型エンドポイントは 10 種で、サービス名は com.amazonaws.ap-northeast-1.<名前>（NFR3.7）', () => {
    expect(endpointNames(template, 'Interface')).toEqual(INTERFACE_ENDPOINTS);
  });

  test('インターフェイス型はプライベート DNS 有効・2 つのサブネット・エンドポイントの SG だけ', () => {
    const sgId = logicalIdOf(stack, closedNetwork.endpointSecurityGroup);
    for (const endpoint of Object.values(template.findResources('AWS::EC2::VPCEndpoint')).filter(
      (e) => e.Properties.VpcEndpointType === 'Interface',
    )) {
      expect(endpoint.Properties.PrivateDnsEnabled).toBe(true);
      expect(endpoint.Properties.SubnetIds).toHaveLength(2);
      expect(endpoint.Properties.SecurityGroupIds).toEqual([{ 'Fn::GetAtt': [sgId, 'GroupId'] }]);
    }
  });

  test('ゲートウェイ型は s3・dynamodb の 2 種', () => {
    expect(endpointNames(template, 'Gateway')).toEqual(['dynamodb', 's3']);
  });

  test('エンドポイントの SG に受け入れのルールがなく、外向きもない（BR4.3）', () => {
    const sgId = logicalIdOf(stack, closedNetwork.endpointSecurityGroup);
    const sg = template.findResources('AWS::EC2::SecurityGroup')[sgId];
    expect(sg.Properties.SecurityGroupIngress ?? []).toEqual([]);
    expect(inlineEgress(sg)).toEqual([]);
    expect(separateRules(template, 'AWS::EC2::SecurityGroupIngress', sgId)).toEqual([]);
    expect(separateRules(template, 'AWS::EC2::SecurityGroupEgress', sgId)).toEqual([]);
  });

  test('S3・DynamoDB のプレフィックスリストの ID を東京リージョンの表から引く（NFR3.3）', () => {
    expect(closedNetwork.s3PrefixListId).toBe('pl-61a54008');
    expect(closedNetwork.dynamoDbPrefixListId).toBe('pl-78a54011');
  });

  test('プレフィックスリストの表にないリージョンはエラー（NFR3.3）', () => {
    expect(() => network(newStack('us-west-2'))).toThrow(/prefix list.*us-west-2/);
  });

  test('FQDN の Private Hosted Zone を VPC に関連付けて作る（BR4.4）', () => {
    template.resourceCountIs('AWS::Route53::HostedZone', 1);
    template.hasResourceProperties('AWS::Route53::HostedZone', {
      Name: `${CLOSED_TEST_VALUES.domainName}.`,
      VPCs: [Match.objectLike({ VPCRegion: REGION })],
    });
  });

  test('検証用を作るときだけ ssm・ssmmessages・ec2messages が加わる（BR6.1）', () => {
    const withTest = newStack();
    network(withTest, true);
    expect(endpointNames(Template.fromStack(withTest), 'Interface')).toEqual(
      [...INTERFACE_ENDPOINTS, ...VALIDATION_ENDPOINTS].sort(),
    );
  });
});

describe('FargateAppService（閉域の入口、BR5.1・BR5.2、NFR4.1・NFR4.2）', () => {
  const stack = newStack();
  const closedNetwork = network(stack);
  const service = new FargateAppService(stack, 'App', {
    vpc: closedNetwork.vpc,
    repository: new ecr.Repository(stack, 'Repository'),
    imageTag: 'abc1234',
    cpu: 1024,
    memoryLimitMiB: 4096,
    desiredCount: 1,
    logGroup: new logs.LogGroup(stack, 'Logs'),
    environment: {},
    secrets: {},
    ingress: { kind: 'closed', certificateArn: CLOSED_TEST_VALUES.certificateArn },
    subnetType: ec2.SubnetType.PRIVATE_ISOLATED,
  });
  const template = Template.fromStack(stack);

  test('HTTPS 443 のリスナーだけで、HTTP 80 がない。証明書と RECOMMENDED_TLS（TLS 1.3・1.2）', () => {
    const listeners = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::Listener'));
    expect(listeners).toHaveLength(1);
    expect(listeners[0].Properties).toMatchObject({
      Port: 443,
      Protocol: 'HTTPS',
      Certificates: [{ CertificateArn: CLOSED_TEST_VALUES.certificateArn }],
      SslPolicy: 'ELBSecurityPolicy-TLS13-1-2-2021-06',
    });
  });

  test('内部 ALB で、無操作のタイムアウトは 300 秒', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', {
      Scheme: 'internal',
      LoadBalancerAttributes: Match.arrayWith([{ Key: 'idle_timeout.timeout_seconds', Value: '300' }]),
    });
  });

  test('ターゲットは HTTP 80 の /health（TLS は ALB で終わる、BR5.2）', () => {
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::TargetGroup', {
      Port: 80,
      Protocol: 'HTTP',
      HealthCheckPath: '/health',
    });
  });

  test('ALB とサービスのサブネットは PRIVATE_ISOLATED', () => {
    const isolated = closedNetwork.vpc.isolatedSubnets.map((s) => ({ Ref: logicalIdOf(stack, s.node.defaultChild as cdk.CfnElement) }));
    template.hasResourceProperties('AWS::ElasticLoadBalancingV2::LoadBalancer', { Subnets: isolated });
    template.hasResourceProperties('AWS::ECS::Service', {
      NetworkConfiguration: { AwsvpcConfiguration: Match.objectLike({ Subnets: isolated, AssignPublicIp: 'DISABLED' }) },
    });
  });

  test('CloudFront のプレフィックスリストを引かない（ALB の SG の受け入れなし）', () => {
    expect(JSON.stringify(template.toJSON())).not.toContain('pl-58a04531');
    const albSg = template.findResources('AWS::EC2::SecurityGroup')[
      logicalIdOf(stack, service.loadBalancerSecurityGroup)
    ];
    expect(albSg.Properties.SecurityGroupIngress ?? []).toEqual([]);
  });

  test('タスクの SG は外向きが空（ルールは App スタックが足す）。受け入れは ALB からの 80 だけ', () => {
    const taskSgId = logicalIdOf(stack, service.taskSecurityGroup);
    const albSgId = logicalIdOf(stack, service.loadBalancerSecurityGroup);
    const taskSg = template.findResources('AWS::EC2::SecurityGroup')[taskSgId];
    expect(inlineEgress(taskSg)).toEqual([]);
    expect(separateRules(template, 'AWS::EC2::SecurityGroupEgress', taskSgId)).toEqual([]);
    const ingress = separateRules(template, 'AWS::EC2::SecurityGroupIngress', taskSgId);
    expect(ingress).toEqual([
      expect.objectContaining({
        FromPort: 80,
        ToPort: 80,
        SourceSecurityGroupId: { 'Fn::GetAtt': [albSgId, 'GroupId'] },
      }),
    ]);
    template.hasResourceProperties('AWS::ECS::Service', {
      NetworkConfiguration: {
        AwsvpcConfiguration: Match.objectLike({
          SecurityGroups: [{ 'Fn::GetAtt': [taskSgId, 'GroupId'] }],
        }),
      },
    });
  });
});

describe('ValidationEnvironment（BR6.2、NFR1.4、NFR3.5）', () => {
  const stack = newStack();
  const closedNetwork = network(stack, true);
  const validation = new ValidationEnvironment(stack, 'Validation', { vpc: closedNetwork.vpc });
  const template = Template.fromStack(stack);

  test('Windows Server 2022（日本語）の t3.medium で、パブリック IP なし', () => {
    template.resourceCountIs('AWS::EC2::Instance', 1);
    template.hasResourceProperties('AWS::EC2::Instance', {
      InstanceType: 't3.medium',
      NetworkInterfaces: [Match.objectLike({ AssociatePublicIpAddress: false })],
    });
    const parameters = JSON.stringify(template.toJSON().Parameters ?? {});
    expect(parameters).toContain('/aws/service/ami-windows-latest/Windows_Server-2022-Japanese-Full-Base');
  });

  test('閉域のサブネットに置く', () => {
    const isolated = closedNetwork.vpc.isolatedSubnets.map((s) => ({
      Ref: logicalIdOf(stack, s.node.defaultChild as cdk.CfnElement),
    }));
    const instance = Object.values(template.findResources('AWS::EC2::Instance'))[0];
    expect(isolated).toContainEqual(instance.Properties.NetworkInterfaces[0].SubnetId);
  });

  test('ロールの管理ポリシーは AmazonSSMManagedInstanceCore だけで、インラインの権限がない（NFR1.4）', () => {
    const roleId = logicalIdOf(stack, validation.role.node.defaultChild as cdk.CfnElement);
    const role = template.findResources('AWS::IAM::Role')[roleId];
    expect(JSON.stringify(role.Properties.ManagedPolicyArns)).toContain(
      ':iam::aws:policy/AmazonSSMManagedInstanceCore',
    );
    expect(role.Properties.ManagedPolicyArns).toHaveLength(1);
    expect(role.Properties.Policies).toBeUndefined();
    const attached = Object.values(template.findResources('AWS::IAM::Policy')).filter((p) =>
      JSON.stringify(p.Properties.Roles).includes(roleId),
    );
    expect(attached).toEqual([]);
  });

  test('SG の受け入れがなく、外向きは KMS の 2 つの宛先への 1688 だけ（443 は App スタックが足す）', () => {
    const sgId = logicalIdOf(stack, validation.securityGroup);
    const sg = template.findResources('AWS::EC2::SecurityGroup')[sgId];
    expect(sg.Properties.SecurityGroupIngress ?? []).toEqual([]);
    expect(separateRules(template, 'AWS::EC2::SecurityGroupIngress', sgId)).toEqual([]);
    expect(
      inlineEgress(sg)
        .map((r: any) => `${r.CidrIp} ${r.IpProtocol} ${r.FromPort}-${r.ToPort}`)
        .sort(),
    ).toEqual(['169.254.169.250/32 tcp 1688-1688', '169.254.169.251/32 tcp 1688-1688']);
  });

  test('キーペアを作る（秘密鍵は SSM パラメータ。Fleet Manager の RDP の認証に使う）', () => {
    template.resourceCountIs('AWS::EC2::KeyPair', 1);
  });
});

// ---------------------------------------------------------------------------
// 閉域の Stage（BR2.1、BR3.3〜BR3.6、BR4.4、BR5.3〜BR5.5、NFR1.1、NFR3.2・NFR3.6、NFR4.4・NFR4.5）
// ---------------------------------------------------------------------------

const CLOSED = 'AIPersonaDevClosed';

function containerOf(template: Template): any {
  const definitions = Object.values(template.findResources('AWS::ECS::TaskDefinition'));
  expect(definitions).toHaveLength(1);
  return definitions[0].Properties.ContainerDefinitions[0];
}

function environmentOf(template: Template): Record<string, unknown> {
  return Object.fromEntries(
    (containerOf(template).Environment as { Name: string; Value: unknown }[]).map((e) => [e.Name, e.Value]),
  );
}

/** Short names of the security groups of the closed App stack, keyed by logical ID. */
function securityGroupNames(template: Template): Map<string, string> {
  const names: Record<string, string> = {
    [`${CLOSED}/App/App/AlbSecurityGroup/Resource`]: 'ALB',
    [`${CLOSED}/App/App/TaskSecurityGroup/Resource`]: 'Task',
    [`${CLOSED}/App/Network/EndpointSecurityGroup/Resource`]: 'Endpoint',
    [`${CLOSED}/App/Validation/SecurityGroup/Resource`]: 'Validation',
  };
  const byLogicalId = new Map<string, string>();
  for (const [logicalId, sg] of Object.entries(template.findResources('AWS::EC2::SecurityGroup'))) {
    const name = names[pathOf(sg)];
    expect(name).toBeDefined();
    byLogicalId.set(logicalId, name);
  }
  return byLogicalId;
}

/** Every security group rule of the closed App stack as `<SG> <in|out> <peer> <port>`. */
function securityGroupRules(template: Template): string[] {
  const names = securityGroupNames(template);
  const peerOf = (rule: any): string => {
    const sg = rule.SourceSecurityGroupId ?? rule.DestinationSecurityGroupId;
    if (sg) {
      return names.get(sg['Fn::GetAtt'][0]) ?? JSON.stringify(sg);
    }
    return rule.CidrIp ?? rule.SourcePrefixListId ?? rule.DestinationPrefixListId ?? JSON.stringify(rule);
  };
  const line = (group: string, direction: string, rule: any) =>
    `${group} ${direction} ${peerOf(rule)} ${rule.IpProtocol} ${rule.FromPort}`;
  const rules: string[] = [];
  for (const [logicalId, sg] of Object.entries(template.findResources('AWS::EC2::SecurityGroup'))) {
    const group = names.get(logicalId)!;
    for (const rule of sg.Properties.SecurityGroupIngress ?? []) {
      rules.push(line(group, 'in', rule));
    }
    for (const rule of inlineEgress(sg)) {
      rules.push(line(group, 'out', rule));
    }
  }
  for (const [type, direction] of [
    ['AWS::EC2::SecurityGroupIngress', 'in'],
    ['AWS::EC2::SecurityGroupEgress', 'out'],
  ] as const) {
    for (const resource of Object.values(template.findResources(type))) {
      const group = names.get(resource.Properties.GroupId['Fn::GetAtt'][0]) ?? 'unknown';
      rules.push(line(group, direction, resource.Properties));
    }
  }
  return rules.sort();
}

describe('閉域の Stage AIPersonaDevClosed（検証用 EC2 あり・許可 CIDR 1 つ）', () => {
  const { app, stage, templates } = synthClosed();
  const data = templates.Data;
  const appStack = templates.App;
  const targets = new Map([...exportTargets(data.toJSON()), ...exportTargets(appStack.toJSON())]);

  test('Data と App の 2 スタックだけで、Edge がない。App は Data にだけ依存する（BR2.1）', () => {
    expect(Object.keys(templates).sort()).toEqual(['App', 'Data']);
    expect(stage.edge).toBeUndefined();
    expect(stage.app.dependencies.map((d) => d.node.id)).toEqual(['Data']);
    const assembly = app.synth();
    const names = assembly.nestedAssemblies.flatMap((n) => n.nestedAssembly.stacks.map((s) => s.stackName));
    expect(names.sort()).toEqual([`${CLOSED}-App`, `${CLOSED}-Data`]);
  });

  test('CloudFront・VPC オリジン・Managed Login のブランディング・Cognito のドメインを作らない（BR5.5、BR3.1）', () => {
    appStack.resourceCountIs('AWS::CloudFront::Distribution', 0);
    appStack.resourceCountIs('AWS::CloudFront::VpcOrigin', 0);
    appStack.resourceCountIs('AWS::Cognito::ManagedLoginBranding', 0);
    data.resourceCountIs('AWS::Cognito::UserPoolDomain', 0);
  });

  test('環境変数: AUTH_MODE=admin_api、APP_BASE_URL=https://<FQDN>、パスワードの条件 5 つ、COGNITO_DOMAIN なし（BR3.6）', () => {
    const env = environmentOf(appStack);
    expect(env.AUTH_MODE).toBe('admin_api');
    expect(env.APP_BASE_URL).toBe(`https://${CLOSED_TEST_VALUES.domainName}`);
    expect(env).not.toHaveProperty('COGNITO_DOMAIN');
    expect({
      PASSWORD_MIN_LENGTH: env.PASSWORD_MIN_LENGTH,
      PASSWORD_REQUIRE_UPPERCASE: env.PASSWORD_REQUIRE_UPPERCASE,
      PASSWORD_REQUIRE_LOWERCASE: env.PASSWORD_REQUIRE_LOWERCASE,
      PASSWORD_REQUIRE_DIGITS: env.PASSWORD_REQUIRE_DIGITS,
      PASSWORD_REQUIRE_SYMBOLS: env.PASSWORD_REQUIRE_SYMBOLS,
    }).toEqual({
      PASSWORD_MIN_LENGTH: String(PASSWORD_POLICY.minLength),
      PASSWORD_REQUIRE_UPPERCASE: String(PASSWORD_POLICY.requireUppercase),
      PASSWORD_REQUIRE_LOWERCASE: String(PASSWORD_POLICY.requireLowercase),
      PASSWORD_REQUIRE_DIGITS: String(PASSWORD_POLICY.requireDigits),
      PASSWORD_REQUIRE_SYMBOLS: String(PASSWORD_POLICY.requireSymbols),
    });
    // ログインに要るほかの値は公開と同じく渡す（アプリの _ADMIN_API_REQUIRED_SETTINGS）
    for (const name of ['COGNITO_USER_POOL_ID', 'COGNITO_CLIENT_ID', 'SESSIONS_TABLE_NAME']) {
      expect(env).toHaveProperty(name);
    }
    const secrets = (containerOf(appStack).Secrets as { Name: string }[]).map((s) => s.Name).sort();
    expect(secrets).toEqual(['COGNITO_CLIENT_SECRET', 'LOGIN_COOKIE_SIGNING_KEY', 'TOKEN_ENCRYPTION_KEYS']);
  });

  test('公開の環境変数のうち、ログイン方式に関わらないものは同じキーを持つ', () => {
    const publicEnv = environmentOf(synthPublic().templates.App);
    const closedEnv = environmentOf(appStack);
    const authKeys = ['AUTH_MODE', 'COGNITO_DOMAIN'];
    const passwordKeys = Object.keys(closedEnv).filter((k) => k.startsWith('PASSWORD_'));
    // U3 の閉域だけの値（NEMOTRON_SOURCE など）は下の別のテストで確かめる
    const closedOnly = [...passwordKeys, ...CLOSED_FEATURE_KEYS];
    expect(Object.keys(closedEnv).filter((k) => !closedOnly.includes(k)).sort()).toEqual(
      Object.keys(publicEnv).filter((k) => k !== 'COGNITO_DOMAIN').sort(),
    );
    expect(authKeys.filter((k) => k in publicEnv)).toEqual(authKeys);
  });

  test('閉域のタスクに U3 の環境変数 3 つ（キーは既定）。公開のタスクにはない（BR5.1、BR5.2）', () => {
    const env = environmentOf(appStack);
    expect({
      NEMOTRON_SOURCE: env.NEMOTRON_SOURCE,
      NEMOTRON_DATASET_KEY: env.NEMOTRON_DATASET_KEY,
      DATA_AGENT_ALLOWED: env.DATA_AGENT_ALLOWED,
    }).toEqual({
      NEMOTRON_SOURCE: 's3',
      NEMOTRON_DATASET_KEY: 'persona-dataset/nemotron-personas-japan.parquet',
      DATA_AGENT_ALLOWED: 'false',
    });
    expect(env.ENABLE_ADDITIONAL_PERSONA_MODELS).toBe('false');
    expect(env).not.toHaveProperty('ENABLE_DATA_AGENT');
    const publicEnv = environmentOf(synthPublic().templates.App);
    for (const key of CLOSED_FEATURE_KEYS) {
      expect(publicEnv).not.toHaveProperty(key);
    }
  });

  test('閉域のタスクに ASSET_DELIVERY=proxy。公開のタスクにはない（閉域対応 U4 BR5.1、BR5.2）', () => {
    expect(environmentOf(appStack).ASSET_DELIVERY).toBe('proxy');
    expect(environmentOf(synthPublic().templates.App)).not.toHaveProperty('ASSET_DELIVERY');
  });

  test('App クライアントは管理 API のログインだけ（OAuth なし・シークレットあり・失効あり・有効期間は公開と同じ、BR3.3）', () => {
    const clients = Object.values(appStack.findResources('AWS::Cognito::UserPoolClient'));
    expect(clients).toHaveLength(1);
    const client = clients[0].Properties;
    expect([...client.ExplicitAuthFlows].sort()).toEqual([
      'ALLOW_ADMIN_USER_PASSWORD_AUTH',
      'ALLOW_REFRESH_TOKEN_AUTH',
    ]);
    expect(client.AllowedOAuthFlows).toBeUndefined();
    expect(client.AllowedOAuthScopes).toBeUndefined();
    expect(client.CallbackURLs).toBeUndefined();
    expect(client.AllowedOAuthFlowsUserPoolClient).toBe(false);
    expect(client.GenerateSecret).toBe(true);
    expect(client.EnableTokenRevocation).toBe(true);
    expect(client.PreventUserExistenceErrors).toBe('ENABLED');
    const publicClient = Object.values(
      synthPublic().templates.App.findResources('AWS::Cognito::UserPoolClient'),
    )[0].Properties;
    for (const key of ['AccessTokenValidity', 'IdTokenValidity', 'RefreshTokenValidity', 'TokenValidityUnits']) {
      expect(client[key]).toEqual(publicClient[key]);
    }
  });

  test('タスクロールの AdminInitiateAuth・AdminRespondToAuthChallenge は閉域の User Pool の ARN だけ（BR3.4、NFR1.1）', () => {
    const statements = Object.values(appStack.findResources('AWS::IAM::Policy')).flatMap(
      (p) => p.Properties.PolicyDocument.Statement as any[],
    );
    const userPoolArn = exportedRef(`${CLOSED}/Data/Cognito/UserPool/Resource`, 'Arn');
    for (const action of ['cognito-idp:AdminInitiateAuth', 'cognito-idp:AdminRespondToAuthChallenge']) {
      const holders = statements.filter((s) => [s.Action].flat().includes(action));
      expect(holders).toHaveLength(1);
      expect([holders[0].Resource].flat().map((r) => flattenReference(r, targets))).toEqual([userPoolArn]);
    }
  });

  test('SG の結線が security-design.md §3 の表のとおり（NFR3.2・NFR3.6、NFR4.4・NFR4.5、BR5.3・BR5.4）', () => {
    expect(securityGroupRules(appStack)).toEqual(
      [
        'ALB in 10.10.0.0/16 tcp 443',
        'ALB in Validation tcp 443',
        'ALB out Task tcp 80',
        'Task in ALB tcp 80',
        'Task out Endpoint tcp 443',
        'Task out pl-61a54008 tcp 443',
        'Task out pl-78a54011 tcp 443',
        'Endpoint in Task tcp 443',
        'Endpoint in Validation tcp 443',
        'Validation out ALB tcp 443',
        'Validation out Endpoint tcp 443',
        'Validation out 169.254.169.250/32 tcp 1688',
        'Validation out 169.254.169.251/32 tcp 1688',
      ].sort(),
    );
  });

  test('Private Hosted Zone の頂点に ALB への A の Alias を置く（BR4.4）', () => {
    appStack.hasResourceProperties('AWS::Route53::RecordSet', {
      Name: `${CLOSED_TEST_VALUES.domainName}.`,
      Type: 'A',
      AliasTarget: Match.objectLike({
        DNSName: Match.anyValue(),
        HostedZoneId: { 'Fn::GetAtt': [Match.stringLikeRegexp('^AppAlb'), 'CanonicalHostedZoneID'] },
      }),
    });
  });

  test('ECS のサービスは、すべての VPC エンドポイントとタスクの SG のルールができてから作る（起動時の GetMemory が通るように）', () => {
    const resources = appStack.toJSON().Resources as Record<string, any>;
    const [serviceId, service] = Object.entries(resources).find(([, r]) => r.Type === 'AWS::ECS::Service')!;
    const dependsOn = new Set<string>(service.DependsOn ?? []);
    const taskSg = service.Properties.NetworkConfiguration.AwsvpcConfiguration.SecurityGroups[0]['Fn::GetAtt'][0];

    const endpoints = Object.keys(resources).filter((id) => resources[id].Type === 'AWS::EC2::VPCEndpoint');
    // タスクの SG が片側にある SG どうしのルール（エンドポイントへの 443、ゲートウェイ型への 443）
    const taskRules = Object.keys(resources).filter((id) => {
      const r = resources[id];
      if (r.Type !== 'AWS::EC2::SecurityGroupIngress' && r.Type !== 'AWS::EC2::SecurityGroupEgress') return false;
      return JSON.stringify(r.Properties).includes(`"${taskSg}"`) && Number(r.Properties.FromPort) === 443;
    });

    expect(endpoints.length).toBe(INTERFACE_ENDPOINTS.length + VALIDATION_ENDPOINTS.length + 2);
    expect(taskRules.length).toBeGreaterThan(0);
    for (const id of [...endpoints, ...taskRules]) {
      expect({ serviceId, missing: dependsOn.has(id) ? undefined : id }).toEqual({ serviceId, missing: undefined });
    }
  });

  test('出力は公開と同じ名前（AppUrl=https://<FQDN>、ClusterName、ServiceName。Data は EcrRepositoryUri・UserPoolId）', () => {
    const outputs = appStack.toJSON().Outputs as Record<string, any>;
    expect(outputs.AppUrl.Value).toBe(`https://${CLOSED_TEST_VALUES.domainName}`);
    expect(outputs).toHaveProperty('ClusterName');
    expect(outputs).toHaveProperty('ServiceName');
    const dataOutputs = data.toJSON().Outputs as Record<string, any>;
    for (const name of ['EcrRepositoryUri', 'UserPoolId', 'AgentCoreMemoryId']) {
      expect(dataOutputs).toHaveProperty(name);
    }
  });

  test('Data スタックの AgentCore Memory の名前に dev_closed が入る（公開の dev と重ならない）', () => {
    data.hasResourceProperties('AWS::BedrockAgentCore::Memory', {
      Name: 'AIPersona_v1_dev_closed_memory',
    });
  });

  test('NAT・IGW がなく、検証用 EC2 は 1 台（BR4.1、BR6.1）', () => {
    appStack.resourceCountIs('AWS::EC2::NatGateway', 0);
    appStack.resourceCountIs('AWS::EC2::InternetGateway', 0);
    appStack.resourceCountIs('AWS::EC2::Instance', 1);
  });
});

describe('閉域の Stage（検証用 EC2 なし・許可 CIDR 2 つ）', () => {
  const { templates } = synthClosed({
    createTestEnvironment: false,
    allowedCidrs: ['10.10.0.0/16', '192.168.10.0/24'],
  });

  test('検証用 EC2・SSM のエンドポイントを作らず、ALB は許可 CIDR からの 443 だけを受ける（BR6.1、BR5.3）', () => {
    templates.App.resourceCountIs('AWS::EC2::Instance', 0);
    expect(endpointNames(templates.App, 'Interface')).toEqual(INTERFACE_ENDPOINTS);
    const rules = securityGroupRules(templates.App);
    expect(rules.filter((r) => r.startsWith('ALB in'))).toEqual([
      'ALB in 10.10.0.0/16 tcp 443',
      'ALB in 192.168.10.0/24 tcp 443',
    ]);
    expect(rules.some((r) => r.includes('Validation'))).toBe(false);
  });
});

/** `bedrock:InvokeModel` を持つ文の、ロールの論理 ID の接頭辞 → Resource の一覧。 */
function invokeModelResources(template: Template): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  for (const policy of Object.values(template.findResources('AWS::IAM::Policy'))) {
    const roles = (policy.Properties.Roles as { Ref: string }[]).map((r) => r.Ref);
    for (const statement of policy.Properties.PolicyDocument.Statement as any[]) {
      const actions = ([] as string[]).concat(statement.Action);
      if (!actions.includes('bedrock:InvokeModel')) {
        continue;
      }
      const resources = ([] as unknown[])
        .concat(statement.Resource)
        .map((r) => (typeof r === 'string' ? r : JSON.stringify(r)));
      for (const role of roles) {
        const key = role.startsWith('SurveyRun') ? 'survey' : 'task';
        result[key] = resources.sort();
      }
    }
  }
  return result;
}

const JP_INFERENCE_ARNS = [
  `arn:aws:bedrock:ap-northeast-1::foundation-model/*`,
  `arn:aws:bedrock:ap-northeast-3::foundation-model/*`,
  `arn:aws:bedrock:${REGION}:${ACCOUNT}:inference-profile/jp.*`,
  `arn:aws:bedrock:*:${ACCOUNT}:application-inference-profile/*`,
].sort();

describe('推論の範囲 jp（閉域はコンテキスト closedInferenceScope、閉域対応 U5 BR1.7、BR2.2、BR2.4）', () => {
  const closedJp = synthClosed({}, { closedInferenceScope: 'jp' });

  test('閉域の jp: INFERENCE_SCOPE=jp と jp の既定のモデル ID を渡す', () => {
    const env = environmentOf(closedJp.templates.App);
    expect(env.INFERENCE_SCOPE).toBe('jp');
    expect(env.BEDROCK_MODEL_ID).toBe('jp.anthropic.claude-haiku-5-5');
    expect(env.AGENT_MODEL_ID).toBe('jp.anthropic.claude-haiku-5-5');
    expect(env.SURVEY_MODEL_ID).toBe('jp.anthropic.claude-haiku-5-5');
  });

  test('閉域の jp: タスクロールは国内の一覧、ステートマシンはアンケートの jp のモデルだけ', () => {
    const resources = invokeModelResources(closedJp.templates.App);
    expect(resources.task).toEqual(JP_INFERENCE_ARNS);
    expect(resources.survey).toEqual(
      [
        `arn:aws:bedrock:${REGION}:${ACCOUNT}:inference-profile/jp.anthropic.claude-haiku-5-5`,
        'arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-5-5',
        'arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-haiku-5-5',
      ].sort(),
    );
  });

  test('閉域の jp: cdk-nag の違反がない（IAM5 の抑止も同じ一覧から作る）', () => {
    expect(() => closedJp.app.synth()).not.toThrow();
  });

  test('閉域の global（既定）には INFERENCE_SCOPE を渡さない', () => {
    expect(environmentOf(synthClosed().templates.App)).not.toHaveProperty('INFERENCE_SCOPE');
  });

  test('公開の jp も INFERENCE_SCOPE=jp と国内の一覧。公開の既定には INFERENCE_SCOPE がない', () => {
    const publicJp = synthPublic({}, { inferenceScope: 'jp' });
    expect(environmentOf(publicJp.templates.App).INFERENCE_SCOPE).toBe('jp');
    expect(invokeModelResources(publicJp.templates.App).task).toEqual(JP_INFERENCE_ARNS);
    expect(() => publicJp.app.synth()).not.toThrow();
    expect(environmentOf(synthPublic().templates.App)).not.toHaveProperty('INFERENCE_SCOPE');
  });

  test('jp で追加のモデルを有効にした公開の Stage は synth で止まる（BR1.4）', () => {
    expect(() => synthPublic({ enableAdditionalPersonaModels: true }, { inferenceScope: 'jp' })).toThrow(
      /inferenceScope=jp.*enableAdditionalPersonaModels/,
    );
  });

  test('不正な範囲は synth で止まる（BR1.2）', () => {
    expect(() => synthClosed({}, { closedInferenceScope: 'us' })).toThrow(/inferenceScope.*"us"/);
  });
});

describe('CLI で渡した範囲は、対象の Stage だけに効く（閉域対応 U5 BR1.1・BR1.9）', () => {
  test('公開は global で追加のモデルを有効、閉域は jp: 両方を組み立てても synth が通る', () => {
    const { app, publicStage, closedStage } = synthBoth(
      { enableAdditionalPersonaModels: true },
      { closedInferenceScope: 'jp' },
    );
    expect(environmentOf(publicStage.templates.App)).not.toHaveProperty('INFERENCE_SCOPE');
    expect(environmentOf(closedStage.templates.App).INFERENCE_SCOPE).toBe('jp');
    expect(() => app.synth()).not.toThrow();
  });

  test('閉域の Stage は -c inferenceScope を読まない（公開の Stage だけに効く）', () => {
    const { publicStage, closedStage } = synthBoth({}, { inferenceScope: 'jp' });
    expect(environmentOf(publicStage.templates.App).INFERENCE_SCOPE).toBe('jp');
    expect(environmentOf(closedStage.templates.App)).not.toHaveProperty('INFERENCE_SCOPE');
  });

  test('公開の Stage は -c closedInferenceScope を読まない', () => {
    const { publicStage } = synthBoth({}, { closedInferenceScope: 'jp' });
    expect(environmentOf(publicStage.templates.App)).not.toHaveProperty('INFERENCE_SCOPE');
  });
});

describe('閉域の Nemotron のキー（コンテキスト closedNemotronDatasetKey、U3 BR5.1・BR5.3）', () => {
  test('指定のキーをタスクに渡す', () => {
    const { templates } = synthClosed({}, { closedNemotronDatasetKey: 'closed/nemotron.parquet' });
    expect(environmentOf(templates.App).NEMOTRON_DATASET_KEY).toBe('closed/nemotron.parquet');
  });

  test.each(['my key.parquet', 'a+b.parquet'])('空白か + を含むキー %p で synth が止まる', (key) => {
    expect(() => synthClosed({}, { closedNemotronDatasetKey: key })).toThrow(/closedNemotronDatasetKey/);
  });
});

// ---------------------------------------------------------------------------
// 閉域のネットワークの仕上げ（閉域対応 U2: BR1.1、BR1.4、BR1.6、BR1.7、BR1.9、BR2.2〜BR2.4、
// BR3.1、BR3.4、BR3.5、BR4.1）
// ---------------------------------------------------------------------------

/** Every VPC endpoint of a template, keyed by the short service name. */
function endpointsByService(template: Template): Record<string, any> {
  const prefix = `com.amazonaws.${REGION}.`;
  const result: Record<string, any> = {};
  for (const endpoint of Object.values(template.findResources('AWS::EC2::VPCEndpoint'))) {
    const name = endpoint.Properties.ServiceName;
    const text =
      typeof name === 'string'
        ? name
        : (name['Fn::Join'][1] as any[]).map((part) => (part?.Ref === 'AWS::Region' ? REGION : part)).join(name['Fn::Join'][0]);
    result[text.slice(prefix.length)] = endpoint;
  }
  return result;
}

const statementsOfEndpoint = (endpoint: any): TemplateStatement[] =>
  (endpoint.Properties.PolicyDocument?.Statement ?? []) as TemplateStatement[];

/** The isolated subnets of the closed App stack, as `Ref`s keyed by AZ. */
function subnetRefsByAz(template: Template): Record<string, { Ref: string }> {
  return Object.fromEntries(
    Object.entries(template.findResources('AWS::EC2::Subnet')).map(([id, subnet]) => [
      subnet.Properties.AvailabilityZone,
      { Ref: id },
    ]),
  );
}

/** The endpoint services an action goes through (undefined for services the 12 do not cover). */
function endpointServicesOf(action: string): string[] | undefined {
  const [prefix, name] = action.split(':');
  switch (prefix) {
    case 's3':
    case 'dynamodb':
    case 'logs':
    case 'secretsmanager':
    case 'cognito-idp':
      return [prefix];
    case 'ecr':
      return ['ecr.api', 'ecr.dkr'];
    case 'bedrock-agentcore':
      return ['bedrock-agentcore', 'bedrock-agentcore-control'];
    case 'bedrock':
      if (name.startsWith('InvokeModel')) return ['bedrock-runtime'];
      if (name === 'Retrieve') return ['bedrock-agent-runtime'];
      // GetFoundationModel・GetInferenceProfile は InvokeModel と同じ文に残るコントロールプレーンの
      // 操作で、アプリのコードは呼ばない。bedrock のエンドポイントは外した（FR8.4、基盤設計 Q1 の A）
      return CONTROL_PLANE_ACTIONS_NOT_CALLED.includes(action) ? [] : undefined;
    case 'states':
      // アンケートの実行の開始・確認・停止（FR9.1、NFR6.1）
      return ['states'];
    default:
      return undefined;
  }
}

/** Services the 12 endpoints do not cover (BR1.4: excluded from the coverage check). */
const NOT_THROUGH_ENDPOINTS = ['kms'];
/** Bedrock control plane actions on the task role that the app never calls (no endpoint). */
const CONTROL_PLANE_ACTIONS_NOT_CALLED = ['bedrock:GetFoundationModel', 'bedrock:GetInferenceProfile'];

/** The statements of the task role and the execution role policies. */
function roleStatements(template: Template): { role: string; statement: TemplateStatement }[] {
  return Object.values(template.findResources('AWS::IAM::Policy')).flatMap((policy) => {
    const roles = JSON.stringify(policy.Properties.Roles);
    const role = roles.includes('TaskRole') ? 'task' : roles.includes('ExecutionRole') ? 'execution' : undefined;
    if (!role) {
      return [];
    }
    return (policy.Properties.PolicyDocument.Statement as TemplateStatement[]).map((statement) => ({ role, statement }));
  });
}

/** `<role> <action> <resource>` of every grant that no corresponding endpoint policy allows (BR1.4). */
function uncoveredGrants(template: Template): string[] {
  const endpoints = endpointsByService(template);
  const missing: string[] = [];
  for (const { role, statement } of roleStatements(template)) {
    for (const { action, resource } of grantsOf(statement)) {
      const services = endpointServicesOf(action);
      if (!services) {
        expect({ action, excluded: NOT_THROUGH_ENDPOINTS.includes(action.split(':')[0]) }).toEqual({ action, excluded: true });
        continue;
      }
      for (const service of services) {
        if (!statementsAllow(statementsOfEndpoint(endpoints[service]), action, resource)) {
          missing.push(`${role} ${service} ${action} ${describePolicyValue(resource)}`);
        }
      }
    }
  }
  return missing;
}

describe('閉域のエンドポイントのポリシー（閉域対応 U2 BR1.1、BR1.2、BR1.5、BR1.7、BR1.9）', () => {
  const { templates } = synthClosed();
  const endpoints = endpointsByService(templates.App);

  test('ポリシーを付ける表は、ゲートウェイ型 2 とインターフェイス型の restricted の 10 で 12 種', () => {
    const restricted = [
      ...CLOSED_GATEWAY_ENDPOINTS.map((spec) => spec.service),
      ...CLOSED_ENDPOINTS.filter((spec) => spec.policy === 'restricted').map((spec) => spec.service),
    ];
    expect(restricted.sort()).toEqual([...RESTRICTED_ENDPOINT_SERVICES].sort());
  });

  test('12 種のエンドポイントに PolicyDocument があり、SSM の 3 つにはない（検証用 EC2 ありの構成）', () => {
    expect(Object.keys(endpoints).sort()).toEqual(
      [...RESTRICTED_ENDPOINT_SERVICES, ...VALIDATION_ENDPOINTS].sort(),
    );
    for (const service of RESTRICTED_ENDPOINT_SERVICES) {
      expect({ service, statements: statementsOfEndpoint(endpoints[service]).length > 0 }).toEqual({
        service,
        statements: true,
      });
    }
    for (const service of VALIDATION_ENDPOINTS) {
      expect({ service, policy: endpoints[service].Properties.PolicyDocument }).toEqual({ service, policy: undefined });
    }
  });

  test('どの文も Allow でプリンシパルは *', () => {
    for (const service of RESTRICTED_ENDPOINT_SERVICES) {
      for (const statement of statementsOfEndpoint(endpoints[service])) {
        expect({ service, Effect: statement.Effect, Principal: statement.Principal }).toEqual({
          service,
          Effect: 'Allow',
          Principal: '*',
        });
      }
    }
  });

  test('S3 に ECR のレイヤーのバケットの s3:GetObject がある（BR1.5）', () => {
    const layer = `arn:aws:s3:::prod-${REGION}-starport-layer-bucket/*`;
    const s3 = statementsOfEndpoint(endpoints.s3);
    expect(statementsAllow(s3, 's3:GetObject', layer)).toBe(true);
    expect(statementsAllow(s3, 's3:PutObject', layer)).toBe(false);
  });

  test('cognito-idp に署名のない cognito-idp:RevokeToken（Resource *）がある（BR1.9）', () => {
    const cognito = statementsOfEndpoint(endpoints['cognito-idp']);
    expect(
      cognito.some((s) => ([] as unknown[]).concat(s.Action).includes('cognito-idp:RevokeToken') && s.Resource === '*'),
    ).toBe(true);
  });

  test('states のエンドポイントはこのアプリのステートマシンとその実行だけを許す（FR9.2、NFR6.2）', () => {
    const states = statementsOfEndpoint(endpoints.states);
    expect(states).toHaveLength(1);
    expect(states[0].Action).toEqual('states:*');
    const resources = JSON.stringify(states[0].Resource);
    expect(resources).toContain('SurveyRun');
    expect(resources).toContain(':execution:');
    expect(([] as unknown[]).concat(states[0].Resource)).not.toContain('*');
  });

  test('DynamoDB のエンドポイントのポリシーに担当者の記録のテーブルが入る（NFR6.3）', () => {
    expect(JSON.stringify(statementsOfEndpoint(endpoints.dynamodb))).toContain('SurveyRunLease');
  });

  test('bedrock のエンドポイントはない（FR8.4）', () => {
    expect(endpoints.bedrock).toBeUndefined();
  });

  test('BR1.4: タスクロールと実行ロールの文の Resource が、対応するエンドポイントのポリシーに含まれる', () => {
    expect(roleStatements(templates.App).map((r) => r.role)).toEqual(expect.arrayContaining(['task', 'execution']));
    expect(uncoveredGrants(templates.App)).toEqual([]);
  });

  test('BR1.4: 推論の範囲 jp の閉域でも含まれる。bedrock-runtime の推論の ARN は国内の一覧そのもの（BR1.6）', () => {
    const jp = synthClosed({}, { closedInferenceScope: 'jp' }).templates.App;
    expect(uncoveredGrants(jp)).toEqual([]);
    const runtime = statementsOfEndpoint(endpointsByService(jp)['bedrock-runtime']);
    expect(runtime).toHaveLength(1);
    expect(([] as string[]).concat(runtime[0].Resource as string[]).sort()).toEqual(JP_INFERENCE_ARNS);
  });

  test('検証用 EC2 なしの構成でも 12 種にポリシーがあり、BR1.4 が通る', () => {
    const { App } = synthClosed({ createTestEnvironment: false }).templates;
    const withoutTest = endpointsByService(App);
    expect(Object.keys(withoutTest).sort()).toEqual([...RESTRICTED_ENDPOINT_SERVICES].sort());
    expect(uncoveredGrants(App)).toEqual([]);
  });
});

describe('インターフェイス型エンドポイントの AZ の数（閉域対応 U2 BR2.2〜BR2.4）', () => {
  const [firstAz, secondAz] = ['ap-northeast-1a', 'ap-northeast-1c'];

  function placementOf(template: Template) {
    const interfaceSubnets = Object.values(template.findResources('AWS::EC2::VPCEndpoint'))
      .filter((e) => e.Properties.VpcEndpointType === 'Interface')
      .map((e) => e.Properties.SubnetIds);
    const service = Object.values(template.findResources('AWS::ECS::Service'))[0];
    const alb = Object.values(template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer'))[0];
    const instance = Object.values(template.findResources('AWS::EC2::Instance'))[0];
    return {
      interfaceSubnets,
      serviceSubnets: service.Properties.NetworkConfiguration.AwsvpcConfiguration.Subnets,
      albSubnets: alb.Properties.Subnets,
      instanceSubnet: instance?.Properties.NetworkInterfaces[0].SubnetId,
    };
  }

  test('AZ の数 1: エンドポイント・ECS サービス・検証用 EC2 は先頭の AZ の 1 つ、ALB は 2 つ（BR2.3、BR2.4）', () => {
    const { App } = synthClosed({}, { closedEndpointAzCount: '1' }).templates;
    const subnets = subnetRefsByAz(App);
    const placement = placementOf(App);
    expect(placement.interfaceSubnets.length).toBe(INTERFACE_ENDPOINTS.length + VALIDATION_ENDPOINTS.length);
    for (const ids of placement.interfaceSubnets) {
      expect(ids).toEqual([subnets[firstAz]]);
    }
    expect(placement.serviceSubnets).toEqual([subnets[firstAz]]);
    expect(placement.instanceSubnet).toEqual(subnets[firstAz]);
    expect([...placement.albSubnets].sort((a, b) => a.Ref.localeCompare(b.Ref))).toEqual(
      [subnets[firstAz], subnets[secondAz]].sort((a, b) => a.Ref.localeCompare(b.Ref)),
    );
  });

  test('AZ の数 2: エンドポイント・ECS サービス・ALB は 2 つの AZ（U1 のまま）', () => {
    const { App } = synthClosed({}, { closedEndpointAzCount: '2' }).templates;
    const subnets = subnetRefsByAz(App);
    const both = [subnets[firstAz], subnets[secondAz]];
    const placement = placementOf(App);
    for (const ids of placement.interfaceSubnets) {
      expect(ids).toEqual(both);
    }
    expect(placement.serviceSubnets).toEqual(both);
    expect(placement.albSubnets).toEqual(both);
    // 検証用 EC2 は今も先頭のサブネット（2 から 1 にしても置き換わらない）
    expect(placement.instanceSubnet).toEqual(subnets[firstAz]);
  });

  test('VPC は AZ の数によらず 2 AZ のサブネットを持つ', () => {
    const { App } = synthClosed({}, { closedEndpointAzCount: '1' }).templates;
    expect(Object.keys(subnetRefsByAz(App)).sort()).toEqual([firstAz, secondAz]);
  });

  test('closedDevParameter の既定（コンテキストなし）は AZ の数 1（BR2.2）', () => {
    const { App } = synthClosed().templates;
    const subnets = subnetRefsByAz(App);
    expect(placementOf(App).serviceSubnets).toEqual([subnets[firstAz]]);
  });

  test('closedEndpointAzCount の誤りは synth で止まる（BR2.1）', () => {
    expect(() => synthClosed({}, { closedEndpointAzCount: '3' })).toThrow(/closedEndpointAzCount.*"3"/);
  });
});

describe('Resolver Inbound Endpoint（閉域対応 U2 BR3.1、BR3.4、BR3.5）', () => {
  const RESOLVER_CONTEXT = {
    closedCreateResolverEndpoint: 'true',
    closedResolverAllowedCidrs: '10.1.0.0/16,10.2.0.0/16',
  };

  test('既定では作らず、出力もない（BR3.1）', () => {
    const { App } = synthClosed().templates;
    App.resourceCountIs('AWS::Route53Resolver::ResolverEndpoint', 0);
    expect(App.toJSON().Outputs).not.toHaveProperty('ResolverEndpointId');
  });

  describe('作る指定と社内 DNS の CIDR 2 つ（AZ の数 1 の既定のまま）', () => {
    const synthesized = synthClosed({}, RESOLVER_CONTEXT);
    const { App } = synthesized.templates;

    test('cdk-nag の違反がない（Resolver の SG に新しい指摘が出ない）', () => {
      expect(() => synthesized.app.synth()).not.toThrow();
    });
    const resolvers = Object.values(App.findResources('AWS::Route53Resolver::ResolverEndpoint'));
    const sgIdOf = () => resolvers[0].Properties.SecurityGroupIds[0]['Fn::GetAtt'][0];

    test('INBOUND で、IP は 2 つの AZ のサブネットに 1 つずつ（BR3.5）', () => {
      expect(resolvers).toHaveLength(1);
      const subnets = subnetRefsByAz(App);
      expect(resolvers[0].Properties.Direction).toBe('INBOUND');
      expect(resolvers[0].Properties.IpAddresses).toEqual([
        { SubnetId: subnets['ap-northeast-1a'] },
        { SubnetId: subnets['ap-northeast-1c'] },
      ]);
    });

    test('SG の受け入れは CIDR ごとの TCP 53・UDP 53 だけで、0.0.0.0/0 がない（BR3.4）', () => {
      const sg = App.findResources('AWS::EC2::SecurityGroup')[sgIdOf()];
      const ingress = [
        ...(sg.Properties.SecurityGroupIngress ?? []),
        ...separateRules(App, 'AWS::EC2::SecurityGroupIngress', sgIdOf()),
      ].map((r: any) => `${r.CidrIp} ${r.IpProtocol} ${r.FromPort}-${r.ToPort}`);
      expect(ingress.sort()).toEqual([
        '10.1.0.0/16 tcp 53-53',
        '10.1.0.0/16 udp 53-53',
        '10.2.0.0/16 tcp 53-53',
        '10.2.0.0/16 udp 53-53',
      ]);
      expect(JSON.stringify(sg)).not.toContain('0.0.0.0/0');
    });

    test('SG の外向きは、すべて許可（-1、0.0.0.0/0）でなく、ルールもない（BR3.4）', () => {
      const sg = App.findResources('AWS::EC2::SecurityGroup')[sgIdOf()];
      expect(inlineEgress(sg)).toEqual([]);
      expect(separateRules(App, 'AWS::EC2::SecurityGroupEgress', sgIdOf())).toEqual([]);
      expect((sg.Properties.SecurityGroupEgress ?? []).some((r: any) => r.IpProtocol === '-1')).toBe(false);
    });

    test('Resolver の ID をスタックの出力にする', () => {
      const outputs = App.toJSON().Outputs as Record<string, any>;
      expect(outputs.ResolverEndpointId.Value).toEqual({
        'Fn::GetAtt': [Object.keys(App.findResources('AWS::Route53Resolver::ResolverEndpoint'))[0], 'ResolverEndpointId'],
      });
    });

    test('タスクの SG の外向き（NFR3 の固定）は変わらない: エンドポイントの SG と 2 つのプレフィックスリストへの 443 だけ', () => {
      const [taskSgId, taskSg] = Object.entries(App.findResources('AWS::EC2::SecurityGroup')).find(
        ([, sg]) => pathOf(sg) === `${CLOSED}/App/App/TaskSecurityGroup/Resource`,
      )!;
      const egress = [...inlineEgress(taskSg), ...separateRules(App, 'AWS::EC2::SecurityGroupEgress', taskSgId)].map(
        (r: any) =>
          `${r.DestinationPrefixListId ?? (r.DestinationSecurityGroupId ? 'sg' : r.CidrIp)} ${r.IpProtocol} ${r.FromPort}`,
      );
      expect(egress.sort()).toEqual(['pl-61a54008 tcp 443', 'pl-78a54011 tcp 443', 'sg tcp 443']);
    });
  });

  test('作る指定で CIDR がなければ synth で止まる（BR3.2）', () => {
    expect(() => synthClosed({}, { closedCreateResolverEndpoint: 'true' })).toThrow(/closedResolverAllowedCidrs/);
  });
});

describe('Flow Logs を作らない（閉域対応 U2 BR4.1）', () => {
  const { templates } = synthClosed();

  test('AWS::EC2::FlowLog がない', () => {
    templates.App.resourceCountIs('AWS::EC2::FlowLog', 0);
  });

  test('VPC7 の抑止の理由は、恒久的な理由（U2 までの一時的な抑止ではない）', () => {
    const vpc = Object.values(templates.App.findResources('AWS::EC2::VPC'))[0];
    const rules = vpc.Metadata.cdk_nag.rules_to_suppress as { id: string; reason: string }[];
    const vpc7 = rules.find((rule) => rule.id === 'AwsSolutions-VPC7');
    expect(vpc7?.reason).toBe(NAG_REASONS.closedFlowLogsNotCreated);
    expect(vpc7?.reason).not.toMatch(/一時的|U2/);
  });
});
