import { inferenceArnList } from '../lib/bedrock-arns';
import { resolveDeploymentProfile } from '../lib/deployment-profile';
import {
  ecrLayerBucketArn,
  EndpointAccessScope,
  endpointPolicyStatements,
  EXECUTION_LIST_ACTIONS,
  RESTRICTED_ENDPOINT_SERVICES,
  RestrictedEndpointService,
  TASK_LIST_ACTIONS,
  UNSIGNED_ACTIONS,
} from '../lib/endpoint-policies';
import { ACCOUNT, REGION, synthPublic } from './helpers/stages';

/**
 * エンドポイントのポリシーの組み立て（閉域対応 U2、functional-spec.md §3、BR1.1〜BR1.3、BR1.5〜BR1.7、BR1.9）。
 *
 * endpointPolicyStatements() は CDK の構成部品を作らない純粋な関数で、形だけの ARN の
 * EndpointAccessScope から、サービスごとの文を確かめる。テンプレートでの結線と BR1.4 の包含は
 * test/closed-stage.test.ts で確かめる。
 */

const ENV = { partition: 'aws', account: ACCOUNT, region: REGION };
const arn = (service: string, resource: string) => `arn:aws:${service}:${REGION}:${ACCOUNT}:${resource}`;

function scope(overrides: Partial<EndpointAccessScope> = {}): EndpointAccessScope {
  return {
    bucketArn: 'arn:aws:s3:::app-bucket',
    ecrLayerBucketArn: ecrLayerBucketArn('aws', REGION),
    tableArns: [arn('dynamodb', 'table/Personas'), arn('dynamodb', 'table/Sessions')],
    userPoolArn: arn('cognito-idp', 'userpool/ap-northeast-1_abc'),
    secretArns: [arn('secretsmanager', 'secret:a'), arn('secretsmanager', 'secret:b'), arn('secretsmanager', 'secret:c')],
    logGroupArns: [arn('logs', 'log-group:app'), arn('logs', 'log-group:app:*')],
    repositoryArn: arn('ecr', 'repository/app'),
    memoryArn: arn('bedrock-agentcore', 'memory/m-1'),
    inferenceArns: inferenceArnList(ENV, { inferenceScope: 'global', explicitModelIds: {} }),
    knowledgeBaseArn: arn('bedrock', 'knowledge-base/*'),
    stateMachineArn: arn('states', 'stateMachine:SurveyRun'),
    stateMachineExecutionsArn: arn('states', 'execution:SurveyRun:*'),
    ...overrides,
  };
}

interface StatementJson {
  Effect: string;
  Principal: unknown;
  Action: string[];
  Resource: string[];
}

const asArray = (value: unknown): string[] => ([] as string[]).concat(value as string | string[]);

/** The statements of one endpoint as JSON, with Action and Resource as arrays. */
function statementsOf(service: RestrictedEndpointService, accessScope = scope()): StatementJson[] {
  return endpointPolicyStatements(service, accessScope).map((statement) => {
    const json = statement.toStatementJson();
    return { ...json, Action: asArray(json.Action), Resource: asArray(json.Resource) };
  });
}

/** `<Action>` → sorted Resource of the endpoint's statements (one entry per statement). */
function shapeOf(service: RestrictedEndpointService, accessScope = scope()): Record<string, string[]> {
  return Object.fromEntries(
    statementsOf(service, accessScope).map((s) => [s.Action.join(','), [...s.Resource].sort()]),
  );
}

