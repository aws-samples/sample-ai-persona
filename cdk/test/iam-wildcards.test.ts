import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { createDevStage, DevStageOverrides } from '../lib/dev-stage';
import { synthClosed, templatesOf } from './helpers/stages';
import { exportedRef, exportTargets, flattenReference, PATH_METADATA_CONTEXT } from './template-paths';

/**
 * IAM の `*` と方針の大きさ、秘密の読み手（U9、NFR7.16、NFR3.14、security-design.md）。
 *
 * Stage を 3 通り（既定の dev の値、追加のペルソナモデルを有効にした値、U1 閉域の Stage）synth し、
 * 全スタック（公開は Data・App・Edge、閉域は Data・App）の全ロールの方針を調べる。`Resource: "*"` の文の操作は、下の例外の一覧と
 * 完全に一致させる（増えても、減って一覧が古くなっても失敗する）。IAM の文を足す変更は、
 * 同じ変更でこの一覧を直す。
 */

jest.setTimeout(60_000);

const ACCOUNT = '123456789012';
const REGION = 'ap-northeast-1';

/** `Resource: "*"` を残してよい操作と理由。リソースを指定できない API だけ。 */
const WILDCARD_EXCEPTIONS: Record<string, string> = {
  'dynamodb:ListTables': '一覧の API で、リソースを指定できない（起動時の接続の確認）',
  'bedrock-agentcore:ListMemories': '一覧の API で、リソースを指定できない',
  'ecr:GetAuthorizationToken': '認可トークンの取得で、リソースを指定できない。CDK が実行ロールに足す',
  // アンケートのステートマシンのログの出力（NFR5.6）。CloudWatch Logs の配信の API は
  // リソースを指定できない（CDK がステートマシンの実行ロールに足す）
  'logs:CreateLogDelivery': 'ログの配信の API で、リソースを指定できない',
  'logs:GetLogDelivery': 'ログの配信の API で、リソースを指定できない',
  'logs:UpdateLogDelivery': 'ログの配信の API で、リソースを指定できない',
  'logs:DeleteLogDelivery': 'ログの配信の API で、リソースを指定できない',
  'logs:ListLogDeliveries': 'ログの配信の API で、リソースを指定できない',
  'logs:PutResourcePolicy': 'ログの配信の API で、リソースを指定できない',
  'logs:DescribeResourcePolicies': 'ログの配信の API で、リソースを指定できない',
  'logs:DescribeLogGroups': 'ログの配信の API で、リソースを指定できない',
};
/** アンケートのステートマシンが呼ぶモデル（global の Haiku 5.5、基盤設計 3.1）。 */
const SURVEY_MODEL_ARNS = [
  `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/global.anthropic.claude-haiku-5-5`,
  'arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5',
];
/** enableAdditionalPersonaModels のときだけ足す例外。 */
const MANTLE_WILDCARD_EXCEPTIONS: Record<string, string> = {
  'bedrock-mantle:CallWithBearerToken':
    'リソースの種類を持たない（条件キー bedrock-mantle:BearerTokenType だけ）',
};

/** 推論の 3 つの ARN（基盤モデル・推論プロファイルはリージョンを `*`）。 */
const INFERENCE_ARNS = [
  'arn:aws:bedrock:*::foundation-model/*',
  `arn:aws:bedrock:*:${ACCOUNT}:inference-profile/*`,
  `arn:aws:bedrock:*:${ACCOUNT}:application-inference-profile/*`,
];

/**
 * タスクロールが Query してよい GSI（アプリが IndexName で引くものだけ、名前で付ける）。
 * OwnerIndex: src/services/stores/resource_share_store.py、TargetIndex: src/services/stores/persona_binding_store.py。
 * 型固定の GSI（NameIndex など）は外す途中で、アプリは使わないため付けない。
 * `data` は Data スタックの construct のパス（公開は AIPersonaDev/Data、閉域は AIPersonaDevClosed/Data）。
 */
