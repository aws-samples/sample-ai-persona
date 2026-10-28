import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as logs from 'aws-cdk-lib/aws-logs';
import { ClosedNetwork } from '../lib/constructs/closed-network';
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

/** U1 のインターフェイス型エンドポイント 10 種（NFR3.7）。 */
const INTERFACE_ENDPOINTS = [
  'bedrock',
  'bedrock-agent-runtime',
  'bedrock-agentcore',
  'bedrock-agentcore-control',
  'bedrock-runtime',
  'cognito-idp',
  'ecr.api',
  'ecr.dkr',
  'logs',
  'secretsmanager',
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
        const key = role.startsWith('BedrockBatchRole') ? 'batch' : 'task';
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
    expect(env.BATCH_INFERENCE_MODEL_ID).toBe('jp.anthropic.claude-haiku-4-5-20251001-v1:0');
  });

  test('閉域の jp: タスクロールとバッチ推論ロールの InvokeModel は同じ国内の一覧', () => {
    const resources = invokeModelResources(closedJp.templates.App);
    expect(resources.task).toEqual(JP_INFERENCE_ARNS);
    expect(resources.batch).toEqual(JP_INFERENCE_ARNS);
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