describe('endpointPolicyStatements: 12 種のサービスごとの文（functional-spec.md §3、BR1.1）', () => {
  const s = scope();

  test('対象は 12 種（ゲートウェイ型 2 とアプリが使うインターフェイス型 10）で、SSM の 3 つは含まない（BR1.7）', () => {
    expect([...RESTRICTED_ENDPOINT_SERVICES].sort()).toEqual(
      [
        'bedrock-agent-runtime',
        'bedrock-agentcore',
        'bedrock-agentcore-control',
        'bedrock-runtime',
        'cognito-idp',
        'dynamodb',
        'ecr.api',
        'ecr.dkr',
        'logs',
        's3',
        'secretsmanager',
        // アンケートの実行の開始・確認・停止（FR9.1）。bedrock のコントロールプレーンは外した（FR8.4）
        'states',
      ],
    );
    expect(RESTRICTED_ENDPOINT_SERVICES).not.toContain('bedrock');
    for (const service of ['ssm', 'ssmmessages', 'ec2messages']) {
      expect(RESTRICTED_ENDPOINT_SERVICES).not.toContain(service);
    }
  });

  test('すべての文が Allow で、プリンシパルは *（リソースだけを絞る、BR1.2）', () => {
    for (const service of RESTRICTED_ENDPOINT_SERVICES) {
      for (const statement of statementsOf(service)) {
        expect({ service, effect: statement.Effect, principal: statement.Principal }).toEqual({
          service,
          effect: 'Allow',
          principal: '*',
        });
      }
    }
  });

  test('資源の文の Action はそのサービスの *、一覧の API の文の Resource は *（BR1.2）', () => {
    expect(shapeOf('dynamodb')).toEqual({
      'dynamodb:*': [...s.tableArns, ...s.tableArns.map((t) => `${t}/index/*`)].sort(),
      'dynamodb:ListTables': ['*'],
    });
    expect(shapeOf('ecr.api')).toEqual({ 'ecr:*': [s.repositoryArn], 'ecr:GetAuthorizationToken': ['*'] });
    expect(shapeOf('ecr.dkr')).toEqual({ 'ecr:*': [s.repositoryArn], 'ecr:GetAuthorizationToken': ['*'] });
    expect(shapeOf('logs')).toEqual({ 'logs:*': [...s.logGroupArns].sort() });
    expect(shapeOf('secretsmanager')).toEqual({ 'secretsmanager:*': [...s.secretArns].sort() });
    expect(shapeOf('bedrock-agent-runtime')).toEqual({ 'bedrock:*': [s.knowledgeBaseArn] });
    for (const service of ['bedrock-agentcore', 'bedrock-agentcore-control'] as const) {
      expect(shapeOf(service)).toEqual({
        'bedrock-agentcore:*': [s.memoryArn, `${s.memoryArn}/*`].sort(),
        'bedrock-agentcore:ListMemories': ['*'],
      });
    }
  });

  test('states: このアプリのステートマシンとその実行に states:*（NFR6.2、基盤設計 3.3）', () => {
    expect(shapeOf('states')).toEqual({
      'states:*': [s.stateMachineArn, s.stateMachineExecutionsArn].sort(),
    });
  });

  test('iam:PassRole はどのエンドポイントにも入らない（バッチ推論のロールを外した）', () => {
    const withPassRole = RESTRICTED_ENDPOINT_SERVICES.filter((service) =>
      statementsOf(service).some((statement) => statement.Action.includes('iam:PassRole')),
    );
    expect(withPassRole).toEqual([]);
  });

  test('S3: アプリのバケットと <ARN>/*、ECR のレイヤーのバケットは s3:GetObject だけ（BR1.5）', () => {
    expect(ecrLayerBucketArn('aws', REGION)).toBe('arn:aws:s3:::prod-ap-northeast-1-starport-layer-bucket');
    expect(shapeOf('s3')).toEqual({
      's3:*': [s.bucketArn, `${s.bucketArn}/*`].sort(),
      's3:GetObject': ['arn:aws:s3:::prod-ap-northeast-1-starport-layer-bucket/*'],
    });
  });

  test('cognito-idp: User Pool に cognito-idp:*、署名のない RevokeToken は Resource *（BR1.9）', () => {
    expect(UNSIGNED_ACTIONS).toEqual(['cognito-idp:RevokeToken']);
    expect(shapeOf('cognito-idp')).toEqual({
      'cognito-idp:*': [s.userPoolArn],
      'cognito-idp:RevokeToken': ['*'],
    });
  });
});

describe('endpointPolicyStatements: Bedrock 系の推論のリソースは BedrockArns の一覧そのもの（BR1.6）', () => {
  const profiles = {
    global: resolveDeploymentProfile({ deploymentMode: 'public', region: REGION }),
    jp: resolveDeploymentProfile({ deploymentMode: 'public', region: REGION, inferenceScope: 'jp' }),
    'jp で global.* を明示': resolveDeploymentProfile({
      deploymentMode: 'public',
      region: REGION,
      inferenceScope: 'jp',
      bedrockModelId: 'global.anthropic.claude-sonnet-5',
    }),
  };

  test.each(Object.entries(profiles))('%s', (_name, profile) => {
    const inferenceArns = inferenceArnList(ENV, profile);
    const runtime = statementsOf('bedrock-runtime', scope({ inferenceArns }));
    expect(runtime).toHaveLength(1);
    expect(runtime[0].Action).toEqual(['bedrock:*']);
    expect([...runtime[0].Resource].sort()).toEqual([...inferenceArns].sort());
  });

  test('jp で global.* を明示したときは、その推論プロファイルの ARN がエンドポイントにも入る', () => {
    const inferenceArns = inferenceArnList(ENV, profiles['jp で global.* を明示']);
    const resources = statementsOf('bedrock-runtime', scope({ inferenceArns }))[0].Resource;
    expect(resources).toContain(`arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.anthropic.claude-sonnet-5`);
  });
});

describe('一覧の API の定数（BR1.3）', () => {
  test('TASK_LIST_ACTIONS は、公開構成のタスクロールの Resource * の文の Action と同じもの', () => {
    const { App } = synthPublic().templates;
    const statements = Object.values(App.findResources('AWS::IAM::Policy'))
      .filter((p) => JSON.stringify(p.Properties.Roles).includes('TaskRole'))
      .flatMap((p) => p.Properties.PolicyDocument.Statement as any[]);
    const wildcard = statements.filter((st) => st.Resource === '*' && asArray(st.Action).includes('dynamodb:ListTables'));
    expect(wildcard).toHaveLength(1);
    // テンプレートの Action は CDK が並べ替えて出す（minimizePolicies）。並びの固定はスナップショットが担う
    expect(asArray(wildcard[0].Action)).toEqual([...TASK_LIST_ACTIONS].sort());
  });

  test('実行ロールの一覧の API は ecr:GetAuthorizationToken だけ', () => {
    expect(EXECUTION_LIST_ACTIONS).toEqual(['ecr:GetAuthorizationToken']);
  });

  test('各一覧の API は、そのサービスのエンドポイントの Resource * の文に入る', () => {
    const listActionsOf = (service: RestrictedEndpointService) =>
      statementsOf(service).filter((st) => st.Resource.includes('*')).flatMap((st) => st.Action);
    const allListActions = RESTRICTED_ENDPOINT_SERVICES.flatMap(listActionsOf);
    for (const action of [...TASK_LIST_ACTIONS, ...EXECUTION_LIST_ACTIONS, ...UNSIGNED_ACTIONS]) {
      expect(allListActions).toContain(action);
    }
  });
});