function queriedIndexArns(data: string): string[] {
  const tableArn = (id: string) => exportedRef(`${data}/Database/${id}/Resource`, 'Arn');
  return [
    `${tableArn('Discussions')}/index/OwnerIndex`,
    `${tableArn('PersonaBindings')}/index/TargetIndex`,
    `${tableArn('Personas')}/index/OwnerIndex`,
    `${tableArn('SurveyTemplates')}/index/OwnerIndex`,
    `${tableArn('Surveys')}/index/OwnerIndex`,
    // 進行役が待ちと実行中を読む疎な索引（src/services/stores/survey_store.py）
    `${tableArn('Surveys')}/index/RunQueueIndex`,
  ];
}

/** IAM の 1 つの方針の上限（区切りの空白を除いた文字数）。 */
const MAX_INLINE_POLICY = 10_240;
const MAX_MANAGED_POLICY = 6_144;

// eslint-disable-next-line @typescript-eslint/no-require-imports
const FEATURE_FLAGS: Record<string, unknown> = require('../cdk.json').context;

interface Variant {
  name: string;
  /** Data スタックの construct のパス。 */
  data: string;
  build: () => Template[];
  expectedWildcards: string[];
}

const VARIANTS: Variant[] = [
  {
    name: '既定の dev の値',
    data: 'AIPersonaDev/Data',
    build: () => templates({}),
    expectedWildcards: Object.keys(WILDCARD_EXCEPTIONS),
  },
  {
    name: 'enableAdditionalPersonaModels: true',
    data: 'AIPersonaDev/Data',
    build: () => templates({ enableAdditionalPersonaModels: true }),
    expectedWildcards: [
      ...Object.keys(WILDCARD_EXCEPTIONS),
      ...Object.keys(MANTLE_WILDCARD_EXCEPTIONS),
    ],
  },
  {
    // U1 閉域: Data・App だけ（Edge なし）。検証用 EC2 のロールは管理ポリシーだけで、* の文を足さない
    name: '閉域の Stage（検証用 EC2 あり）',
    data: 'AIPersonaDevClosed/Data',
    build: () => Object.values(synthClosed().templates),
    expectedWildcards: Object.keys(WILDCARD_EXCEPTIONS),
  },
];

interface PolicyDoc {
  /** The resource that holds the document (policy logical ID or role logical ID). */
  holder: string;
  type: 'AWS::IAM::Policy' | 'AWS::IAM::ManagedPolicy' | 'AWS::IAM::Role';
  /** Logical IDs of the roles the document applies to. */
  roles: string[];
  document: { Statement: any[] };
}

function templates(overrides: DevStageOverrides): Template[] {
  const stage = createDevStage(
    new cdk.App({ context: { ...FEATURE_FLAGS, ...PATH_METADATA_CONTEXT } }),
    { account: ACCOUNT, ...overrides },
  );
  return Object.values(templatesOf(stage));
}

/** Export name → `{path.attribute}` across the given templates. */
const targetsOf = (all: Template[]) => new Map(all.flatMap((t) => [...exportTargets(t.toJSON())]));

function roleRefs(value: unknown): string[] {
  return ([value].flat().filter(Boolean) as any[]).map((r) => r.Ref ?? JSON.stringify(r));
}

function policyDocs(all: Template[]): PolicyDoc[] {
  const docs: PolicyDoc[] = [];
  for (const t of all) {
    for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy'] as const) {
      for (const [id, r] of Object.entries(t.findResources(type))) {
        docs.push({ holder: id, type, roles: roleRefs(r.Properties.Roles), document: r.Properties.PolicyDocument });
      }
    }
    for (const [id, r] of Object.entries(t.findResources('AWS::IAM::Role'))) {
      for (const p of r.Properties.Policies ?? []) {
        docs.push({ holder: id, type: 'AWS::IAM::Role', roles: [id], document: p.PolicyDocument });
      }
    }
  }
  return docs;
}

const statementsOf = (docs: PolicyDoc[]) => docs.flatMap((d) => d.document.Statement);
const actionsOf = (s: any): string[] => [s.Action].flat().filter(Boolean);
const resourcesOf = (s: any): any[] => [s.Resource].flat().filter((r) => r !== undefined);
const withAction = (docs: PolicyDoc[], action: string) =>
  statementsOf(docs).filter((s) => actionsOf(s).includes(action));

describe.each(VARIANTS)('IAM（$name）', ({ data: DATA, build, expectedWildcards }) => {
  const QUERIED_INDEX_ARNS = queriedIndexArns(DATA);
  const all = build();
  const docs = policyDocs(all);
  const targets = targetsOf(all);
  const flat = (s: any) => resourcesOf(s).map((r) => flattenReference(r, targets));

  test('方針が見つかる（空振りしない）', () => {
    expect(docs.length).toBeGreaterThan(3);
  });

  test('Resource が * の文の操作は、例外の一覧と完全に一致する', () => {
    const wildcardActions = new Set(
      statementsOf(docs)
        .filter((s) => resourcesOf(s).includes('*'))
        .flatMap(actionsOf),
    );
    expect([...wildcardActions].sort()).toEqual([...expectedWildcards].sort());
  });

  test('NotAction・NotResource を持つ文がない', () => {
    for (const s of statementsOf(docs)) {
      expect(s.NotAction).toBeUndefined();
      expect(s.NotResource).toBeUndefined();
    }
  });

  test('バッチ推論の操作と iam:PassRole はどのロールにもない（FR8.2、NFR5.10）', () => {
    for (const action of [
      'bedrock:CreateModelInvocationJob',
      'bedrock:GetModelInvocationJob',
      'iam:PassRole',
    ]) {
      expect(withAction(docs, action)).toEqual([]);
    }
  });

  test('bedrock:InvokeModel: タスクロールは推論の 3 つの ARN、ステートマシンはアンケートのモデルだけ', () => {
    const holders = (d: PolicyDoc) =>
      d.document.Statement.some((s) => actionsOf(s).includes('bedrock:InvokeModel'));
    const task = docs.filter((d) => holders(d) && d.roles.some((r) => r.startsWith('AppTaskDefinitionTaskRole')));
    const survey = docs.filter((d) => holders(d) && d.roles.some((r) => r.startsWith('SurveyRun')));
    expect(task.length).toBeGreaterThan(0);
    expect(survey.length).toBeGreaterThan(0);
    const resources = (list: PolicyDoc[]) =>
      statementsOf(list)
        .filter((s) => actionsOf(s).includes('bedrock:InvokeModel'))
        .flatMap(resourcesOf)
        .sort();
    expect(resources(task)).toEqual([...INFERENCE_ARNS].sort());
    expect(resources(survey)).toEqual([...SURVEY_MODEL_ARNS].sort());
  });

  test('states の操作はこのアプリのステートマシンとその実行だけ（NFR5.5、NFR5.7）', () => {
    for (const action of ['states:StartExecution', 'states:DescribeExecution', 'states:StopExecution']) {
      const statements = withAction(docs, action);
      expect(statements.length).toBeGreaterThan(0);
      for (const s of statements) {
        expect(resourcesOf(s)).not.toContain('*');
        expect(JSON.stringify(resourcesOf(s))).toContain('SurveyRun');
      }
    }
  });

  test('bedrock:Retrieve はこのアカウントの KB の ARN だけ', () => {
    const statements = withAction(docs, 'bedrock:Retrieve');
    expect(statements).toHaveLength(1);
    expect(resourcesOf(statements[0])).toEqual([
      `arn:aws:bedrock:${REGION}:${ACCOUNT}:knowledge-base/*`,
    ]);
  });

  test('AgentCore Memory の操作のリソースは Memory の ARN と <ARN>/*', () => {
    const statements = withAction(docs, 'bedrock-agentcore:GetMemory');
    expect(statements).toHaveLength(1);
    const memoryArn = exportedRef(`${DATA}/Memory/Default`, 'MemoryArn');
    expect(flat(statements[0]).sort()).toEqual([memoryArn, `${memoryArn}/*`]);
    expect(actionsOf(statements[0])).not.toContain('bedrock-agentcore:ListMemories');
  });

  test('テーブルのインデックスは名前で付け、/index/* はない。対象はアプリが Query する GSI だけ', () => {
    const indexArns = statementsOf(docs)
      .flatMap(flat)
      .filter((r) => r.includes('/index/'));
    expect(indexArns.filter((r) => r.endsWith('/index/*'))).toEqual([]);
    expect([...new Set(indexArns)].sort()).toEqual([...QUERIED_INDEX_ARNS].sort());
  });

  test('インデックスの操作は同じ表への操作と同じで、そのテーブルの ARN の文に並ぶ', () => {
    for (const indexArn of QUERIED_INDEX_ARNS) {
      const table = indexArn.slice(0, indexArn.indexOf('/index/'));
      const statements = statementsOf(docs).filter((s) => flat(s).includes(indexArn));
      expect(statements.length).toBeGreaterThan(0);
      for (const s of statements) {
        expect(flat(s)).toContain(table);
        expect(actionsOf(s)).toContain('dynamodb:Query');
      }
    }
  });

  test('方針の文字数が上限に収まる（Policy 10,240、ManagedPolicy 6,144、ロールごとのインラインの合計 10,240）', () => {
    const size = (d: PolicyDoc) => JSON.stringify(d.document).length;
    const inlineByRole: Record<string, number> = {};
    for (const d of docs) {
      if (d.type === 'AWS::IAM::ManagedPolicy') {
        expect(size(d)).toBeLessThanOrEqual(MAX_MANAGED_POLICY);
        continue;
      }
      expect(size(d)).toBeLessThanOrEqual(MAX_INLINE_POLICY);
      for (const role of d.roles) {
        inlineByRole[role] = (inlineByRole[role] ?? 0) + size(d);
      }
    }
    for (const total of Object.values(inlineByRole)) {
      expect(total).toBeLessThanOrEqual(MAX_INLINE_POLICY);
    }
  });

  test('3 つの秘密を読めるのは実行ロールだけ（今の付け方を保つ。NFR3.14）', () => {
    const readers = (secretLogicalId: string) =>
      [
        ...new Set(
          docs
            .filter((d) =>
              d.document.Statement.some(
                (s) =>
                  actionsOf(s).includes('secretsmanager:GetSecretValue') &&
                  JSON.stringify(resourcesOf(s)).includes(secretLogicalId),
              ),
            )
            .flatMap((d) => d.roles),
        ),
      ].sort();
    for (const secret of ['TokenEncryptionKeys', 'LoginCookieSigningKey', 'CognitoClientSecret']) {
      const roles = readers(secret);
      expect(roles).toHaveLength(1);
      expect(roles[0]).toMatch(/^AppTaskDefinitionExecutionRole[0-9A-F]{8}$/);
    }
    // 秘密を読む文はほかにない（ワイルドカードでも読ませない）
    const all = withAction(docs, 'secretsmanager:GetSecretValue');
    expect(all.flatMap(resourcesOf)).not.toContain('*');
  });
});

describe('IAM: Bedrock Mantle（enableAdditionalPersonaModels: true）', () => {
  const docs = policyDocs(templates({ enableAdditionalPersonaModels: true }));

  test('bedrock-mantle:CreateInference はこのアカウントの project の ARN（リージョンは *）だけ', () => {
    const statements = withAction(docs, 'bedrock-mantle:CreateInference');
    expect(statements).toHaveLength(1);
    expect(resourcesOf(statements[0])).toEqual([
      `arn:aws:bedrock-mantle:*:${ACCOUNT}:project/*`,
    ]);
  });

  test('既定の dev の値では Mantle の操作を付けない', () => {
    const defaults = policyDocs(templates({}));
    const actions = statementsOf(defaults).flatMap(actionsOf);
    expect(actions.filter((a) => a.startsWith('bedrock-mantle:'))).toEqual([]);
  });
});
